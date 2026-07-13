"""Promote routes + Phase 10 surface privacy (Task 9 / amendment A2).

Promote-to-memory / promote-to-task are human-authority mutations (the click IS the
authority), project-scoped, source='user'. And the A2 pin: the Costs / Projects / sessions
read models never expose a verbatim orchestration prompt or a secret — a canary seeded into
an agent_runs prompt (the debug-only verbatim store) appears on NONE of those surfaces."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from jarvis.config import MemoryConfig, load_config
from jarvis.memory.embeddings import FakeEmbedder
from jarvis.memory.service import MemoryService
from jarvis.memory.store import MemoryStore
from jarvis.observability.budget import BudgetService
from jarvis.observability.cost import load_pricing
from jarvis.observability.ledger import CostLedger
from jarvis.persistence.db import connect
from jarvis.projects import ProjectService, ProjectStore
from jarvis.scheduler.service import TaskService
from jarvis.scheduler.store import TaskStore
from jarvis.ui.auth import SESSION_COOKIE, AuthManager
from jarvis.ui.readmodels import UiServices, list_tasks
from jarvis.ui.server import create_app

_OPEN: list = []


@pytest.fixture(autouse=True)
async def _close():
    yield
    while _OPEN:
        await _OPEN.pop().close()


async def _app(tmp_path: Path):
    cfg = load_config(root=tmp_path, env_file=None)
    db = await connect(tmp_path / "a.db")
    _OPEN.append(db)
    lock = asyncio.Lock()
    project_store = ProjectStore(db, lock)
    pid = await project_store.create(name="Proj")  # id 1
    memory = MemoryService(
        store=MemoryStore(db, lock), embedder=FakeEmbedder(), config=MemoryConfig()
    )
    tasks = TaskService(TaskStore(db, lock), cfg.scheduler)
    ledger = CostLedger(db, lock, load_pricing(None))
    budgets = BudgetService(db, lock, cfg.budgets)
    projects = ProjectService(project_store)
    await projects.activate(pid)
    auth = AuthManager(token="tok")
    app = create_app(
        cfg,
        auth=auth,
        services=UiServices(memory=memory, tasks=tasks, ledger=ledger, budgets=budgets),
    )
    app.state.projects = projects
    return TestClient(app, base_url="http://127.0.0.1"), auth, memory, db, lock, pid


def _hdr(auth: AuthManager, *, post: bool = False) -> dict[str, str]:
    h = {"cookie": f"{SESSION_COOKIE}={auth.mint_session()}"}
    if post:
        h["origin"] = "http://127.0.0.1"
    return h


async def test_promote_to_memory_is_project_scoped_user_source(tmp_path: Path) -> None:
    client, auth, memory, _db, _lock, pid = await _app(tmp_path)
    r = client.post(
        "/api/memory/remember",
        json={"content": "the release ships in Q3", "type": "fact"},
        headers=_hdr(auth, post=True),
    )
    assert r.status_code == 200 and r.json()["ok"] is True
    rows = await memory.store.all_live()
    assert len(rows) == 1
    m = rows[0]
    assert m.content == "the release ships in Q3" and m.source == "user" and m.project_id == pid


async def test_promote_to_memory_requires_content(tmp_path: Path) -> None:
    client, auth, _m, _db, _lock, _pid = await _app(tmp_path)
    r = client.post("/api/memory/remember", json={"content": "  "}, headers=_hdr(auth, post=True))
    assert r.status_code == 400


async def test_promote_to_memory_rejects_malformed_content_and_type(tmp_path: Path) -> None:
    client, auth, _m, _db, _lock, _pid = await _app(tmp_path)
    headers = _hdr(auth, post=True)
    for body in (
        [],
        {"content": 1},
        {"content": "x" * 4001},
        {"content": "valid", "type": "unknown"},
        {"content": "valid", "type": []},
        {"content": "valid", "type": {}},
    ):
        assert client.post("/api/memory/remember", json=body, headers=headers).status_code == 400


async def test_promote_to_task_creates_user_task(tmp_path: Path) -> None:
    client, auth, _m, _db, _lock, _pid = await _app(tmp_path)
    r = client.post(
        "/api/tasks/create",
        json={
            "kind": "reminder",
            "title": "follow up",
            "payload": "email the team",
            "schedule_kind": "cron",
            "schedule_spec": "0 9 * * *",
        },
        headers=_hdr(auth, post=True),
    )
    assert r.status_code == 200 and r.json()["ok"] is True


async def test_promote_to_task_bad_schedule_is_400(tmp_path: Path) -> None:
    client, auth, _m, _db, _lock, _pid = await _app(tmp_path)
    r = client.post(
        "/api/tasks/create",
        json={"title": "x", "schedule_kind": "cron", "schedule_spec": "not a cron"},
        headers=_hdr(auth, post=True),
    )
    assert r.status_code == 400


async def test_a2_no_verbatim_prompt_on_cost_project_session_surfaces(tmp_path: Path) -> None:
    # Seed a canary into an agent_runs prompt (the debug-only verbatim store) + a model_calls
    # row. The Costs / Projects / sessions surfaces must NOT echo the verbatim prompt (A2).
    client, auth, _m, db, lock, pid = await _app(tmp_path)
    canary = "SECRET-ORCH-PROMPT-CANARY-9f"
    await db.execute(
        "INSERT INTO agent_runs (title, prompt, tools_scope, status, started_at, created_at, "
        "project_id) VALUES ('child', ?, '[]', 'ok', 't', 't', ?)",
        (f"do the thing with {canary}", pid),
    )
    await db.execute(
        "INSERT INTO model_calls (ts, project_id, purpose, provider, model, cost_usd, created_at) "
        "VALUES ('t', ?, 'orchestration', 'anthropic', 'claude-opus-4-8', 0.5, 't')",
        (pid,),
    )
    await db.commit()

    for path in ("/api/costs", "/api/projects", "/api/sessions"):
        body = client.get(path, headers=_hdr(auth)).text
        assert canary not in body, f"verbatim prompt leaked on {path}"


async def test_costs_exposes_safe_model_request_health(tmp_path: Path) -> None:
    client, auth, _m, _db, _lock, _pid = await _app(tmp_path)
    response = client.get("/api/costs", headers=_hdr(auth))
    assert response.status_code == 200
    health = response.json()["model_request_health"]
    assert health["totals"]["attempts"] == 0
    assert health["totals"]["error_rate"] is None
    assert health["by_provider_model"] == [] and health["error_classes"] == []
    assert health["recording_degraded"]["degraded"] is False


async def test_status_feed_reports_project_mode_spend(tmp_path: Path) -> None:
    client, auth, _m, _db, _lock, pid = await _app(tmp_path)
    status = client.get("/api/runner", headers=_hdr(auth)).json()
    assert status["mode"] == "approval"  # no ModeState wired ⇒ default
    assert status["project"] == {"id": pid, "name": "Proj"}
    assert status["today_spend_usd"] == 0.0 and status["ledger_degraded"] is False


# --- task project scoping (Task 9.5) ---------------------------------------


async def test_tasks_scoped_by_project_no_cross_leak(tmp_path: Path) -> None:
    # Two projects + a global task. A project page (?project_id=) shows that project's tasks
    # + global, and NEVER another project's — the isolation guarantee.
    client, auth, _m, db, lock, _pid = await _app(tmp_path)
    from jarvis.config import SchedulerConfig
    from jarvis.projects import ProjectStore
    from jarvis.scheduler.service import TaskService
    from jarvis.scheduler.store import TaskStore

    b = await ProjectStore(db, lock).create(name="ProjectB")  # id 2 (Proj=1 from _app)
    tasks = TaskService(TaskStore(db, lock), SchedulerConfig())

    async def _mk(title: str, project_id: int | None) -> None:
        await tasks.schedule(
            kind="reminder",
            title=title,
            payload="p",
            schedule_kind="cron",
            schedule_spec="0 9 * * *",
            created_by="user",
            project_id=project_id,
        )

    await _mk("A-only task", 1)
    await _mk("B-only task", b)
    await _mk("global task", None)

    def titles(rows):
        return {t["title"] for t in rows}

    # Project A page: A + global, never B.
    a_rows = await list_tasks(tasks, project_id=1)
    assert titles(a_rows) == {"A-only task", "global task"}
    # Project B page: B + global, never A.
    b_rows = await list_tasks(tasks, project_id=b)
    assert titles(b_rows) == {"B-only task", "global task"}
    # Global/unscoped Tasks screen: everything.
    all_rows = await list_tasks(tasks)
    assert titles(all_rows) == {"A-only task", "B-only task", "global task"}
    # And the row carries its scope.
    assert next(t for t in a_rows if t["title"] == "A-only task")["project_id"] == 1


async def test_promote_to_task_scopes_to_active_project(tmp_path: Path) -> None:
    client, auth, _m, db, lock, pid = await _app(tmp_path)
    r = client.post(
        "/api/tasks/create",
        json={
            "title": "promoted",
            "payload": "x",
            "schedule_kind": "cron",
            "schedule_spec": "0 9 * * *",
        },
        headers=_hdr(auth, post=True),
    )
    assert r.status_code == 200
    from jarvis.config import SchedulerConfig
    from jarvis.scheduler.service import TaskService
    from jarvis.scheduler.store import TaskStore

    tasks = TaskService(TaskStore(db, lock), SchedulerConfig())
    rows = await list_tasks(tasks, project_id=pid)
    assert any(t["title"] == "promoted" and t["project_id"] == pid for t in rows)
