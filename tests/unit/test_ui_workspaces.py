"""Phase 16.5 browser-workspace isolation tests.

These use two simultaneous live socket doubles under the same authenticated UI cookie.  The
assertions prove that server-owned workspace binding, not a browser-provided project/session id,
chooses the delivery recipient.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from jarvis.config import load_config
from jarvis.core.client import ToolCall
from jarvis.core.events import TextDelta
from jarvis.core.execution import (
    ExecutionContext,
    bind_execution_context,
    current_execution_context,
)
from jarvis.permissions.gate import Decision
from jarvis.persistence import SessionStore
from jarvis.persistence.db import connect
from jarvis.projects import ProjectService, ProjectStore
from jarvis.tools import Permission
from jarvis.ui.approver import ApprovalManager
from jarvis.ui.auth import SESSION_COOKIE, AuthManager
from jarvis.ui.connections import ConnectionManager
from jarvis.ui.readmodels import UiServices
from jarvis.ui.server import WORKSPACE_HEADER, create_app
from jarvis.ui.session import UiSession
from jarvis.ui.workspaces import UiWorkspaceRegistry

_OPEN: list = []


@pytest.fixture(autouse=True)
async def _close() -> None:
    yield
    while _OPEN:
        await _OPEN.pop().close()


class _Socket:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send_json(self, message: dict) -> None:
        self.sent.append(message)


class _Loop:
    """A tiny loop double that makes the task-local scope observable."""

    def __init__(self, label: str) -> None:
        self.label = label
        self.contexts: list[ExecutionContext | None] = []

    async def run_turn(self, messages: list[dict], *, on_event) -> object:
        self.contexts.append(current_execution_context())
        on_event(TextDelta(f"{self.label}: reply"))
        return SimpleNamespace(
            messages=[*messages, {"role": "assistant", "content": f"{self.label}: reply"}],
            text=f"{self.label}: reply",
        )


class _TaskRecorder:
    def __init__(self) -> None:
        self.project_ids: list[int | None] = []

    async def schedule(self, **kwargs):
        self.project_ids.append(kwargs["project_id"])
        return SimpleNamespace(id=len(self.project_ids))


async def _registry(
    tmp_path: Path, *, on_context_replaced=None
) -> tuple[UiWorkspaceRegistry, ConnectionManager, ProjectService]:
    db = await connect(tmp_path / "workspaces.db")
    _OPEN.append(db)
    lock = asyncio.Lock()
    store = SessionStore(db, lock)
    projects = ProjectService(ProjectStore(db, lock))
    connections = ConnectionManager(clock=lambda: 0.0)
    loops: list[_Loop] = []

    def make_session(workspace) -> UiSession:
        loop = _Loop(f"workspace-{len(loops) + 1}")
        loops.append(loop)
        return UiSession(
            loop=loop,
            connections=connections,
            sessions=store,
            project_id=workspace.project.project_id,
        )

    registry = UiWorkspaceRegistry(
        connections=connections,
        make_session=make_session,
        projects=projects,
        on_context_replaced=on_context_replaced,
    )
    return registry, connections, projects


async def test_two_live_sockets_keep_turn_events_and_project_contexts_isolated(
    tmp_path: Path,
) -> None:
    registry, connections, projects = await _registry(tmp_path)
    project_a = await projects.store.create(name="Project A")
    project_b = await projects.store.create(name="Project B")

    socket_a, socket_b = _Socket(), _Socket()
    # Deliberately share the auth cookie: tabs under one local login still need isolation.
    conn_a = connections.register(socket_a, owner_session="same-browser")
    conn_b = connections.register(socket_b, owner_session="same-browser")
    workspace_a = await registry.attach(conn_a, owner_session="same-browser")
    workspace_b = await registry.attach(conn_b, owner_session="same-browser")

    await workspace_a.select_project(project_a)
    registry.refresh_context(workspace_a)
    await workspace_b.select_project(project_b)
    registry.refresh_context(workspace_b)
    assert workspace_a.context.project_id == project_a
    assert workspace_b.context.project_id == project_b
    assert workspace_a.context.session_id != workspace_b.context.session_id
    with bind_execution_context(workspace_a.context):
        assert projects.current().project_id == project_a
    with bind_execution_context(workspace_b.context):
        assert projects.current().project_id == project_b

    await workspace_a.session.handle_text("A only")
    await asyncio.gather(*list(workspace_a.session._pushes))

    a_events = [message for message in socket_a.sent if message.get("kind") == "event"]
    b_events = [message for message in socket_b.sent if message.get("kind") == "event"]
    assert a_events and not b_events
    assert all(
        (event["session_id"], event["project_id"])
        == (workspace_a.context.session_id, project_a)
        for event in a_events
    )
    # The tool project provider resolves from the task-local execution context, not the mutable
    # process active project.  This is what keeps a project-scoped tool inside A's workspace.
    assert workspace_a.session.loop.contexts == [workspace_a.context]
    assert workspace_b.session.messages == []

    await workspace_b.session.handle_text("B only")
    await asyncio.gather(*list(workspace_b.session._pushes))
    b_events = [message for message in socket_b.sent if message.get("kind") == "event"]
    assert b_events
    assert all(
        (event["session_id"], event["project_id"])
        == (workspace_b.context.session_id, project_b)
        for event in b_events
    )
    assert workspace_b.session.loop.contexts == [workspace_b.context]


async def test_workspace_id_is_bound_to_its_authenticated_cookie(tmp_path: Path) -> None:
    registry, connections, _projects = await _registry(tmp_path)
    conn = connections.register(_Socket(), owner_session="owner-a")
    workspace = await registry.attach(conn, owner_session="owner-a")

    assert (
        registry.resolve(owner_session="owner-a", workspace_id=workspace.workspace_id) is workspace
    )
    assert registry.resolve(owner_session="owner-b", workspace_id=workspace.workspace_id) is None
    assert registry.resolve(owner_session="owner-a", workspace_id="short") is None


async def test_emergency_cancel_covers_every_live_workspace(tmp_path: Path) -> None:
    registry, connections, _projects = await _registry(tmp_path)
    workspace_a = await registry.attach(
        connections.register(_Socket(), owner_session="same-browser"),
        owner_session="same-browser",
    )
    workspace_b = await registry.attach(
        connections.register(_Socket(), owner_session="same-browser"),
        owner_session="same-browser",
    )
    workspace_a.session._current = asyncio.create_task(asyncio.Event().wait())
    workspace_b.session._current = asyncio.create_task(asyncio.Event().wait())

    with pytest.raises(RuntimeError, match="busy"):
        await workspace_a.start_new_session()
    assert registry.cancel_all() == 2
    await asyncio.gather(
        workspace_a.session._current,
        workspace_b.session._current,
        return_exceptions=True,
    )
    assert not workspace_a.session.busy and not workspace_b.session.busy


async def test_project_switch_fails_old_context_gate_approval(tmp_path: Path) -> None:
    registry, connections, projects = await _registry(tmp_path)
    project_id = await projects.store.create(name="Replacement")
    workspace = await registry.attach(
        connections.register(_Socket(), owner_session="same-browser"),
        owner_session="same-browser",
    )
    approvals = ApprovalManager(connections)
    workspace.on_context_replaced = approvals.fail_context
    old_context = workspace.context
    with bind_execution_context(old_context):
        pending = asyncio.create_task(
            approvals.request(
                ToolCall("call-a", "write_file", {"path": "old-context.txt"}),
                Decision(Permission.ASK, "confirm"),
                kind="turn",
                title=None,
                on_always=lambda: None,
            )
        )
    await asyncio.sleep(0)
    assert approvals.pending_for(old_context)

    await workspace.select_project(project_id)

    assert await pending is Permission.DENY
    assert not approvals.pending_for(old_context)


async def test_context_replacement_blocks_active_run_and_archived_resume(tmp_path: Path) -> None:
    registry, connections, projects = await _registry(tmp_path)
    project_id = await projects.store.create(name="Run Project")
    workspace_a = await registry.attach(
        connections.register(_Socket(), owner_session="same-browser"),
        owner_session="same-browser",
    )
    await workspace_a.select_project(project_id)
    blocked_context = workspace_a.context
    workspace_a.context_busy = lambda context: context == blocked_context
    with pytest.raises(RuntimeError, match="busy"):
        await workspace_a.start_new_session(None)

    workspace_a.context_busy = None
    await workspace_a.session.sessions.save_messages(
        blocked_context.session_id,
        [{"role": "user", "content": "archived"}],
    )
    await workspace_a.session.sessions.set_archived(blocked_context.session_id, True)
    workspace_b = await registry.attach(
        connections.register(_Socket(), owner_session="same-browser"),
        owner_session="same-browser",
    )
    assert not await workspace_b.resume(blocked_context.session_id)


async def test_resume_moves_session_and_project_together(tmp_path: Path) -> None:
    registry, connections, projects = await _registry(tmp_path)
    project_a = await projects.store.create(name="Project A")
    project_b = await projects.store.create(name="Project B")
    workspace = await registry.attach(
        connections.register(_Socket(), owner_session="same-browser"),
        owner_session="same-browser",
    )
    await workspace.select_project(project_a)
    target = await workspace.session.sessions.create_session(project_id=project_b)
    await workspace.session.sessions.save_messages(
        target, [{"role": "user", "content": "project B only"}]
    )

    assert await workspace.resume(target)
    assert workspace.context == ExecutionContext(session_id=target, project_id=project_b)
    assert workspace.session.project_id == project_b


async def test_voice_activity_blocks_context_replacement(tmp_path: Path) -> None:
    registry, connections, projects = await _registry(tmp_path)
    project_id = await projects.store.create(name="Voice Project")
    workspace = await registry.attach(
        connections.register(_Socket(), owner_session="same-browser"),
        owner_session="same-browser",
    )

    async with registry.voice_activity(workspace):
        assert workspace.attended_busy
        with pytest.raises(RuntimeError, match="busy"):
            await workspace.select_project(project_id)

    await workspace.select_project(project_id)
    assert workspace.context.project_id == project_id


async def test_two_websockets_receive_distinct_server_owned_workspace_contexts(
    tmp_path: Path,
) -> None:
    """The WS/HTTP seam never accepts a browser-provided session or project selector."""
    registry, connections, projects = await _registry(tmp_path)
    config = load_config(root=tmp_path, env_file=None)
    auth = AuthManager(token="tok")
    app = create_app(config, auth=auth, connections=connections)
    app.state.projects = projects
    # Both the workspace UiSession and runner status use this same store.
    sample_workspace = await registry.attach(
        connections.register(_Socket(), owner_session="setup"), owner_session="setup"
    )
    app.state.services = UiServices(sessions=sample_workspace.session.sessions)
    task_recorder = _TaskRecorder()
    app.state.services.tasks = task_recorder
    app.state.runner = SimpleNamespace(is_running=True, in_flight="another project's job")
    app.state.workspaces = registry
    client = TestClient(app, base_url="http://127.0.0.1")
    cookie = f"{SESSION_COOKIE}={auth.mint_session()}"
    headers = {"host": "127.0.0.1", "origin": "http://127.0.0.1", "cookie": cookie}

    with client.websocket_connect("/ws", headers=headers) as socket_a:
        assert socket_a.receive_json()["type"] == "hello"
        socket_a.send_json({"type": "hello", "surfaces": []})
        hello_a = socket_a.receive_json()
        assert hello_a["type"] == "workspace"
        with client.websocket_connect("/ws", headers=headers) as socket_b:
            assert socket_b.receive_json()["type"] == "hello"
            socket_b.send_json({"type": "hello", "surfaces": []})
            hello_b = socket_b.receive_json()
            assert hello_b["type"] == "workspace"
            assert hello_a["workspace_id"] != hello_b["workspace_id"]
            response_a = client.get(
                "/api/runner", headers={"cookie": cookie, WORKSPACE_HEADER: hello_a["workspace_id"]}
            )
            response_b = client.get(
                "/api/runner", headers={"cookie": cookie, WORKSPACE_HEADER: hello_b["workspace_id"]}
            )
            assert response_a.status_code == response_b.status_code == 200
            assert response_a.json()["session_id"] != response_b.json()["session_id"]
            assert response_a.json()["in_flight"] is None
            assert response_b.json()["in_flight"] is None

            project_a = await projects.store.create(name="Socket A only")
            switched = client.post(
                "/api/projects/select",
                json={"project_id": project_a},
                headers={
                    "cookie": cookie,
                    "origin": "http://127.0.0.1",
                    WORKSPACE_HEADER: hello_a["workspace_id"],
                },
            )
            assert switched.status_code == 200 and switched.json()["active_project_id"] == project_a
            changed = socket_a.receive_json()
            assert changed["kind"] == "project_changed"
            assert changed["project_id"] == project_a
            assert changed["workspace_id"] == hello_a["workspace_id"]
            after_a = client.get(
                "/api/runner", headers={"cookie": cookie, WORKSPACE_HEADER: hello_a["workspace_id"]}
            ).json()
            after_b = client.get(
                "/api/runner", headers={"cookie": cookie, WORKSPACE_HEADER: hello_b["workspace_id"]}
            ).json()
            assert after_a["project"]["id"] == project_a
            assert after_b["project"]["id"] is None

            # A different live workspace may not inspect or mutate Project A's transcript just
            # by knowing its numeric id. Resume below is the sole deliberate cross-project path.
            foreign_get = client.get(
                f"/api/sessions/{after_a['session_id']}",
                headers={"cookie": cookie, WORKSPACE_HEADER: hello_b["workspace_id"]},
            )
            assert foreign_get.status_code == 404
            foreign_pin = client.post(
                f"/api/sessions/{after_a['session_id']}/pin",
                json={"pinned": True},
                headers={
                    "cookie": cookie,
                    "origin": "http://127.0.0.1",
                    WORKSPACE_HEADER: hello_b["workspace_id"],
                },
            )
            assert foreign_pin.status_code == 404

            for workspace_id in (hello_a["workspace_id"], hello_b["workspace_id"]):
                created = client.post(
                    "/api/tasks/create",
                    json={"title": "scoped", "schedule_spec": "2099-01-01T00:00:00Z"},
                    headers={
                        "cookie": cookie,
                        "origin": "http://127.0.0.1",
                        WORKSPACE_HEADER: workspace_id,
                    },
                )
                assert created.status_code == 200
            assert task_recorder.project_ids == [project_a, None]

            # A second live tab may deliberately resume the same persisted chat. Archiving that
            # row must transition every bound workspace, never leave B saving into an archived
            # session behind A's back.
            await sample_workspace.session.sessions.save_messages(
                after_a["session_id"],
                [{"role": "user", "content": "shared chat"}],
            )
            resumed_b = client.post(
                f"/api/sessions/{after_a['session_id']}/resume",
                json={},
                headers={
                    "cookie": cookie,
                    "origin": "http://127.0.0.1",
                    WORKSPACE_HEADER: hello_b["workspace_id"],
                },
            )
            assert resumed_b.status_code == 200 and resumed_b.json()["ok"] is True
            resumed_event = socket_b.receive_json()
            assert resumed_event["kind"] == "session_resumed"
            assert resumed_event["session_id"] == after_a["session_id"]

            archived = client.post(
                f"/api/sessions/{after_a['session_id']}/archive",
                json={"archived": True},
                headers={
                    "cookie": cookie,
                    "origin": "http://127.0.0.1",
                    WORKSPACE_HEADER: hello_a["workspace_id"],
                },
            )
            assert archived.status_code == 200 and archived.json()["ok"] is True
            replacement = socket_a.receive_json()
            assert replacement["kind"] == "session_new"
            assert replacement["session_id"] != after_a["session_id"]
            assert replacement["project_id"] == project_a
            replacement_b = socket_b.receive_json()
            assert replacement_b["kind"] == "session_new"
            assert replacement_b["session_id"] != after_a["session_id"]
            assert replacement_b["session_id"] != replacement["session_id"]
            assert replacement_b["project_id"] == project_a
