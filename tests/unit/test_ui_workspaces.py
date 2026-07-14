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

from jarvis.actions.intents import IntentStore
from jarvis.actions.journal import ConnectorWriteJournal
from jarvis.agents import AgentRunStore
from jarvis.attention.store import AttentionKind, AttentionStore
from jarvis.config import load_config
from jarvis.core.client import ToolCall
from jarvis.core.events import TextDelta
from jarvis.core.execution import (
    ExecutionContext,
    bind_execution_context,
    current_execution_context,
)
from jarvis.graph import GraphStore
from jarvis.memory.store import MemoryStore
from jarvis.permissions.gate import Decision
from jarvis.persistence import SessionStore
from jarvis.persistence.db import connect
from jarvis.projects import ProjectService, ProjectStore
from jarvis.scheduler.service import TaskService
from jarvis.scheduler.store import TaskStore
from jarvis.tools import Permission
from jarvis.ui.approver import ApprovalManager
from jarvis.ui.auth import SESSION_COOKIE, AuthManager
from jarvis.ui.connections import ConnectionManager
from jarvis.ui.notices import NoticeBoard
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
        # The real AgentLoop exposes the registered tool names to capability_truth. Keep the
        # workspace double structurally compatible so route tests can prove per-workspace truth.
        self.registry = SimpleNamespace(names=lambda: [])

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


async def test_project_metadata_update_refreshes_live_contexts_without_switching_sessions(
    tmp_path: Path,
) -> None:
    registry, connections, projects = await _registry(tmp_path)
    project_id = await projects.store.create(name="Before", description="old context")
    await projects.activate(project_id)  # legacy process context is refreshed too
    auth = AuthManager(token="tok")
    owner_a, owner_b = auth.mint_session(), auth.mint_session()
    workspace_a = await registry.attach(
        connections.register(_Socket(), owner_session=owner_a), owner_session=owner_a
    )
    workspace_b = await registry.attach(
        connections.register(_Socket(), owner_session=owner_b), owner_session=owner_b
    )
    await workspace_a.select_project(project_id)
    await workspace_b.select_project(project_id)
    session_a, session_b = workspace_a.context.session_id, workspace_b.context.session_id

    config = load_config(root=tmp_path, env_file=None)
    app = create_app(config, auth=auth, connections=connections)
    app.state.projects = projects
    app.state.workspaces = registry
    client = TestClient(app, base_url="http://127.0.0.1")
    headers = {
        "cookie": f"{SESSION_COOKIE}={owner_a}",
        WORKSPACE_HEADER: workspace_a.workspace_id,
        "origin": "http://127.0.0.1",
    }
    updated = client.post(
        f"/api/projects/{project_id}/update",
        json={"name": "After", "description": "fresh context"},
        headers=headers,
    )
    assert updated.status_code == 200 and updated.json()["ok"] is True
    assert (workspace_a.context.session_id, workspace_b.context.session_id) == (
        session_a,
        session_b,
    )
    assert workspace_a.project.name == workspace_b.project.name == "After"
    with bind_execution_context(workspace_a.context):
        assert "After" in projects.current().system_extra
        assert "fresh context" in projects.current().system_extra
    assert projects.current().name == "After"

    missing_workspace = client.post(
        f"/api/projects/{project_id}/update",
        json={"name": "No workspace"},
        headers={"cookie": f"{SESSION_COOKIE}={owner_a}", "origin": "http://127.0.0.1"},
    )
    assert missing_workspace.status_code == 409


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


async def test_revoked_owner_drop_cancels_and_forgets_only_owned_workspaces(
    tmp_path: Path,
) -> None:
    replaced: list[ExecutionContext] = []
    registry, connections, _projects = await _registry(
        tmp_path, on_context_replaced=replaced.append
    )
    conn_a1 = connections.register(_Socket(), owner_session="owner-a")
    conn_a2 = connections.register(_Socket(), owner_session="owner-a")
    conn_b = connections.register(_Socket(), owner_session="owner-b")
    workspace_a1 = await registry.attach(conn_a1, owner_session="owner-a")
    workspace_a2 = await registry.attach(conn_a2, owner_session="owner-a")
    workspace_b = await registry.attach(conn_b, owner_session="owner-b")
    workspace_a1.session._current = asyncio.create_task(asyncio.Event().wait())
    workspace_a2.session._current = asyncio.create_task(asyncio.Event().wait())

    assert registry.drop_owner_session("owner-a") == 2
    await asyncio.gather(
        workspace_a1.session._current,
        workspace_a2.session._current,
        return_exceptions=True,
    )
    assert registry.resolve(
        owner_session="owner-a", workspace_id=workspace_a1.workspace_id
    ) is None
    assert (
        registry.resolve(owner_session="owner-b", workspace_id=workspace_b.workspace_id)
        is workspace_b
    )
    assert {context.session_id for context in replaced} == {
        workspace_a1.context.session_id,
        workspace_a2.context.session_id,
    }

    assert registry.drop_all() == 1
    assert registry.resolve(
        owner_session="owner-b", workspace_id=workspace_b.workspace_id
    ) is None


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


async def test_workspace_read_models_use_the_live_workspace_scope(tmp_path: Path) -> None:
    """Workspace-only reads cannot be selected by a guessed project or task id.

    This also pins the capability-truth promise: each surface must use the live workspace loop's
    actual tool registry rather than the process-wide/ambient session.
    """
    registry, connections, projects = await _registry(tmp_path)
    config = load_config(root=tmp_path, env_file=None)
    auth = AuthManager(token="tok")
    owner_a, owner_b = auth.mint_session(), auth.mint_session()
    workspace_a = await registry.attach(
        connections.register(_Socket(), owner_session=owner_a), owner_session=owner_a
    )
    workspace_b = await registry.attach(
        connections.register(_Socket(), owner_session=owner_b), owner_session=owner_b
    )
    project_a = await projects.store.create(name="Project A")
    project_b = await projects.store.create(name="Project B")
    await workspace_a.select_project(project_a)
    registry.refresh_context(workspace_a)
    await workspace_b.select_project(project_b)
    registry.refresh_context(workspace_b)
    workspace_a.session.loop.registry = SimpleNamespace(names=lambda: ["drive_search"])

    tasks = TaskService(
        TaskStore(workspace_a.session.sessions.db, workspace_a.session.sessions.lock),
        config.scheduler,
    )
    own_task = await tasks.schedule(
        kind="reminder",
        title="Project A only",
        payload="x",
        schedule_kind="once",
        schedule_spec="2099-01-01T00:00:00+00:00",
        created_by="user",
        project_id=project_a,
    )
    global_task = await tasks.schedule(
        kind="reminder",
        title="Global task",
        payload="x",
        schedule_kind="once",
        schedule_spec="2099-01-01T00:00:00+00:00",
        created_by="user",
        project_id=None,
    )
    graph = GraphStore(workspace_a.session.sessions.db, workspace_a.session.sessions.lock)
    memory_store = MemoryStore(workspace_a.session.sessions.db)
    remembered: list[tuple[str, str, int | None]] = []

    async def remember(content: str, mem_type: str, *, source: str, project_id: int | None):
        remembered.append((content, mem_type, project_id))
        return SimpleNamespace(memory_id=len(remembered), action="inserted")

    memory = SimpleNamespace(store=memory_store, remember=remember)
    intents = IntentStore(workspace_a.session.sessions.db, workspace_a.session.sessions.lock)
    own_intent = await intents.create_draft(
        idempotency_key="project-a-intent",
        provider="google",
        kind="calendar_create",
        request={},
        summary="Project A draft",
        source="agent",
        project_id=project_a,
    )
    foreign_intent = await intents.create_draft(
        idempotency_key="project-b-intent",
        provider="google",
        kind="calendar_create",
        request={},
        summary="Project B draft",
        source="agent",
        project_id=project_b,
    )
    await intents.mark_previewed(
        own_intent,
        preview={"title": "A", "fields": [], "diff": [], "notes": [], "warnings": []},
    )
    await intents.mark_previewed(
        foreign_intent,
        preview={"title": "B", "fields": [], "diff": [], "notes": [], "warnings": []},
    )
    attention = AttentionStore(workspace_a.session.sessions.db, workspace_a.session.sessions.lock)
    own_attention = await attention.create(
        kind=AttentionKind.PROPOSAL,
        source="dreaming",
        title="Project A proposal",
        project_id=project_a,
    )
    foreign_attention = await attention.create(
        kind=AttentionKind.PROPOSAL,
        source="dreaming",
        title="Project B proposal",
        project_id=project_b,
    )
    journal = ConnectorWriteJournal(
        workspace_a.session.sessions.db, workspace_a.session.sessions.lock
    )
    own_write = await journal.record(
        provider="google", verb="calendar_create", status="executed", project_id=project_a
    )
    foreign_write = await journal.record(
        provider="google", verb="calendar_create", status="executed", project_id=project_b
    )
    own_suggestion = await graph.add_suggestion(
        kind="memory",
        payload={"content": "Project A only"},
        trust_class="model_generated",
        project_id=project_a,
    )
    foreign_suggestion = await graph.add_suggestion(
        kind="memory",
        payload={"content": "Project B only"},
        trust_class="model_generated",
        project_id=project_b,
    )
    global_suggestion = await graph.add_suggestion(
        kind="memory",
        payload={"content": "Global review"},
        trust_class="model_generated",
        project_id=None,
    )
    connectors = SimpleNamespace(
        status=lambda: {
            "demo": False,
            "google": {"connected": True, "needs_reconnect": False},
            "notifiers": {},
        }
    )

    async def list_artifacts(**_kwargs):
        return []

    app = create_app(config, auth=auth, connections=connections)
    app.state.projects = projects
    app.state.services = UiServices(
        sessions=workspace_a.session.sessions,
        tasks=tasks,
        memory=memory,
        intents=intents,
        attention=attention,
        projects=projects,
        connectors=connectors,
        graph=graph,
        write_journal=journal,
        artifacts=SimpleNamespace(db=workspace_a.session.sessions.db, list=list_artifacts),
    )
    app.state.workspaces = registry
    app.state.notices = NoticeBoard(now=lambda: "t")
    app.state.notices.post("Project A scheduler payload", kind="task", project_id=project_a)
    app.state.notices.post("Project B scheduler payload", kind="task", project_id=project_b)
    client = TestClient(app, base_url="http://127.0.0.1")

    def headers(owner: str, workspace_id: str) -> dict[str, str]:
        return {
            "cookie": f"{SESSION_COOKIE}={owner}",
            WORKSPACE_HEADER: workspace_id,
        }

    own = headers(owner_a, workspace_a.workspace_id)
    foreign = headers(owner_b, workspace_b.workspace_id)

    for path in (
        f"/api/workspace/{project_a}",
        f"/api/workspace/{project_a}/activity",
        f"/api/tasks/{own_task.id}/runs",
    ):
        assert client.get(path, headers=own).status_code == 200
        assert client.get(path, headers=foreign).status_code == 404
    # Global tasks deliberately remain visible from either workspace, just like the scoped task
    # list. The guard is project isolation, not an accidental removal of global reminders.
    assert client.get(f"/api/tasks/{global_task.id}/runs", headers=foreign).status_code == 200
    assert client.get(f"/api/tasks?project_id={project_a}", headers=foreign).status_code == 404
    assert client.get(f"/api/memory?project_id={project_a}", headers=foreign).status_code == 404
    assert client.get(
        "/api/search", params={"q": "project-a", "project_id": project_a}, headers=foreign
    ).status_code == 404
    assert [row["text"] for row in client.get("/api/notices", headers=own).json()["notices"]] == [
        "Project A scheduler payload"
    ]
    foreign_notices = client.get("/api/notices", headers=foreign).json()["notices"]
    assert [row["text"] for row in foreign_notices] == ["Project B scheduler payload"]
    assert [row["text"] for row in client.get("/api/daily", headers=own).json()["notices"]] == [
        "Project A scheduler payload"
    ]
    assert [
        row["text"] for row in client.get("/api/daily", headers=foreign).json()["notices"]
    ] == ["Project B scheduler payload"]
    digest_response = client.post(
        "/api/digest/run", headers={**own, "origin": "http://127.0.0.1"}
    )
    assert digest_response.status_code == 409
    # Task ids are not authority either: foreign project tasks cannot be cancelled by guessing
    # an id, while global reminders remain a deliberate shared workspace surface.
    foreign_post = {**foreign, "origin": "http://127.0.0.1"}
    assert client.post(f"/api/tasks/{own_task.id}/cancel", headers=foreign_post).status_code == 404
    assert (await tasks.store.get(own_task.id)).status != "cancelled"
    own_post = {**own, "origin": "http://127.0.0.1"}
    assert client.post(f"/api/tasks/{own_task.id}/cancel", headers=own_post).json()["ok"] is True
    assert (await tasks.store.get(own_task.id)).status == "cancelled"

    # Project metadata is a mutable capability too: a Project A workspace cannot rename or
    # rewrite project details for B merely by guessing its numeric id.
    foreign_update = client.post(
        f"/api/projects/{project_b}/update", json={"name": "hijacked"}, headers=own_post
    )
    assert foreign_update.status_code == 404
    assert (await projects.store.get(project_b)).name == "Project B"

    # Memory deletion must obey the same P + global boundary. A foreign id cannot become a
    # capability merely because its numeric value was observed elsewhere in the UI.
    own_memory = await memory.store.add(
        type="fact", content="A only", embedding=[0.1, 0.2], embedding_model="fake", source="user",
        project_id=project_a,
    )
    foreign_memory = await memory.store.add(
        type="fact", content="B only", embedding=[0.3, 0.4], embedding_model="fake", source="user",
        project_id=project_b,
    )
    assert client.post(f"/api/memory/{foreign_memory}/forget", headers=own_post).status_code == 404
    assert (await memory.store.get(foreign_memory)).status == "live"
    assert client.post(f"/api/memory/{own_memory}/forget", headers=own_post).json()["ok"] is True
    assert (await memory.store.get(own_memory)).status == "forgotten"

    # Gate detail, queue, and every mutation route must share the live workspace boundary; a
    # numeric intent/attention id from Project B never becomes read or execution authority in A.
    assert client.get("/api/intents", headers=own).json()["pending"][0]["id"] == own_intent
    assert client.get(
        "/api/intents", params={"project_id": project_b}, headers=own
    ).status_code == 404
    for suffix in ("", "/approve", "/reject", "/undo"):
        method = client.get if not suffix else client.post
        kwargs = {"headers": own if not suffix else own_post}
        assert method(f"/api/intents/{foreign_intent}{suffix}", **kwargs).status_code == 404
    assert (await intents.get(foreign_intent)).state.value == "previewed"
    assert client.post(
        f"/api/attention/{foreign_attention}/resolve", json={"action": "dismiss"}, headers=own_post
    ).status_code == 404
    assert (await attention.get(foreign_attention)).state.value == "open"
    assert client.post(
        f"/api/attention/{own_attention}/resolve", json={"action": "dismiss"}, headers=own_post
    ).json()["ok"] is True

    # Connector-write audit rows follow the same server-owned workspace scope. The browser has
    # no project selector and no remote/rollback handles to turn a numeric id into authority.
    own_audit = client.get("/api/connector-writes", headers=own).json()["writes"]
    foreign_audit = client.get("/api/connector-writes", headers=foreign).json()["writes"]
    assert [row["id"] for row in own_audit] == [own_write]
    assert [row["id"] for row in foreign_audit] == [foreign_write]

    # Quarantined graph-suggestion review mutations use the same P + global workspace scope as
    # their review queue. A guessed numeric id from Project B must never resolve its proposal.
    own_post = {**own, "origin": "http://127.0.0.1"}
    assert client.post(
        f"/api/graph/suggestions/{foreign_suggestion}/approve", headers=own_post
    ).status_code == 404
    assert client.post(
        f"/api/graph/suggestions/{foreign_suggestion}/reject", headers=own_post
    ).status_code == 404
    assert (await graph.get_suggestion(foreign_suggestion)).status == "pending"
    assert client.get(
        f"/api/graph/suggestions?project_id={project_a}", headers=foreign
    ).status_code == 404
    assert client.post(
        f"/api/graph/suggestions/{own_suggestion}/approve", headers=own_post
    ).json()["ok"] is True
    assert client.post(
        f"/api/graph/suggestions/{global_suggestion}/reject", headers=own_post
    ).json()["ok"] is True

    def drive_exposed(payload: dict) -> bool:
        row = next(
            row
            for row in payload["capabilities"]["connectors"]
            if row["name"] == "Google Drive"
        )
        return row["exposed_to_chat"]

    for path in ("/api/capabilities", "/api/daily", "/api/hub", "/api/settings"):
        own_payload = client.get(path, headers=own).json()
        foreign_payload = client.get(path, headers=foreign).json()
        own_caps = own_payload if path == "/api/capabilities" else own_payload["capabilities"]
        foreign_caps = (
            foreign_payload
            if path == "/api/capabilities"
            else foreign_payload["capabilities"]
        )
        assert drive_exposed({"capabilities": own_caps}) is True, path
        assert drive_exposed({"capabilities": foreign_caps}) is False, path

    # A human-review draft binds to the exact live context it opened in. If another duplicated
    # tab switches this shared workspace before Save, the stale assertion fails closed rather
    # than retagging reviewed Project A content as Project B durable memory.
    expected_context = {
        "session_id": workspace_a.context.session_id,
        "project_id": workspace_a.context.project_id,
    }
    scheduled = client.post(
        "/api/tasks/create",
        json={
            "title": "A reviewed task",
            "schedule_spec": "2099-01-01T00:00:00Z",
            "expected_context": expected_context,
        },
        headers=own_post,
    )
    assert scheduled.status_code == 200
    task_count = len(await tasks.store.list(include_finished=True))
    saved = client.post(
        "/api/memory/remember",
        json={"content": "A reviewed fact", "type": "fact", "expected_context": expected_context},
        headers=own_post,
    )
    assert saved.status_code == 200 and remembered == [("A reviewed fact", "fact", project_a)]
    switched = client.post(
        "/api/projects/select", json={"project_id": project_b}, headers=own_post
    )
    assert switched.status_code == 200
    stale = client.post(
        "/api/memory/remember",
        json={"content": "A stale fact", "type": "fact", "expected_context": expected_context},
        headers=own_post,
    )
    assert stale.status_code == 409 and remembered == [("A reviewed fact", "fact", project_a)]
    stale_task = client.post(
        "/api/tasks/create",
        json={
            "title": "A stale task",
            "schedule_spec": "2099-01-01T00:00:00Z",
            "expected_context": expected_context,
        },
        headers=own_post,
    )
    assert stale_task.status_code == 409
    assert len(await tasks.store.list(include_finished=True)) == task_count

    # The global workspace is deliberately the administrative aggregate used by the Tasks
    # screen. It may inspect and cancel an existing project task; only a project workspace is
    # restricted to its own rows plus explicit global reminders.
    admin_task = await tasks.schedule(
        kind="reminder",
        title="Project A admin task",
        payload="review",
        schedule_kind="once",
        schedule_spec="2099-01-01T00:00:00Z",
        created_by="user",
        project_id=project_a,
    )
    global_scope = client.post(
        "/api/projects/select", json={"project_id": None}, headers=own_post
    )
    assert global_scope.status_code == 200
    assert client.get(f"/api/tasks/{admin_task.id}/runs", headers=own).status_code == 200
    assert client.post(f"/api/tasks/{admin_task.id}/cancel", headers=own_post).json()["ok"] is True


async def test_delegation_history_is_scoped_by_the_live_workspace(tmp_path: Path) -> None:
    registry, connections, projects = await _registry(tmp_path)
    project_a = await projects.store.create(name="Project A")
    project_b = await projects.store.create(name="Project B")
    auth = AuthManager(token="tok")
    owner_a, owner_b, owner_global = auth.mint_session(), auth.mint_session(), auth.mint_session()
    workspace_a = await registry.attach(
        connections.register(_Socket(), owner_session=owner_a), owner_session=owner_a
    )
    workspace_b = await registry.attach(
        connections.register(_Socket(), owner_session=owner_b), owner_session=owner_b
    )
    global_workspace = await registry.attach(
        connections.register(_Socket(), owner_session=owner_global), owner_session=owner_global
    )
    await workspace_a.select_project(project_a)
    await workspace_b.select_project(project_b)
    registry.refresh_context(workspace_a)
    registry.refresh_context(workspace_b)
    run_store = AgentRunStore(workspace_a.session.sessions.db, workspace_a.session.sessions.lock)
    run_a = await run_store.begin_run(
        parent_session_id=None,
        parent_trace_id="parent-a",
        title="A delegation",
        prompt="PRIVATE-A-PROMPT",
        tools_scope=["read_file"],
        project_id=project_a,
    )
    await run_store.begin_run(
        parent_session_id=None,
        parent_trace_id="parent-b",
        title="B delegation",
        prompt="PRIVATE-B-PROMPT",
        tools_scope=["web_search"],
        project_id=project_b,
    )
    await run_store.begin_run(
        parent_session_id=None,
        parent_trace_id="parent-global",
        title="Global delegation",
        prompt="GLOBAL-PROMPT",
        tools_scope=[],
    )

    app = create_app(load_config(root=tmp_path, env_file=None), auth=auth, connections=connections)
    app.state.projects = projects
    app.state.services = UiServices(sessions=workspace_a.session.sessions, run_store=run_store)
    app.state.workspaces = registry
    client = TestClient(app, base_url="http://127.0.0.1")

    def headers(owner: str, workspace_id: str) -> dict[str, str]:
        return {"cookie": f"{SESSION_COOKIE}={owner}", WORKSPACE_HEADER: workspace_id}

    a_rows = client.get("/api/agents", headers=headers(owner_a, workspace_a.workspace_id))
    b_rows = client.get("/api/agents", headers=headers(owner_b, workspace_b.workspace_id))
    global_rows = client.get(
        "/api/agents", headers=headers(owner_global, global_workspace.workspace_id)
    )
    assert a_rows.status_code == b_rows.status_code == global_rows.status_code == 200
    assert [row["id"] for row in a_rows.json()] == [run_a]
    assert [row["title"] for row in b_rows.json()] == ["B delegation"]
    assert [row["title"] for row in global_rows.json()] == [
        "Global delegation",
        "B delegation",
        "A delegation",
    ]
    assert set(a_rows.json()[0]) == {
        "id",
        "title",
        "status",
        "tools_scope",
        "iterations",
        "denied_count",
        "cost_usd",
        "started_at",
    }
    assert "PRIVATE-A-PROMPT" not in a_rows.text and "parent-a" not in a_rows.text
    missing_workspace = client.get(
        "/api/agents", headers={"cookie": f"{SESSION_COOKIE}={owner_a}"}
    )
    assert missing_workspace.status_code == 409


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
            await sample_workspace.session.sessions.save_messages(
                after_a["session_id"],
                [{"role": "user", "content": "shared chat"}],
            )

            # The project graph is as private as a chat transcript.  A tab in another live
            # workspace cannot inspect it merely by putting Project A's numeric id in a GET URL.
            graph_a = client.get(
                f"/api/workspace/{project_a}/graph",
                headers={"cookie": cookie, WORKSPACE_HEADER: hello_a["workspace_id"]},
            )
            graph_b = client.get(
                f"/api/workspace/{project_a}/graph",
                headers={"cookie": cookie, WORKSPACE_HEADER: hello_b["workspace_id"]},
            )
            assert graph_a.status_code == 200
            assert graph_b.status_code == 404

            # Graph cards are scoped too.  A derived folder label is not a capability just
            # because another workspace can guess its project-qualified ref.
            graph_store = GraphStore(
                sample_workspace.session.sessions.db, sample_workspace.session.sessions.lock
            )
            await graph_store.upsert_edge(
                src_kind="project", src_id=str(project_a), dst_kind="folder",
                dst_id=f"{project_a}:private", edge_kind="contains", origin="derived",
                trust_class="trusted_local", created_by="system",
                created_at="2026-01-01T00:00:00+00:00",
                project_id=project_a,
            )
            app.state.services.graph = graph_store
            own_folder = client.get(
                f"/api/graph/node/folder/{project_a}:private",
                headers={"cookie": cookie, WORKSPACE_HEADER: hello_a["workspace_id"]},
            )
            foreign_folder = client.get(
                f"/api/graph/node/folder/{project_a}:private",
                headers={"cookie": cookie, WORKSPACE_HEADER: hello_b["workspace_id"]},
            )
            assert own_folder.status_code == 200 and own_folder.json()["label"] == "private"
            assert foreign_folder.status_code == 404

            # History titles are scoped too: a global/other-project workspace cannot enumerate
            # Project A's chat list just by calling the collection route or adding a query string.
            history_a = client.get(
                "/api/sessions?limit=50",
                headers={"cookie": cookie, WORKSPACE_HEADER: hello_a["workspace_id"]},
            )
            history_b = client.get(
                "/api/sessions?limit=50",
                headers={"cookie": cookie, WORKSPACE_HEADER: hello_b["workspace_id"]},
            )
            assert history_a.status_code == history_b.status_code == 200
            assert after_a["session_id"] in {row["id"] for row in history_a.json()["sessions"]}
            assert after_a["session_id"] not in {row["id"] for row in history_b.json()["sessions"]}
            foreign_history = client.get(
                f"/api/sessions?project_id={project_a}",
                headers={"cookie": cookie, WORKSPACE_HEADER: hello_b["workspace_id"]},
            )
            assert foreign_history.status_code == 404

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
                runner = client.get(
                    "/api/runner", headers={"cookie": cookie, WORKSPACE_HEADER: workspace_id}
                ).json()
                created = client.post(
                    "/api/tasks/create",
                    json={
                        "title": "scoped",
                        "schedule_spec": "2099-01-01T00:00:00Z",
                        "expected_context": {
                            "session_id": runner["session_id"],
                            "project_id": runner["project"]["id"],
                        },
                    },
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
