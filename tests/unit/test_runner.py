"""BackgroundRunner + JobRunner: firing semantics and unattended job execution.

FakeClient + stepped clock throughout — no test sleeps. Connections opened here
are closed by an autouse fixture (an unclosed aiosqlite connection hangs pytest
at exit)."""

from __future__ import annotations

import asyncio
import datetime as dt
from pathlib import Path

import pytest

from jarvis.cli.jobs import JobRunner
from jarvis.config import SchedulerConfig, load_config
from jarvis.core import FakeClient, ToolCall, text_message, tool_use_message
from jarvis.permissions import PermissionGate, Policy
from jarvis.persistence.db import connect
from jarvis.persistence.sessions import SessionStore
from jarvis.scheduler.runner import BackgroundRunner, JobOutcome
from jarvis.scheduler.service import TaskService
from jarvis.scheduler.store import TaskStore
from jarvis.tools import Permission, ToolContext
from jarvis.tools.executor import ToolExecutor
from jarvis.tools.registry import ToolRegistry

UTC = dt.UTC
START = dt.datetime(2026, 7, 6, 8, 0, tzinfo=UTC)

_OPEN_DBS: list = []


@pytest.fixture(autouse=True)
async def _close_dbs():
    yield
    while _OPEN_DBS:
        await _OPEN_DBS.pop().close()


class Clock:
    def __init__(self, at: dt.datetime = START) -> None:
        self.at = at

    def __call__(self) -> dt.datetime:
        return self.at

    def advance(self, **kw: float) -> None:
        self.at += dt.timedelta(**kw)


async def _service(tmp_path: Path, **config_kw) -> tuple[TaskService, Clock, TaskStore]:
    clock = Clock()
    store = TaskStore(await connect(tmp_path / "tasks.db"))
    _OPEN_DBS.append(store.db)
    service = TaskService(store, SchedulerConfig(**config_kw), now=clock)
    return service, clock, store


async def _schedule(service: TaskService, **kw):
    return await service.schedule(
        kind=kw.get("kind", "job"),
        title=kw.get("title", "t"),
        payload=kw.get("payload", "do it"),
        schedule_kind=kw.get("schedule_kind", "interval"),
        schedule_spec=kw.get("schedule_spec", "3600"),
        created_by=kw.get("created_by", "user"),
        timezone="UTC",
    )


class Recorder:
    """Captures notify lines and can stand in as a run_job that returns/raises/counts."""

    def __init__(self) -> None:
        self.lines: list[str] = []
        self.job_calls = 0

    def notify(self, line: str) -> None:
        self.lines.append(line)


# --- BackgroundRunner: reminders ---------------------------------------------


async def test_reminder_fires_notify_once_and_records_ok(tmp_path: Path) -> None:
    service, clock, store = await _service(tmp_path)
    task = await _schedule(service, kind="reminder", payload="stretch")
    rec = Recorder()

    async def _run_job(_t):  # never called for reminders
        raise AssertionError("reminders make no model call")

    runner = BackgroundRunner(
        service, notify=rec.notify, run_job=_run_job, turn_lock=asyncio.Lock()
    )
    clock.advance(hours=1, minutes=1)  # past the 09:00 first fire
    assert await runner.check_due() == 1
    assert len(rec.lines) == 1 and "stretch" in rec.lines[0]
    runs = await store.runs_for(task.id)
    assert [r.status for r in runs] == ["ok"]
    assert (await store.get(task.id)).next_run_at == "2026-07-06T10:00:00+00:00"  # advanced


async def test_reminder_notify_precedes_recording(tmp_path: Path) -> None:
    # At-least-once: notify must happen before the run is recorded, so a crash in
    # between re-delivers (never drops). Force recording to fail and assert notify
    # already fired.
    service, clock, _ = await _service(tmp_path)
    await _schedule(service, kind="reminder", payload="drink water")
    rec = Recorder()

    async def _boom_begin(_due):
        raise RuntimeError("crash right after notifying")

    service.begin_run = _boom_begin  # type: ignore[assignment]
    runner = BackgroundRunner(service, notify=rec.notify, run_job=None, turn_lock=asyncio.Lock())
    clock.advance(hours=1, minutes=1)
    with pytest.raises(RuntimeError):
        await runner.check_due()
    assert rec.lines and "drink water" in rec.lines[0]  # delivered before the crash


async def test_reminder_beyond_grace_still_fires_annotated(tmp_path: Path) -> None:
    service, clock, _ = await _service(tmp_path, misfire_grace_seconds=3600)
    await _schedule(service, kind="reminder", payload="call mom")
    rec = Recorder()
    runner = BackgroundRunner(service, notify=rec.notify, run_job=None, turn_lock=asyncio.Lock())
    clock.advance(hours=5)  # well beyond grace
    await runner.check_due()
    assert "call mom" in rec.lines[0]
    assert "missed" in rec.lines[0]  # late beats silent, but flagged


# --- BackgroundRunner: jobs --------------------------------------------------


async def test_job_fires_run_job_and_records_outcome(tmp_path: Path) -> None:
    service, clock, store = await _service(tmp_path)
    task = await _schedule(service, kind="job", title="digest")
    # task_runs.session_id has a real FK; insert a session for the fake outcome to reference.
    await store.db.execute(
        "INSERT INTO sessions (id, created_at, updated_at, kind) VALUES (7, ?, ?, 'task')",
        (START.isoformat(), START.isoformat()),
    )
    await store.db.commit()
    rec = Recorder()

    async def _run_job(t):
        rec.job_calls += 1
        return JobOutcome(session_id=7, text="here is the digest", denied_count=0, cost_usd=0.02)

    runner = BackgroundRunner(
        service, notify=rec.notify, run_job=_run_job, turn_lock=asyncio.Lock()
    )
    clock.advance(hours=1, minutes=1)
    await runner.check_due()
    assert rec.job_calls == 1
    (run,) = await store.runs_for(task.id, limit=1)
    assert run.status == "ok"
    assert run.session_id == 7
    assert run.result_text == "here is the digest"
    assert run.cost_usd == 0.02
    assert any("digest" in line and "✓" in line for line in rec.lines)


async def test_job_exception_records_error_and_counts_failure(tmp_path: Path) -> None:
    service, clock, store = await _service(tmp_path)
    task = await _schedule(service, kind="job")
    rec = Recorder()

    async def _run_job(_t):
        raise RuntimeError("model API exploded")

    runner = BackgroundRunner(
        service, notify=rec.notify, run_job=_run_job, turn_lock=asyncio.Lock()
    )
    clock.advance(hours=1, minutes=1)
    await runner.check_due()  # must not propagate — a bad job can't kill the loop
    (run,) = await store.runs_for(task.id, limit=1)
    assert run.status == "error"
    assert "exploded" in run.error
    assert (await store.get(task.id)).consecutive_failures == 1
    assert any("✗" in line for line in rec.lines)


async def test_missed_job_records_missed_and_makes_no_model_call(tmp_path: Path) -> None:
    service, clock, store = await _service(tmp_path, misfire_grace_seconds=3600)
    task = await _schedule(service, kind="job", title="stale")
    rec = Recorder()

    async def _run_job(_t):
        rec.job_calls += 1
        return JobOutcome(session_id=1, text="should not happen")

    runner = BackgroundRunner(
        service, notify=rec.notify, run_job=_run_job, turn_lock=asyncio.Lock()
    )
    clock.advance(hours=5)  # beyond grace -> missed, not run
    await runner.check_due()
    assert rec.job_calls == 0  # the whole point: no unattended run of a stale job
    (run,) = await store.runs_for(task.id, limit=1)
    assert run.status == "missed"
    assert any("missed" in line for line in rec.lines)


async def test_held_turn_lock_delays_firing(tmp_path: Path) -> None:
    service, clock, store = await _service(tmp_path)
    await _schedule(service, kind="reminder", payload="ping")
    rec = Recorder()
    lock = asyncio.Lock()
    runner = BackgroundRunner(service, notify=rec.notify, run_job=None, turn_lock=lock)
    clock.advance(hours=1, minutes=1)

    await lock.acquire()  # simulate an interactive turn in progress
    check = asyncio.create_task(runner.check_due())
    await asyncio.sleep(0.05)
    assert rec.lines == []  # blocked on the lock — nothing fired yet
    lock.release()
    await check
    assert rec.lines and "ping" in rec.lines[0]  # fired once the turn released


# --- JobRunner: unattended execution -----------------------------------------


async def _job_runner(tmp_path: Path, client, policy: Policy | None = None) -> JobRunner:
    cfg = load_config(root=tmp_path, env_file=None)
    store = SessionStore(await connect(tmp_path / "sessions.db"))
    _OPEN_DBS.append(store.db)
    registry = ToolRegistry()
    registry.discover("jarvis.tools.builtin", ToolContext(config=cfg))
    executor = ToolExecutor(
        timeout=cfg.limits.tool_timeout_seconds, max_result_chars=cfg.limits.max_tool_result_chars
    )
    gate = PermissionGate(policy or Policy(), cfg.root)
    return JobRunner(
        session_store=store,
        client=client,
        registry=registry,
        executor=executor,
        gate=gate,
        config=cfg,
    )


async def _a_job_task(tmp_path: Path):
    # A standalone task row to hand to JobRunner (its own tiny store).
    store = TaskStore(await connect(tmp_path / "tasks2.db"))
    _OPEN_DBS.append(store.db)
    tid = await store.add(
        kind="job",
        title="summarize notes",
        payload="Read notes.txt and report the key number.",
        schedule_kind="once",
        schedule_spec="2026-07-06T09:00:00",
        timezone="UTC",
        next_run_at="2026-07-06T09:00:00+00:00",
        created_by="user",
    )
    return await store.get(tid)


async def test_job_runs_in_task_session_with_envelope(tmp_path: Path) -> None:
    client = FakeClient([text_message("The key number is 42.")])
    runner = await _job_runner(tmp_path, client)
    task = await _a_job_task(tmp_path)

    outcome = await runner.run(task)

    assert outcome.error is None
    assert outcome.text == "The key number is 42."
    assert outcome.cost_usd is not None and outcome.cost_usd > 0
    # ran in a fresh kind='task' session (invisible to --resume / reflection)
    assert outcome.session_id is not None
    assert await runner.session_store.latest_session_id() is None  # no interactive session exists
    saved = await runner.session_store.load_messages(outcome.session_id)
    first = saved[0]["content"]
    assert "Scheduled task #" in first and "STORED instruction" in first
    assert "Read notes.txt" in first  # the payload, verbatim, inside the envelope


async def test_unattended_denial_flows_back_and_is_counted(tmp_path: Path) -> None:
    # write_file is ALLOW in policy (as if the user had persisted it interactively);
    # the UnattendedGate must demote it, the denial must reach the model as an
    # is_error result, and denied_count must reflect it.
    policy = Policy(tools={"write_file": Permission.ALLOW})
    client = FakeClient(
        [
            tool_use_message(
                [ToolCall(id="c1", name="write_file", input={"path": "out.txt", "content": "hi"})]
            ),
            text_message("I couldn't write unattended, so here's what I found instead."),
        ]
    )
    runner = await _job_runner(tmp_path, client, policy=policy)
    task = await _a_job_task(tmp_path)

    outcome = await runner.run(task)

    assert outcome.denied_count == 1  # the demoted write
    assert outcome.error is None  # the model adapted and ended cleanly
    saved = await runner.session_store.load_messages(outcome.session_id)
    tool_results = [
        b
        for m in saved
        if isinstance(m["content"], list)
        for b in m["content"]
        if isinstance(b, dict) and b.get("type") == "tool_result"
    ]
    assert tool_results and tool_results[0]["is_error"] is True  # model saw the denial
    assert not (tmp_path / "out.txt").exists()  # nothing was written


async def test_non_end_turn_stop_is_reported_as_failure(tmp_path: Path) -> None:
    # A run that exhausts the iteration budget is a failure to report, not silence.
    cfg_client = FakeClient(
        [tool_use_message([ToolCall(id="c1", name="read_file", input={"path": "x"})])]
    )
    runner = await _job_runner(tmp_path, cfg_client)
    runner.config.limits.max_iterations = 1  # force the guard after one iteration
    task = await _a_job_task(tmp_path)

    outcome = await runner.run(task)
    assert outcome.error is not None
    assert "max_iterations" in outcome.error
