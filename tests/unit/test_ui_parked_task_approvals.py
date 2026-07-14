"""Parked unattended-run review stays local, scoped, and one-time."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

from jarvis.config import load_config
from jarvis.core.execution import ExecutionContext
from jarvis.scheduler.store import ParkedContinuation, TaskRun
from jarvis.ui.approver import ParkedTaskApprovalManager
from jarvis.ui.auth import SESSION_COOKIE, AuthManager
from jarvis.ui.connections import ConnectionManager
from jarvis.ui.readmodels import UiServices
from jarvis.ui.server import (
    EXPECTED_CONTEXT_REVISION_HEADER,
    EXPECTED_PROJECT_HEADER,
    EXPECTED_SESSION_HEADER,
    WORKSPACE_HEADER,
    create_app,
)

_A = ExecutionContext(session_id=41, project_id=1)
_B = ExecutionContext(session_id=42, project_id=2)


class _Socket:
    async def send_json(self, _message: dict) -> None:
        return None


@dataclass
class _Task:
    id: int = 7
    title: str = "Collect weekly report"
    project_id: int | None = 1


def _continuation() -> ParkedContinuation:
    return ParkedContinuation.from_call(
        tool_id="call_exact",
        tool_name="write_file",
        tool_input={"path": "report.md", "content": "exact saved body"},
        decision_reason="writing needs approval",
    )


async def test_parked_task_manager_binds_one_time_nonce_to_live_workspace() -> None:
    connections = ConnectionManager(clock=lambda: 0.0)
    manager = ParkedTaskApprovalManager(connections)
    task = _Task()
    pending = manager.register(run_id=9, task=task, continuation=_continuation())
    assert pending.to_public()["tool_input"] == {"path": "report.md", "content": "exact saved body"}

    conn_a = connections.register(_Socket(), owner_session="same-browser")
    connections.bind_workspace(
        conn_a, owner_session="same-browser", workspace_id="a" * 24, context=_A
    )
    conn_b = connections.register(_Socket(), owner_session="same-browser")
    connections.bind_workspace(
        conn_b, owner_session="same-browser", workspace_id="b" * 24, context=_B
    )

    assert manager.visible_to(9, _B) is None
    assert await manager.mint_nonce(9, conn_b) is None
    nonce = await manager.mint_nonce(9, conn_a)
    assert nonce is not None
    # A nonce observed in another project does not consume or authorize the call.
    assert manager.reserve(9, nonce, "approve", context=_B, context_revision=1)[0] is None
    reserved, message = manager.reserve(9, nonce, "approve", context=_A, context_revision=1)
    assert reserved == pending and message == "reserved"
    assert manager.reserve(9, nonce, "approve", context=_A, context_revision=1)[0] is None

    # A failed host handoff never revives that credential; the person must visibly reopen the
    # exact call to obtain a fresh nonce.
    manager.complete(9, committed=False)
    assert await manager.mint_nonce(9, conn_a) is not None
    manager.complete(9, committed=True)
    assert manager.get(9) is None


async def test_parked_task_nonce_cannot_survive_same_context_aba() -> None:
    connections = ConnectionManager(clock=lambda: 0.0)
    manager = ParkedTaskApprovalManager(connections)
    manager.register(run_id=9, task=_Task(), continuation=_continuation())
    conn = connections.register(_Socket(), owner_session="same-browser")
    connections.bind_workspace(
        conn,
        owner_session="same-browser",
        workspace_id="a" * 24,
        context=_A,
        context_revision=7,
    )
    nonce = await manager.mint_nonce(9, conn)
    assert nonce is not None

    connections.update_workspace_context(
        owner_session="same-browser",
        workspace_id="a" * 24,
        context=_B,
        context_revision=8,
    )
    connections.update_workspace_context(
        owner_session="same-browser",
        workspace_id="a" * 24,
        context=_A,
        context_revision=9,
    )
    assert manager.reserve(9, nonce, "approve", context=_A, context_revision=9)[0] is None
    assert manager.reserve(9, nonce, "approve", context=_A, context_revision=7)[0] is None


def test_parked_task_endpoint_delegates_only_after_nonce_and_fresh_scope(tmp_path: Path) -> None:
    config = load_config(root=tmp_path, env_file=None)
    auth = AuthManager(token="test")
    owner = auth.mint_session()
    connections = ConnectionManager(clock=lambda: 0.0)
    app = create_app(config, auth=auth, connections=connections)
    task = _Task()
    continuation = _continuation()
    run = TaskRun(
        id=9,
        task_id=task.id,
        scheduled_for="2026-01-01T00:00:00+00:00",
        started_at="2026-01-01T00:00:00+00:00",
        finished_at=None,
        status="running",
        session_id=3,
        result_text=None,
        denied_count=0,
        error=None,
        cost_usd=None,
        created_at="2026-01-01T00:00:00+00:00",
        continuation=continuation,
        approval_state="pending",
    )

    class _Store:
        async def get(self, task_id: int):
            return task if task_id == task.id else None

        async def runs_for(self, task_id: int, *, limit: int = 20):
            assert task_id == task.id and limit == 200
            return [run]

    app.state.services = UiServices(tasks=SimpleNamespace(store=_Store()))
    workspace = SimpleNamespace(context=_A, context_revision=1)
    app.state.workspaces = SimpleNamespace(
        resolve=lambda **_kwargs: workspace,
        transition_lock=asyncio.Lock(),
        claim_matches=lambda item, context, revision: (
            item is workspace and context == _A and revision == 1
        ),
    )
    app.state.parked_task_approvals.register(run_id=run.id, task=task, continuation=continuation)
    seen: list[tuple[int, str]] = []

    async def resume_parked(run_id: int, resolution: str) -> bool:
        seen.append((run_id, resolution))
        return True

    app.state.resume_parked = resume_parked
    conn = connections.register(_Socket(), owner_session=owner)
    connections.bind_workspace(conn, owner_session=owner, workspace_id="w" * 24, context=_A)

    async def _nonce() -> str:
        value = await app.state.parked_task_approvals.mint_nonce(run.id, conn)
        assert value is not None
        return value

    # pytest executes async tests natively; this synchronous route test gets its one nonce from
    # a tiny direct event-loop bridge instead of pretending a POST body is an approval credential.
    nonce = asyncio.run(_nonce())
    client = TestClient(app, base_url="http://127.0.0.1")
    response = client.post(
        f"/api/parked-task-approvals/{run.id}/resolve",
        json={"nonce": nonce, "action": "approve"},
        headers={
            "cookie": f"{SESSION_COOKIE}={owner}",
            "origin": "http://127.0.0.1",
            WORKSPACE_HEADER: "w" * 24,
            EXPECTED_SESSION_HEADER: str(_A.session_id),
            EXPECTED_PROJECT_HEADER: str(_A.project_id),
            EXPECTED_CONTEXT_REVISION_HEADER: "1",
        },
    )
    assert response.status_code == 200 and response.json()["ok"] is True
    assert seen == [(run.id, "approve")]
    # It cannot call the host twice with the now-consumed nonce/run projection.
    replay = client.post(
        f"/api/parked-task-approvals/{run.id}/resolve",
        json={"nonce": nonce, "action": "approve"},
        headers={
            "cookie": f"{SESSION_COOKIE}={owner}",
            "origin": "http://127.0.0.1",
            WORKSPACE_HEADER: "w" * 24,
            EXPECTED_SESSION_HEADER: str(_A.session_id),
            EXPECTED_PROJECT_HEADER: str(_A.project_id),
            EXPECTED_CONTEXT_REVISION_HEADER: "1",
        },
    )
    assert replay.status_code == 404 and seen == [(run.id, "approve")]


def test_parked_task_browser_surface_is_exact_and_has_no_always_action() -> None:
    root = Path(__file__).parents[2]
    draft = (root / "src/jarvis/ui/static/ui/task-draft.js").read_text(encoding="utf-8")
    app = (root / "src/jarvis/ui/static/app.js").read_text(encoding="utf-8")
    assert "Exact saved tool call" in draft
    assert "Approve once & resume" in draft
    assert "Reject task run" in draft
    parked_dialog = draft[draft.index("openParkedTaskApproval") : draft.index("function runText")]
    assert "Always" not in parked_dialog
    assert 'type: "parked_task_approval_shown"' in app
    assert '"parked_task_approval_nonce"' in app
