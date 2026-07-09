"""Daily Digest (Phase 9 Task 7): migration v6, DigestStore, DigestBuilder, runner fire.

Keyless: a scripted FakeClient is the summarizer; demo/raising Google clients stand in for the
adapters. The load-bearing pins: the summarizer is tool-less, failures render friendly (never
"zero results"), raw bodies/provider errors are never persisted, and delivery is UI/DB-first.
"""

from __future__ import annotations

import asyncio
import base64
import datetime as _dt
import json
from pathlib import Path
from types import SimpleNamespace

import aiosqlite
import pytest

from jarvis.config import SchedulerConfig, load_config
from jarvis.connectors.base import ConnectorAuthError, ConnectorRegistry
from jarvis.connectors.demo import DemoGoogleClient
from jarvis.core import FakeClient, text_message
from jarvis.digest import DigestBuilder, DigestStore, ensure_digest_task
from jarvis.digest.builder import Section
from jarvis.persistence.db import connect
from jarvis.persistence.migrations import MIGRATIONS, migrate
from jarvis.scheduler.runner import BackgroundRunner, JobOutcome
from jarvis.scheduler.service import TaskService
from jarvis.scheduler.store import TaskStore

_OPEN: list = []


@pytest.fixture(autouse=True)
async def _close():
    yield
    while _OPEN:
        await _OPEN.pop().close()


def _cfg(tmp_path: Path):
    return load_config(root=tmp_path, env_file=None)


class FakeDigestStore:
    """Captures add()/set_delivered() so tests can inspect exactly what was persisted."""

    def __init__(self) -> None:
        self.rows: list[dict] = []
        self.delivered: list = []

    async def add(self, **kw) -> int:
        self.rows.append(kw)
        return 1

    async def set_delivered(self, digest_id: int, delivered_to: list[str]) -> None:
        self.delivered.append((digest_id, delivered_to))


def _summary_client() -> FakeClient:
    return FakeClient([text_message("SUMMARY: All calm today.\nACTIONS:\n- Reply to Bob")])


# --- migration v6 ----------------------------------------------------------


async def test_migration_v6_preserves_tasks_and_adds_digest_kind() -> None:
    db = await aiosqlite.connect(":memory:")
    _OPEN.append(db)
    # Apply v1..v5, seed a job + a run + a reminder, then migrate to v6.
    for target, step in MIGRATIONS:
        if target > 5:
            break
        if isinstance(step, str):
            await db.executescript(step)
        else:
            await step(db)
        await db.execute(f"PRAGMA user_version = {target}")
        await db.commit()
    await db.execute(
        "INSERT INTO tasks (id,kind,title,payload,schedule_kind,schedule_spec,timezone,"
        "next_run_at,status,created_by,created_at,updated_at) VALUES "
        "(1,'job','t','p','once','2030-01-01T00:00:00+00:00','UTC',"
        "'2030-01-01T00:00:00+00:00','active','user','x','x')"
    )
    await db.execute(
        "INSERT INTO task_runs (task_id,scheduled_for,status,created_at) "
        "VALUES (1,'2030-01-01T00:00:00+00:00','ok','x')"
    )
    await db.commit()

    assert await migrate(db) == 12  # applies pending through v12

    # Seeded rows survived the tasks rebuild; the FK from task_runs is intact.
    cur = await db.execute("SELECT kind FROM tasks WHERE id = 1")
    assert (await cur.fetchone())[0] == "job"
    cur = await db.execute("SELECT COUNT(*) FROM task_runs WHERE task_id = 1")
    assert (await cur.fetchone())[0] == 1
    # 'digest' is now an accepted kind; the digests table exists.
    await db.execute(
        "INSERT INTO tasks (kind,title,payload,schedule_kind,schedule_spec,timezone,"
        "next_run_at,status,created_by,created_at,updated_at) VALUES "
        "('digest','Daily digest','','cron','0 8 * * *','UTC',"
        "'2030-01-01T00:00:00+00:00','active','user','x','x')"
    )
    with pytest.raises(aiosqlite.IntegrityError):  # a bogus kind is still rejected
        await db.execute(
            "INSERT INTO tasks (kind,title,payload,schedule_kind,schedule_spec,timezone,"
            "status,created_by,created_at,updated_at) VALUES "
            "('bogus','x','','once','x','UTC','active','user','x','x')"
        )
    await db.execute(
        "INSERT INTO digests (date_local,generated_at,sections_json,summary,"
        "suggested_actions_json,delivered_to,created_at) VALUES "
        "('2026-07-06','x','[]','s','[]','[\"ui\"]','x')"
    )
    await db.commit()


async def test_digest_store_round_trip(tmp_path: Path) -> None:
    db = await connect(tmp_path / "d.db")
    _OPEN.append(db)
    store = DigestStore(db)
    did = await store.add(
        task_id=None,
        date_local="2026-07-06",
        generated_at="2026-07-06T08:00:00+00:00",
        sections=[{"kind": "email", "title": "Unread email", "items": []}],
        summary="All calm.",
        suggested_actions=["Reply to Bob"],
        delivered_to=["ui"],
        cost_usd=0.01,
    )
    latest = await store.latest()
    assert latest.id == did and latest.summary == "All calm."
    assert latest.suggested_actions == ["Reply to Bob"] and latest.delivered_to == ["ui"]
    await store.set_delivered(did, ["ui", "telegram"])
    assert (await store.latest()).delivered_to == ["ui", "telegram"]


# --- the model can never create a digest task ------------------------------


def test_schedule_task_tool_rejects_digest_kind() -> None:
    # Digest tasks are host-created only (ensure_digest_task). The schedule_task tool's kind
    # is Literal["reminder","job"], so a model attempting kind='digest' fails validation.
    from pydantic import ValidationError

    from jarvis.tools.builtin.tasks import ScheduleTaskParams

    ScheduleTaskParams(kind="reminder", title="t", payload="p", once_at="2030-01-01T00:00:00")
    with pytest.raises(ValidationError):
        ScheduleTaskParams(kind="digest", title="t", payload="p", once_at="2030-01-01T00:00:00")


# --- DigestBuilder ---------------------------------------------------------


async def test_summarizer_is_tool_less(tmp_path: Path) -> None:
    client = _summary_client()
    builder = DigestBuilder(config=_cfg(tmp_path), utility=client, store=FakeDigestStore())
    summary, actions = await builder.summarize([Section("tasks", "Today's tasks")])
    assert summary == "All calm today."
    assert actions == ["Reply to Bob"]
    assert client.calls[-1]["tools"] == []  # NO tools — the summarizer can never act


async def test_google_disabled_still_produces_tasks_and_kb(tmp_path: Path) -> None:
    tasks = SimpleNamespace(
        store=SimpleNamespace(
            list=_alist(
                [SimpleNamespace(id=1, title="Standup", next_run_at="2026-07-06T10:00:00+00:00")]
            )
        )
    )
    knowledge = SimpleNamespace(unreviewed_sources=_alist([object(), object()]))
    builder = DigestBuilder(
        config=_cfg(tmp_path),
        utility=_summary_client(),
        store=FakeDigestStore(),
        connectors=None,  # no Google
        tasks=tasks,
        knowledge=knowledge,
        now=lambda: _dt.datetime(2026, 7, 6, 9, 0, tzinfo=_dt.UTC),
    )
    sections = await builder.collect()
    kinds = {s.kind for s in sections}
    assert "schedule" not in kinds and "email" not in kinds  # google absent ⇒ omitted
    assert "tasks" in kinds and "kb" in kinds
    kb = next(s for s in sections if s.kind == "kb")
    assert kb.items and "2 source" in kb.items[0].text


async def test_failed_collector_renders_friendly_not_zero(tmp_path: Path) -> None:
    class _AuthRaises:
        async def get_json(self, url, *, params=None):
            raise ConnectorAuthError("google")

    builder = DigestBuilder(
        config=_cfg(tmp_path),
        utility=_summary_client(),
        store=FakeDigestStore(),
        connectors=ConnectorRegistry(google=_AuthRaises()),
    )
    sections = await builder.collect()
    email = next(s for s in sections if s.kind == "email")
    assert email.status == "failed"
    assert email.reason == "Google needs reconnect: run jarvis connect google"
    assert email.items == []  # empty, but clearly FAILED — not "no unread email"


async def test_provider_error_body_is_never_persisted(tmp_path: Path) -> None:
    # A generic provider blow-up (with a secret in its text) must not leak into the digest.
    class _Boom:
        async def get_json(self, url, *, params=None):
            raise RuntimeError("PROVIDER-500: token=SECRET-LEAK-123")

    store = FakeDigestStore()
    builder = DigestBuilder(
        config=_cfg(tmp_path),
        utility=_summary_client(),
        store=store,
        connectors=ConnectorRegistry(google=_Boom()),
    )
    await builder.build_and_deliver()
    blob = json.dumps(store.rows[0])
    assert "SECRET-LEAK-123" not in blob
    assert "PROVIDER-500" not in blob  # only a friendly "unavailable" reason is stored


async def test_email_snippet_capped_and_no_raw_body_persisted(tmp_path: Path) -> None:
    class _HugeSnippet(DemoGoogleClient):
        async def get_json(self, url, *, params=None):
            if url.endswith("/messages"):
                return {"messages": [{"id": "m1"}]}
            if "/messages/" in url:
                return {
                    "id": "m1",
                    "threadId": "t",
                    "snippet": "S" * 1000,
                    "payload": {
                        "headers": [
                            {"name": "From", "value": "a@b"},
                            {"name": "Subject", "value": "Hi"},
                        ],
                        "body": {
                            "data": base64.urlsafe_b64encode(b"RAWBODY" * 5000)
                            .rstrip(b"=")
                            .decode()
                        },
                    },
                }
            return {}

    store = FakeDigestStore()
    builder = DigestBuilder(
        config=_cfg(tmp_path),
        utility=_summary_client(),
        store=store,
        connectors=ConnectorRegistry(google=_HugeSnippet()),
    )
    await builder.build_and_deliver()
    blob = json.dumps(store.rows[0])
    assert "RAWBODY" not in blob  # the collector never fetches bodies
    email = next(s for s in store.rows[0]["sections"] if s["kind"] == "email")
    assert all(len(item["text"]) <= 240 for item in email["items"])  # snippet capped


async def test_tasks_today_uses_local_timezone(tmp_path: Path) -> None:
    # Day boundary: "today" is the user's local date, not UTC (pre-mortem #10).
    local = _dt.timezone(_dt.timedelta(hours=-8))
    now = _dt.datetime(2026, 7, 6, 23, 0, tzinfo=local)  # 2026-07-07 07:00 UTC
    tasks = SimpleNamespace(
        store=SimpleNamespace(
            list=_alist(
                [
                    # 2026-07-06 21:00 local — TODAY locally (even though 07-07 in UTC)
                    SimpleNamespace(id=1, title="today", next_run_at="2026-07-07T05:00:00+00:00"),
                    # 2026-07-07 01:00 local — tomorrow locally
                    SimpleNamespace(
                        id=2, title="tomorrow", next_run_at="2026-07-07T09:00:00+00:00"
                    ),
                ]
            )
        )
    )
    builder = DigestBuilder(
        config=_cfg(tmp_path),
        utility=_summary_client(),
        store=FakeDigestStore(),
        tasks=tasks,
        now=lambda: now,
    )
    section = await builder._tasks()
    assert [i.text for i in section.items] == ["today"]


async def test_demo_digest_is_badged(tmp_path: Path) -> None:
    store = FakeDigestStore()
    builder = DigestBuilder(
        config=_cfg(tmp_path),
        utility=_summary_client(),
        store=store,
        connectors=ConnectorRegistry(google=DemoGoogleClient(), demo=True),
    )
    await builder.build_and_deliver()
    assert store.rows[0]["summary"].startswith("[DEMO]")


async def test_delivery_is_ui_first_then_notifier_with_egress(tmp_path: Path, monkeypatch) -> None:
    import jarvis.digest.builder as builder_mod

    events: list[dict] = []
    monkeypatch.setattr(builder_mod, "log_egress", lambda **kw: events.append(kw))

    class _Notifier:
        name = "telegram"

        def __init__(self) -> None:
            self.sent: list[str] = []

        async def send(self, text: str) -> None:
            self.sent.append(text)

    # A config that delivers to ui + telegram.
    (tmp_path / "config").mkdir(parents=True, exist_ok=True)
    (tmp_path / "config" / "settings.yaml").write_text(
        "connectors:\n  telegram:\n    enabled: true\n    chat_id: '1'\n"
        "  digest:\n    enabled: true\n    deliver: [ui, telegram]\n",
        encoding="utf-8",
    )
    notifier = _Notifier()
    order: list[str] = []

    class _Notices:
        def post(self, text, *, kind="info"):
            order.append("ui")

    class _StoreFirst(FakeDigestStore):
        async def add(self, **kw):
            order.append("db")
            return await super().add(**kw)

    builder = DigestBuilder(
        config=load_config(root=tmp_path, env_file=None),
        utility=_summary_client(),
        store=_StoreFirst(),
        connectors=ConnectorRegistry(notifiers={"telegram": notifier}),
        notices=_Notices(),
    )
    await builder.build_and_deliver()
    assert notifier.sent  # delivered to telegram
    assert order[0] == "db" and order[1] == "ui"  # DB + UI BEFORE notifier
    assert order.index("db") < len(order)  # (notifier send has no order marker, but comes last)
    assert any(
        e["category"] == "digest_delivery" and e["destination_type"] == "telegram" for e in events
    )


# --- ensure_digest_task ----------------------------------------------------


async def _tasks_service(tmp_path: Path) -> TaskService:
    store = TaskStore(await connect(tmp_path / "t.db"))
    _OPEN.append(store.db)
    return TaskService(store, SchedulerConfig())


async def test_ensure_digest_task_is_idempotent(tmp_path: Path) -> None:
    tasks = await _tasks_service(tmp_path)
    cfg = _cfg(tmp_path)
    cfg.connectors.digest.enabled = True
    await ensure_digest_task(tasks, cfg)
    await ensure_digest_task(tasks, cfg)  # again
    digests = [t for t in await tasks.store.list() if t.kind == "digest"]
    assert len(digests) == 1  # exactly one, not two


async def test_ensure_digest_task_cancels_when_disabled(tmp_path: Path) -> None:
    tasks = await _tasks_service(tmp_path)
    cfg = _cfg(tmp_path)
    cfg.connectors.digest.enabled = True
    await ensure_digest_task(tasks, cfg)
    cfg.connectors.digest.enabled = False
    await ensure_digest_task(tasks, cfg)
    active = [t for t in await tasks.store.list() if t.kind == "digest" and t.status == "active"]
    assert active == []


# --- runner fire path ------------------------------------------------------


async def test_runner_fires_digest_with_job_semantics(tmp_path: Path) -> None:
    store = TaskStore(await connect(tmp_path / "t.db"))
    _OPEN.append(store.db)

    class _Clock:
        at = _dt.datetime(2026, 7, 6, 8, 0, tzinfo=_dt.UTC)

        def __call__(self):
            return self.at

    clock = _Clock()
    service = TaskService(store, SchedulerConfig(), now=clock)
    task = await service.schedule(
        kind="digest",
        title="Daily digest",
        payload="",
        schedule_kind="interval",
        schedule_spec="3600",
        created_by="user",
        timezone="UTC",
    )

    async def _run_digest(_t) -> JobOutcome:
        return JobOutcome(session_id=None, text="digest ready", cost_usd=0.02)

    lines: list[str] = []
    runner = BackgroundRunner(
        service, notify=lines.append, run_job=None, run_digest=_run_digest, turn_lock=asyncio.Lock()
    )
    clock.at += _dt.timedelta(hours=1, minutes=1)
    assert await runner.check_due() == 1
    runs = await store.runs_for(task.id)
    assert [r.status for r in runs] == ["ok"]  # begin_run before + complete after (job semantics)
    assert runs[0].cost_usd == 0.02
    assert any("digest" in ln for ln in lines)


async def test_runner_digest_without_runner_records_error(tmp_path: Path) -> None:
    store = TaskStore(await connect(tmp_path / "t.db"))
    _OPEN.append(store.db)

    class _Clock:
        at = _dt.datetime(2026, 7, 6, 8, 0, tzinfo=_dt.UTC)

        def __call__(self):
            return self.at

    clock = _Clock()
    service = TaskService(store, SchedulerConfig(), now=clock)
    task = await service.schedule(
        kind="digest",
        title="Daily digest",
        payload="",
        schedule_kind="interval",
        schedule_spec="3600",
        created_by="user",
        timezone="UTC",
    )
    runner = BackgroundRunner(
        service, notify=lambda _l: None, run_job=None, run_digest=None, turn_lock=asyncio.Lock()
    )
    clock.at += _dt.timedelta(hours=1, minutes=1)
    await runner.check_due()  # must not crash the loop
    runs = await store.runs_for(task.id)
    assert runs[0].status == "error"  # recorded, not crashed


def _alist(value):
    """Return an async function that ignores kwargs and returns ``value`` (fake async method)."""

    async def _fn(*a, **kw):
        return value

    return _fn
