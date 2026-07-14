"""Phase 16.5 browser-workspace isolation tests.

These use two simultaneous live socket doubles under the same authenticated UI cookie.  The
assertions prove that server-owned workspace binding, not a browser-provided project/session id,
chooses the delivery recipient.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from jarvis.actions.intents import IntentStore
from jarvis.actions.journal import ConnectorWriteJournal
from jarvis.agents import AgentRunStore
from jarvis.attention.store import AttentionKind, AttentionStore
from jarvis.config import KnowledgeConfig, load_config
from jarvis.core.client import ToolCall
from jarvis.core.events import TextDelta
from jarvis.core.execution import (
    ExecutionContext,
    bind_execution_context,
    current_execution_context,
)
from jarvis.graph import GraphStore
from jarvis.knowledge.service import KnowledgeService
from jarvis.knowledge.store import KnowledgeStore
from jarvis.memory.embeddings import FakeEmbedder
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
from jarvis.ui.server import (
    EXPECTED_CONTEXT_REVISION_HEADER,
    EXPECTED_PROJECT_HEADER,
    EXPECTED_SESSION_HEADER,
    LEGACY_EXPECTED_CONTEXT_REVISION_HEADER,
    LEGACY_EXPECTED_PROJECT_HEADER,
    LEGACY_EXPECTED_SESSION_HEADER,
    LEGACY_WORKSPACE_HEADER,
    WORKSPACE_HEADER,
    create_app,
)
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
        self.cancelled_messages: list[dict] | None = None
        # The real AgentLoop exposes the registered tool names to capability_truth. Keep the
        # workspace double structurally compatible so route tests can prove per-workspace truth.
        self.registry = SimpleNamespace(names=lambda: [])

    def reset_cancellation_snapshot(self) -> None:
        self.cancelled_messages = None

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
    tmp_path: Path,
    *,
    on_context_replaced=None,
    turn_lock: asyncio.Lock | None = None,
) -> tuple[UiWorkspaceRegistry, ConnectionManager, ProjectService]:
    db = await connect(tmp_path / "workspaces.db")
    _OPEN.append(db)
    store_lock = asyncio.Lock()
    store = SessionStore(db, store_lock)
    projects = ProjectService(ProjectStore(db, store_lock))
    connections = ConnectionManager(clock=lambda: 0.0)
    loops: list[_Loop] = []

    def make_session(workspace) -> UiSession:
        loop = _Loop(f"workspace-{len(loops) + 1}")
        loops.append(loop)
        return UiSession(
            loop=loop,
            connections=connections,
            turn_lock=turn_lock,
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


def _workspace_claim(workspace) -> dict:
    return {**workspace.context.to_wire(), "context_revision": workspace.context_revision}


def _workspace_post_headers(owner: str, workspace) -> dict[str, str]:
    return {
        "cookie": f"{SESSION_COOKIE}={owner}",
        WORKSPACE_HEADER: workspace.workspace_id,
        "origin": "http://127.0.0.1",
    }


async def _seed_workspace_approval(app, workspace, conn):
    with bind_execution_context(workspace.context):
        resolution = asyncio.create_task(
            app.state.approvals.request(
                ToolCall("lock-order-call", "write_file", {"path": "review.txt"}),
                Decision(Permission.ASK, "confirm"),
                kind="turn",
                title=None,
                on_always=lambda: None,
            )
        )
    await asyncio.sleep(0)
    (pending,) = app.state.approvals.pending_for(workspace.context)
    nonce = await app.state.approvals.mint_nonce(pending.decision_id, conn)
    assert nonce is not None
    return resolution, pending.decision_id, nonce


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
        (event["session_id"], event["project_id"]) == (workspace_a.context.session_id, project_a)
        for event in a_events
    )
    assert all(event["context_revision"] == workspace_a.context_revision for event in a_events)
    # The tool project provider resolves from the task-local execution context, not the mutable
    # process active project.  This is what keeps a project-scoped tool inside A's workspace.
    assert workspace_a.session.loop.contexts == [workspace_a.context]
    assert workspace_b.session.messages == []

    await workspace_b.session.handle_text("B only")
    await asyncio.gather(*list(workspace_b.session._pushes))
    b_events = [message for message in socket_b.sent if message.get("kind") == "event"]
    assert b_events
    assert all(
        (event["session_id"], event["project_id"]) == (workspace_b.context.session_id, project_b)
        for event in b_events
    )
    assert all(event["context_revision"] == workspace_b.context_revision for event in b_events)
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


async def test_reconnect_attach_waits_for_transition_and_binds_final_revision(
    tmp_path: Path,
) -> None:
    registry, connections, _projects = await _registry(tmp_path)
    owner = "owner-a"
    workspace = await registry.attach(
        connections.register(_Socket(), owner_session=owner), owner_session=owner
    )
    reconnect = connections.register(_Socket(), owner_session=owner)

    async with registry.transition_lock:
        attaching = asyncio.create_task(
            registry.attach(
                reconnect,
                owner_session=owner,
                requested_workspace_id=workspace.workspace_id,
            )
        )
        await asyncio.sleep(0)
        assert not attaching.done()
        await workspace.start_new_session()
        registry.refresh_context(workspace)
        final_context = workspace.context
        final_revision = workspace.context_revision

    assert await attaching is workspace
    assert reconnect.context == final_context
    assert reconnect.context_revision == final_revision


async def test_fresh_attach_does_not_hold_transition_lock_while_waiting_for_turn_lock(
    tmp_path: Path, monkeypatch
) -> None:
    shared_turn_lock = asyncio.Lock()
    registry, connections, _projects = await _registry(tmp_path, turn_lock=shared_turn_lock)
    owner = "owner-a"
    conn = connections.register(_Socket(), owner_session=owner)
    waiting_for_turn = asyncio.Event()
    original_ensure = UiSession.ensure_session

    async def observed_ensure(session: UiSession):
        waiting_for_turn.set()
        return await original_ensure(session)

    monkeypatch.setattr(UiSession, "ensure_session", observed_ensure)
    await shared_turn_lock.acquire()
    attaching = asyncio.create_task(registry.attach(conn, owner_session=owner))
    await asyncio.wait_for(waiting_for_turn.wait(), timeout=1)

    transition_waiter = asyncio.create_task(registry.transition_lock.acquire())
    done, _pending = await asyncio.wait({transition_waiter}, timeout=0.5)
    transition_was_available = transition_waiter in done
    if transition_was_available:
        registry.transition_lock.release()
    else:
        transition_waiter.cancel()
        await asyncio.gather(transition_waiter, return_exceptions=True)
    shared_turn_lock.release()
    workspace = await asyncio.wait_for(attaching, timeout=1)

    assert transition_was_available
    assert registry.resolve(owner_session=owner, workspace_id=workspace.workspace_id) is workspace


async def test_failed_new_session_allocation_preserves_workspace_authority(
    tmp_path: Path, monkeypatch
) -> None:
    invalidated: list[ExecutionContext] = []
    registry, connections, _projects = await _registry(
        tmp_path, on_context_replaced=invalidated.append
    )
    workspace = await registry.attach(
        connections.register(_Socket(), owner_session="owner-a"), owner_session="owner-a"
    )
    workspace.session.messages = [{"role": "user", "content": "keep me"}]
    before = (
        workspace.context,
        workspace.context_revision,
        workspace.project,
        list(workspace.session.messages),
    )

    async def fail_create(*, project_id=None):
        raise OSError("storage unavailable")

    monkeypatch.setattr(workspace.session.sessions, "create_session", fail_create)
    with pytest.raises(OSError, match="storage unavailable"):
        await workspace.start_new_session()

    assert workspace.context == before[0]
    assert workspace.context_revision == before[1]
    assert workspace.project == before[2]
    assert workspace.session.messages == before[3]
    assert invalidated == []


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
        EXPECTED_SESSION_HEADER: str(workspace_a.context.session_id),
        EXPECTED_PROJECT_HEADER: str(workspace_a.context.project_id),
        EXPECTED_CONTEXT_REVISION_HEADER: str(workspace_a.context_revision),
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


async def test_legacy_workspace_headers_work_but_canonical_claims_take_precedence(
    tmp_path: Path,
) -> None:
    registry, connections, projects = await _registry(tmp_path)
    project_id = await projects.store.create(name="Legacy tab")
    auth = AuthManager(token="tok")
    owner = auth.mint_session()
    workspace = await registry.attach(
        connections.register(_Socket(), owner_session=owner), owner_session=owner
    )
    await workspace.select_project(project_id)

    app = create_app(load_config(root=tmp_path, env_file=None), auth=auth, connections=connections)
    app.state.projects = projects
    app.state.workspaces = registry
    client = TestClient(app, base_url="http://127.0.0.1")

    def legacy_headers() -> dict[str, str]:
        return {
            "cookie": f"{SESSION_COOKIE}={owner}",
            "origin": "http://127.0.0.1",
            LEGACY_WORKSPACE_HEADER: workspace.workspace_id,
            LEGACY_EXPECTED_SESSION_HEADER: str(workspace.context.session_id),
            LEGACY_EXPECTED_PROJECT_HEADER: str(workspace.context.project_id),
            LEGACY_EXPECTED_CONTEXT_REVISION_HEADER: str(workspace.context_revision),
        }

    misrouted = client.post(
        f"/api/projects/{project_id}/update",
        json={"name": "Must not route"},
        headers={**legacy_headers(), WORKSPACE_HEADER: "z" * 24},
    )
    assert misrouted.status_code == 409
    assert (await projects.store.get(project_id)).name == "Legacy tab"

    updated = client.post(
        f"/api/projects/{project_id}/update",
        json={"name": "Migrated tab"},
        headers=legacy_headers(),
    )
    assert updated.status_code == 200

    conflicting = client.post(
        f"/api/projects/{project_id}/update",
        json={"name": "Must not apply"},
        headers={
            **legacy_headers(),
            WORKSPACE_HEADER: workspace.workspace_id,
            EXPECTED_SESSION_HEADER: "999999",
            EXPECTED_PROJECT_HEADER: str(workspace.context.project_id),
            EXPECTED_CONTEXT_REVISION_HEADER: str(workspace.context_revision),
        },
    )
    assert conflicting.status_code == 409
    assert (await projects.store.get(project_id)).name == "Migrated tab"


async def test_project_service_change_refreshes_every_bound_context_and_exact_socket(
    tmp_path: Path,
) -> None:
    registry, connections, projects = await _registry(tmp_path)
    project_a = await projects.store.create(name="Project A")
    project_b = await projects.store.create(name="Project B")
    await projects.activate(project_a)
    auth = AuthManager(token="tok")
    owner_a, owner_b, owner_c = auth.mint_session(), auth.mint_session(), auth.mint_session()
    socket_a, socket_a_peer, socket_b, socket_c = _Socket(), _Socket(), _Socket(), _Socket()
    workspace_a = await registry.attach(
        connections.register(socket_a, owner_session=owner_a), owner_session=owner_a
    )
    workspace_b = await registry.attach(
        connections.register(socket_b, owner_session=owner_b), owner_session=owner_b
    )
    workspace_c = await registry.attach(
        connections.register(socket_c, owner_session=owner_c), owner_session=owner_c
    )
    await workspace_a.select_project(project_a)
    await workspace_b.select_project(project_a)
    await workspace_c.select_project(project_b)
    for workspace in (workspace_a, workspace_b, workspace_c):
        registry.refresh_context(workspace)
    duplicate_a = await registry.attach(
        connections.register(socket_a_peer, owner_session=owner_a),
        owner_session=owner_a,
        requested_workspace_id=workspace_a.workspace_id,
    )
    assert duplicate_a is workspace_a

    config = load_config(root=tmp_path, env_file=None)
    config.services.enabled = ["exa", "firecrawl"]
    app = create_app(config, auth=auth, connections=connections)
    app.state.projects = projects
    app.state.workspaces = registry
    client = TestClient(app, base_url="http://127.0.0.1")
    headers = {
        **_workspace_post_headers(owner_a, workspace_a),
        EXPECTED_SESSION_HEADER: str(workspace_a.context.session_id),
        EXPECTED_PROJECT_HEADER: str(project_a),
        EXPECTED_CONTEXT_REVISION_HEADER: str(workspace_a.context_revision),
    }
    before = {
        workspace.workspace_id: (workspace.context, workspace.context_revision)
        for workspace in (workspace_a, workspace_b, workspace_c)
    }

    workspace_b.voice_active = 1
    busy = client.post(
        f"/api/projects/{project_a}/services",
        json={"services": [], "expected_services": None},
        headers=headers,
    )
    workspace_b.voice_active = 0
    assert busy.status_code == 409 and busy.json()["reason"] == "project_busy"
    assert "services" not in (await projects.store.get(project_a)).settings
    assert not any(socket.sent for socket in (socket_a, socket_a_peer, socket_b, socket_c))

    saved = client.post(
        f"/api/projects/{project_a}/services",
        json={"services": [], "expected_services": None},
        headers=headers,
    )
    assert saved.status_code == 200 and saved.json()["services"] == []
    assert workspace_a.project.services == workspace_b.project.services == ()
    assert workspace_c.project.services is None
    assert projects.current().services == ()
    with bind_execution_context(workspace_a.context):
        assert projects.current().services == ()
    with bind_execution_context(workspace_b.context):
        assert projects.current().services == ()
    with bind_execution_context(workspace_c.context):
        assert projects.current().project_id == project_b
        assert projects.current().services is None
    for workspace in (workspace_a, workspace_b, workspace_c):
        assert (workspace.context, workspace.context_revision) == before[workspace.workspace_id]

    for socket, workspace in (
        (socket_a, workspace_a),
        (socket_a_peer, workspace_a),
        (socket_b, workspace_b),
    ):
        assert socket.sent == [
            {
                "kind": "project_services_changed",
                **workspace.context.to_wire(),
                "workspace_id": workspace.workspace_id,
                "context_revision": workspace.context_revision,
            }
        ]
    assert socket_c.sent == []

    read_a = {
        "cookie": f"{SESSION_COOKIE}={owner_a}",
        WORKSPACE_HEADER: workspace_a.workspace_id,
    }
    capabilities = client.get("/api/capabilities", headers=read_a).json()
    firecrawl = next(row for row in capabilities["services"] if row["name"] == "firecrawl")
    assert firecrawl["state"] == "disabled"
    assert firecrawl["reason"] == "Not enabled for this project."
    studio = client.get("/api/studio", headers=read_a).json()
    assert studio["active_project_id"] == project_a
    assert all(
        row["state"] == "disabled"
        for row in studio["services"]
        if row["name"] in {"exa", "firecrawl"}
    )

    for socket in (socket_a, socket_a_peer, socket_b, socket_c):
        socket.sent.clear()
    cleared = client.post(
        f"/api/projects/{project_a}/services",
        json={"services": None, "expected_services": []},
        headers=headers,
    )
    assert cleared.status_code == 200 and cleared.json()["services"] is None
    assert workspace_a.project.services is workspace_b.project.services is None
    assert projects.current().services is None
    assert len(socket_a.sent) == len(socket_a_peer.sent) == len(socket_b.sent) == 1
    assert socket_c.sent == []


@pytest.mark.parametrize("transition_kind", ["new", "select", "resume"])
async def test_delayed_context_transition_reloads_latest_service_policy(
    tmp_path: Path, monkeypatch, transition_kind: str
) -> None:
    registry, connections, projects = await _registry(tmp_path)
    project_a = await projects.store.create(name="Project A")
    project_b = await projects.store.create(name="Project B")
    auth = AuthManager(token="tok")
    transition_owner, policy_owner = auth.mint_session(), auth.mint_session()
    transition_socket, policy_socket = _Socket(), _Socket()
    transition_workspace = await registry.attach(
        connections.register(transition_socket, owner_session=transition_owner),
        owner_session=transition_owner,
    )
    policy_workspace = await registry.attach(
        connections.register(policy_socket, owner_session=policy_owner),
        owner_session=policy_owner,
    )
    await transition_workspace.select_project(
        project_a if transition_kind == "new" else project_b
    )
    await policy_workspace.select_project(project_a)
    registry.refresh_context(transition_workspace)
    registry.refresh_context(policy_workspace)

    target_session = None
    if transition_kind == "resume":
        target_session = await transition_workspace.session.sessions.create_session(
            project_id=project_a
        )
        await transition_workspace.session.sessions.save_messages(
            target_session, [{"role": "user", "content": "resume Project A"}]
        )

    if transition_kind == "new":
        path = "/api/sessions/new"
        payload = {"expected_context": _workspace_claim(transition_workspace)}
        prepare_name = "prepare_new_session"
    elif transition_kind == "select":
        path = "/api/projects/select"
        payload = {
            "project_id": project_a,
            "expected_context": _workspace_claim(transition_workspace),
        }
        prepare_name = "prepare_new_session"
    else:
        path = f"/api/sessions/{target_session}/resume"
        payload = {"expected_context": _workspace_claim(transition_workspace)}
        prepare_name = "prepare_resume"

    prepared_old_policy = asyncio.Event()
    release_transition = asyncio.Event()
    original_prepare = getattr(transition_workspace, prepare_name)

    async def delayed_prepare(*args, **kwargs):
        prepared = await original_prepare(*args, **kwargs)
        assert prepared is not None and prepared.project.services is None
        prepared_old_policy.set()
        await release_transition.wait()
        return prepared

    monkeypatch.setattr(transition_workspace, prepare_name, delayed_prepare)
    config = load_config(root=tmp_path, env_file=None)
    config.services.enabled = ["exa", "firecrawl"]
    app = create_app(config, auth=auth, connections=connections)
    app.state.projects = projects
    app.state.workspaces = registry
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
        transition_request = asyncio.create_task(
            client.post(
                path,
                json=payload,
                headers=_workspace_post_headers(transition_owner, transition_workspace),
            )
        )
        await asyncio.wait_for(prepared_old_policy.wait(), timeout=1)
        policy_response = await client.post(
            f"/api/projects/{project_a}/services",
            json={
                "services": [],
                "expected_services": None,
                "expected_context": _workspace_claim(policy_workspace),
            },
            headers=_workspace_post_headers(policy_owner, policy_workspace),
        )
        assert policy_response.status_code == 200
        release_transition.set()
        transition_response = await asyncio.wait_for(transition_request, timeout=2)

    assert transition_response.status_code == 200
    assert transition_workspace.project.project_id == project_a
    assert transition_workspace.project.services == ()
    with bind_execution_context(transition_workspace.context):
        assert projects.current().services == ()


@pytest.mark.parametrize("transition_kind", ["new", "select", "resume"])
async def test_context_transition_reload_translates_busy_to_409(
    tmp_path: Path, monkeypatch, transition_kind: str
) -> None:
    registry, connections, projects = await _registry(tmp_path)
    project_a = await projects.store.create(name="Project A")
    project_b = await projects.store.create(name="Project B")
    auth = AuthManager(token="tok")
    owner = auth.mint_session()
    workspace = await registry.attach(
        connections.register(_Socket(), owner_session=owner), owner_session=owner
    )
    await workspace.select_project(project_a if transition_kind == "new" else project_b)
    registry.refresh_context(workspace)
    before = (workspace.context, workspace.context_revision, workspace.project)

    if transition_kind == "new":
        path = "/api/sessions/new"
        payload = {"expected_context": _workspace_claim(workspace)}
        refresh_name = "refresh_prepared_new_session"
    elif transition_kind == "select":
        path = "/api/projects/select"
        payload = {
            "project_id": project_a,
            "expected_context": _workspace_claim(workspace),
        }
        refresh_name = "refresh_prepared_new_session"
    else:
        target_session = await workspace.session.sessions.create_session(project_id=project_a)
        await workspace.session.sessions.save_messages(
            target_session, [{"role": "user", "content": "resume Project A"}]
        )
        path = f"/api/sessions/{target_session}/resume"
        payload = {"expected_context": _workspace_claim(workspace)}
        refresh_name = "refresh_prepared_resume"

    async def became_busy(_prepared):
        raise RuntimeError("busy")

    monkeypatch.setattr(workspace, refresh_name, became_busy)
    app = create_app(
        load_config(root=tmp_path, env_file=None), auth=auth, connections=connections
    )
    app.state.projects = projects
    app.state.workspaces = registry
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
        response = await client.post(
            path, json=payload, headers=_workspace_post_headers(owner, workspace)
        )

    assert response.status_code == 409
    assert response.json() == {"ok": False, "message": "busy"}
    assert (workspace.context, workspace.context_revision, workspace.project) == before


async def test_cancelled_service_commit_finishes_cache_publication_before_unlocking(
    tmp_path: Path, monkeypatch
) -> None:
    registry, connections, projects = await _registry(tmp_path)
    project_id = await projects.store.create(name="Project A")
    await projects.activate(project_id)
    auth = AuthManager(token="tok")
    owner, peer_owner = auth.mint_session(), auth.mint_session()
    socket, peer_socket = _Socket(), _Socket()
    workspace = await registry.attach(
        connections.register(socket, owner_session=owner), owner_session=owner
    )
    peer_workspace = await registry.attach(
        connections.register(peer_socket, owner_session=peer_owner), owner_session=peer_owner
    )
    await workspace.select_project(project_id)
    await peer_workspace.select_project(project_id)
    registry.refresh_context(workspace)
    registry.refresh_context(peer_workspace)

    config = load_config(root=tmp_path, env_file=None)
    config.services.enabled = ["exa", "firecrawl"]
    app = create_app(config, auth=auth, connections=connections)
    app.state.projects = projects
    app.state.workspaces = registry
    committed = asyncio.Event()
    release_commit = asyncio.Event()
    publication_entered = asyncio.Event()
    release_publication = asyncio.Event()
    cas_calls = 0
    apply_calls = 0
    original_cas = projects.store.compare_and_set_services_with_project
    original_apply = projects.apply_project_context
    original_publish = registry.publish_workspace

    async def delayed_cas(*args, **kwargs):
        nonlocal cas_calls
        cas_calls += 1
        result = await original_cas(*args, **kwargs)
        committed.set()
        await release_commit.wait()
        return result

    def observed_apply(project):
        nonlocal apply_calls
        apply_calls += 1
        return original_apply(project)

    async def delayed_publish(*args, **kwargs):
        publication_entered.set()
        await release_publication.wait()
        await original_publish(*args, **kwargs)

    monkeypatch.setattr(projects.store, "compare_and_set_services_with_project", delayed_cas)
    monkeypatch.setattr(projects, "apply_project_context", observed_apply)
    monkeypatch.setattr(registry, "publish_workspace", delayed_publish)
    payload = {
        "services": [],
        "expected_services": None,
        "expected_context": _workspace_claim(workspace),
    }
    headers = _workspace_post_headers(owner, workspace)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
        request_task = asyncio.create_task(
            client.post(
                f"/api/projects/{project_id}/services", json=payload, headers=headers
            )
        )
        await asyncio.wait_for(committed.wait(), timeout=1)
        request_task.cancel()
        await asyncio.sleep(0)
        request_task.cancel()
        for _ in range(5):
            await asyncio.sleep(0)
        assert not request_task.done()
        assert registry.transition_lock.locked()
        assert projects.service_access_lock.locked()
        assert workspace.session.turn_lock.locked()
        assert (await projects.store.get(project_id)).settings["services"] == []
        assert workspace.project.services is None
        assert peer_workspace.project.services is None

        turn_barrier_waiter = asyncio.create_task(workspace.session.turn_lock.acquire())
        await asyncio.sleep(0)
        assert not turn_barrier_waiter.done()
        release_commit.set()
        await asyncio.wait_for(publication_entered.wait(), timeout=1)
        assert registry.transition_lock.locked()
        transition_waiter = asyncio.create_task(registry.transition_lock.acquire())
        await asyncio.sleep(0)
        assert not transition_waiter.done()
        release_publication.set()
        with pytest.raises(asyncio.CancelledError):
            await request_task
        await asyncio.wait_for(turn_barrier_waiter, timeout=1)
        workspace.session.turn_lock.release()
        await asyncio.wait_for(transition_waiter, timeout=1)
        registry.transition_lock.release()

        assert workspace.project.services == ()
        assert peer_workspace.project.services == ()
        assert projects.current().services == ()
        with bind_execution_context(workspace.context):
            assert projects.current().services == ()
        assert not registry.transition_lock.locked()
        assert not projects.service_access_lock.locked()
        assert cas_calls == apply_calls == 1
        for event_socket, event_workspace in (
            (socket, workspace),
            (peer_socket, peer_workspace),
        ):
            assert event_socket.sent == [
                {
                    "kind": "project_services_changed",
                    **event_workspace.context.to_wire(),
                    "workspace_id": event_workspace.workspace_id,
                    "context_revision": event_workspace.context_revision,
                }
            ]

        socket.sent.clear()
        peer_socket.sent.clear()
        workspace.voice_active = 1
        retry = await client.post(
            f"/api/projects/{project_id}/services", json=payload, headers=headers
        )
        workspace.voice_active = 0

    assert retry.status_code == 200 and retry.json()["services"] == []
    assert cas_calls == apply_calls == 1
    assert socket.sent == []
    assert peer_socket.sent == []


async def test_unchanged_durable_policy_never_repairs_stale_cache_during_work(
    tmp_path: Path,
) -> None:
    registry, connections, projects = await _registry(tmp_path)
    project_id = await projects.store.create(name="Project A")
    auth = AuthManager(token="tok")
    owner = auth.mint_session()
    workspace = await registry.attach(
        connections.register(_Socket(), owner_session=owner), owner_session=owner
    )
    await workspace.select_project(project_id)
    registry.refresh_context(workspace)
    assert workspace.project.services is None
    # Reproduce the only state an interrupted pre-fix request could leave: durable policy is new,
    # while the live immutable execution binding is still old.
    assert await projects.store.set_services(project_id, [])

    config = load_config(root=tmp_path, env_file=None)
    config.services.enabled = ["exa", "firecrawl"]
    app = create_app(config, auth=auth, connections=connections)
    app.state.projects = projects
    app.state.workspaces = registry
    payload = {
        "services": [],
        "expected_services": None,
        "expected_context": _workspace_claim(workspace),
    }
    headers = _workspace_post_headers(owner, workspace)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
        workspace.voice_active = 1
        busy = await client.post(
            f"/api/projects/{project_id}/services", json=payload, headers=headers
        )
        assert busy.status_code == 409 and busy.json()["reason"] == "project_busy"
        assert workspace.project.services is None

        workspace.voice_active = 0
        repaired = await client.post(
            f"/api/projects/{project_id}/services", json=payload, headers=headers
        )

    assert repaired.status_code == 200
    assert workspace.project.services == ()
    with bind_execution_context(workspace.context):
        assert projects.current().services == ()


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


async def test_emergency_cancel_snapshots_all_before_draining(tmp_path: Path) -> None:
    registry, connections, _projects = await _registry(tmp_path)
    workspaces = [
        await registry.attach(
            connections.register(_Socket(), owner_session=f"owner-{index}"),
            owner_session=f"owner-{index}",
        )
        for index in range(2)
    ]
    started = [asyncio.Event(), asyncio.Event()]
    cancel_seen = [asyncio.Event(), asyncio.Event()]
    release = [asyncio.Event(), asyncio.Event()]

    async def held_turn(index: int) -> None:
        started[index].set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancel_seen[index].set()
            await release[index].wait()
            raise

    for index, workspace in enumerate(workspaces):
        workspace.session._current = asyncio.create_task(held_turn(index))
    await asyncio.gather(*(event.wait() for event in started))

    draining = asyncio.create_task(registry.cancel_all_and_wait())
    # A sequential cancel-and-await implementation would deadlock on the first cleanup and
    # never signal the second event. Both cancellation requests must be issued up front.
    await asyncio.gather(*(event.wait() for event in cancel_seen))
    assert registry.global_turn_busy and not draining.done()
    for event in release:
        event.set()

    assert await draining == 2
    assert not registry.global_turn_busy


async def test_emergency_drain_waits_non_cancellable_snapshot_and_shields_targets(
    tmp_path: Path,
) -> None:
    registry, connections, _projects = await _registry(tmp_path)
    workspace = await registry.attach(
        connections.register(_Socket(), owner_session="owner"), owner_session="owner"
    )
    started = asyncio.Event()
    cancel_seen = asyncio.Event()
    release = asyncio.Event()

    async def settling_turn() -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancel_seen.set()
            await release.wait()
            raise

    target = asyncio.create_task(settling_turn())
    workspace.session._current = target
    await started.wait()
    draining = asyncio.create_task(registry.cancel_all_and_wait())
    await cancel_seen.wait()

    # Cancelling the HTTP-style waiter must not become a second Task.cancel() source for the
    # target's terminal cleanup.
    draining.cancel()
    with pytest.raises(asyncio.CancelledError):
        await draining
    assert target.cancelling() == 1 and not target.done()
    release.set()
    await asyncio.gather(target, return_exceptions=True)

    # A task already in the non-cancellable save phase is counted as zero cancellations, but
    # the exact live snapshot is still drained before a successful response may settle.
    save_release = asyncio.Event()
    save_started = asyncio.Event()

    async def normal_save() -> None:
        save_started.set()
        await save_release.wait()

    target = asyncio.create_task(normal_save())
    workspace.session._current = target
    workspace.session._cancellable_task = None
    workspace.session._settling_task = target
    await save_started.wait()
    cancel_observed = asyncio.Event()
    original_cancel = workspace.session.cancel

    def observed_cancel() -> bool:
        result = original_cancel()
        cancel_observed.set()
        return result

    workspace.session.cancel = observed_cancel  # type: ignore[method-assign]
    draining = asyncio.create_task(registry.cancel_all_and_wait())
    await cancel_observed.wait()
    assert not target.done() and not draining.done()
    save_release.set()
    assert await draining == 0


async def test_pause_establishes_runner_stop_then_drains_exact_turn_and_generation(
    tmp_path: Path,
) -> None:
    registry, connections, _projects = await _registry(tmp_path)
    auth = AuthManager(token="tok")
    owner = auth.mint_session()
    socket = _Socket()
    workspace = await registry.attach(
        connections.register(socket, owner_session=owner), owner_session=owner
    )
    turn_started = asyncio.Event()
    turn_cancelled = asyncio.Event()
    turn_release = asyncio.Event()
    stop_requested = asyncio.Event()
    runner_drain_started = asyncio.Event()
    runner_release = asyncio.Event()

    async def held_turn() -> None:
        turn_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            assert stop_requested.is_set()
            turn_cancelled.set()
            await turn_release.wait()
            raise

    class ExactRunner:
        def __init__(self) -> None:
            self.desired = True
            self.in_flight = None
            self.request_calls = 0
            self.stop_calls = 0
            self.snapshot: asyncio.Task | None = None

        @property
        def is_running(self) -> bool:
            return self.desired

        async def _drain(self) -> None:
            runner_drain_started.set()
            await runner_release.wait()

        def request_stop(self) -> asyncio.Task:
            self.request_calls += 1
            self.desired = False
            stop_requested.set()
            self.snapshot = asyncio.create_task(self._drain())
            return self.snapshot

        async def stop(self) -> None:
            self.stop_calls += 1
            raise AssertionError("pause must not issue a second stop command")

        def start(self) -> None:
            self.desired = True

    runner = ExactRunner()
    target = asyncio.create_task(held_turn())
    workspace.session._current = target
    await turn_started.wait()
    app = create_app(
        load_config(root=tmp_path, env_file=None),
        auth=auth,
        connections=connections,
        runner=runner,
    )
    app.state.workspaces = registry
    headers = _workspace_post_headers(owner, workspace)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1"
    ) as client:
        request = asyncio.create_task(client.post("/api/runner/pause", headers=headers))
        await asyncio.gather(turn_cancelled.wait(), runner_drain_started.wait())
        assert not request.done()
        turn_release.set()
        assert runner.snapshot is not None and not runner.snapshot.done()
        runner_release.set()
        response = await request

    assert response.status_code == 200
    assert response.json() == {
        "runner_available": True,
        "runner_running": False,
        "background_busy": False,
        "global_turn_busy": False,
        "in_flight": None,
        "turn_busy": False,
        "turn_id": None,
        "cancelled_turns": 1,
    }
    assert runner.request_calls == 1 and runner.stop_calls == 0
    assert socket.sent[-1] == {"kind": "runner_state"}


async def test_turn_cancel_requires_the_exact_context_and_turn_generation(
    tmp_path: Path,
) -> None:
    registry, connections, _projects = await _registry(tmp_path)
    auth = AuthManager(token="tok")
    owner = auth.mint_session()
    workspace = await registry.attach(
        connections.register(_Socket(), owner_session=owner), owner_session=owner
    )
    config = load_config(root=tmp_path, env_file=None)
    app = create_app(config, auth=auth, connections=connections)
    app.state.workspaces = registry
    headers = {
        "cookie": f"{SESSION_COOKIE}={owner}",
        WORKSPACE_HEADER: workspace.workspace_id,
        "origin": "http://127.0.0.1",
    }

    first = asyncio.create_task(asyncio.Event().wait())
    workspace.session._turn_generation = 1
    workspace.session._current = first
    old_context = workspace.context
    old_revision = workspace.context_revision
    old_claim = {**old_context.to_wire(), "context_revision": old_revision}
    first.cancel()
    await asyncio.gather(first, return_exceptions=True)

    second = asyncio.create_task(asyncio.Event().wait())
    workspace.session._turn_generation = 2
    workspace.session._current = second
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
        stale_turn = await client.post(
            "/api/turn/cancel",
            json={"expected_context": old_claim, "turn_id": 1},
            headers=headers,
        )
        assert stale_turn.status_code == 409
        assert not second.cancelled() and workspace.session.current_turn_id == 2

        second.cancel()
        await asyncio.gather(second, return_exceptions=True)
        async with registry.transition_lock:
            await workspace.start_new_session()
            registry.refresh_context(workspace)
        third = asyncio.create_task(asyncio.Event().wait())
        workspace.session._turn_generation = 3
        workspace.session._current = third
        stale_context = await client.post(
            "/api/turn/cancel",
            json={"expected_context": old_claim, "turn_id": 3},
            headers=headers,
        )
        assert stale_context.status_code == 409
        assert not third.cancelled() and workspace.session.current_turn_id == 3
        current_claim = {
            **workspace.context.to_wire(),
            "context_revision": workspace.context_revision,
        }
        current = await client.post(
            "/api/turn/cancel",
            json={"expected_context": current_claim, "turn_id": 3},
            headers=headers,
        )
        assert current.status_code == 200 and current.json()["cancelled"] is True
        await asyncio.gather(third, return_exceptions=True)


@pytest.mark.parametrize("transition_kind", ["new", "select", "resume"])
async def test_context_transition_wait_does_not_block_gate_resolution(
    tmp_path: Path, monkeypatch, transition_kind: str
) -> None:
    shared_turn_lock = asyncio.Lock()
    registry, connections, projects = await _registry(tmp_path, turn_lock=shared_turn_lock)
    auth = AuthManager(token="tok")
    owner = auth.mint_session()
    conn_a = connections.register(_Socket(), owner_session=owner)
    conn_b = connections.register(_Socket(), owner_session=owner)
    workspace_a = await registry.attach(conn_a, owner_session=owner)
    workspace_b = await registry.attach(conn_b, owner_session=owner)
    app = create_app(
        load_config(root=tmp_path, env_file=None),
        auth=auth,
        connections=connections,
    )
    app.state.projects = projects
    app.state.workspaces = registry
    resolution, decision_id, nonce = await _seed_workspace_approval(app, workspace_a, conn_a)

    target_project = await projects.store.create(name="Target")
    target_session = await workspace_b.session.sessions.create_session()
    await workspace_b.session.sessions.save_messages(
        target_session, [{"role": "user", "content": "resume target"}]
    )
    if transition_kind == "new":
        path = "/api/sessions/new"
        payload = {"expected_context": _workspace_claim(workspace_b)}
        prepare_name = "prepare_new_session"
    elif transition_kind == "select":
        path = "/api/projects/select"
        payload = {
            "project_id": target_project,
            "expected_context": _workspace_claim(workspace_b),
        }
        prepare_name = "prepare_new_session"
    else:
        path = f"/api/sessions/{target_session}/resume"
        payload = {"expected_context": _workspace_claim(workspace_b)}
        prepare_name = "prepare_resume"

    entered_prepare = asyncio.Event()
    original_prepare = getattr(workspace_b, prepare_name)

    async def observed_prepare(*args, **kwargs):
        entered_prepare.set()
        return await original_prepare(*args, **kwargs)

    monkeypatch.setattr(workspace_b, prepare_name, observed_prepare)
    transport = httpx.ASGITransport(app=app)
    transition_response = None
    approval_response = None
    approval_completed_while_turn_locked = False
    transition_request = None
    approval_request = None
    await shared_turn_lock.acquire()
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
        try:
            transition_request = asyncio.create_task(
                client.post(
                    path,
                    json=payload,
                    headers=_workspace_post_headers(owner, workspace_b),
                )
            )
            await asyncio.wait_for(entered_prepare.wait(), timeout=1)
            approval_request = asyncio.create_task(
                client.post(
                    f"/api/approvals/{decision_id}/resolve",
                    json={
                        "nonce": nonce,
                        "action": "approve",
                        "expected_context": _workspace_claim(workspace_a),
                    },
                    headers=_workspace_post_headers(owner, workspace_a),
                )
            )
            done, _pending = await asyncio.wait({approval_request}, timeout=1)
            approval_completed_while_turn_locked = approval_request in done
        finally:
            shared_turn_lock.release()
            if transition_request is not None:
                transition_response = await asyncio.wait_for(transition_request, timeout=2)
            if approval_request is not None:
                approval_response = await asyncio.wait_for(approval_request, timeout=2)

    if not resolution.done():
        app.state.approvals.resolve(decision_id, nonce, "deny", context=workspace_a.context)
    resolved_permission = await asyncio.wait_for(resolution, timeout=1)
    assert approval_completed_while_turn_locked
    assert approval_response is not None and approval_response.status_code == 200
    assert transition_response is not None and transition_response.status_code == 200
    assert resolved_permission is Permission.ALLOW


async def test_queued_turn_keeps_approval_and_exact_cancel_routes_responsive(
    tmp_path: Path,
) -> None:
    shared_turn_lock = asyncio.Lock()
    registry, connections, projects = await _registry(tmp_path, turn_lock=shared_turn_lock)
    auth = AuthManager(token="tok")
    owner = auth.mint_session()
    conn_a = connections.register(_Socket(), owner_session=owner)
    conn_b = connections.register(_Socket(), owner_session=owner)
    workspace_a = await registry.attach(conn_a, owner_session=owner)
    workspace_b = await registry.attach(conn_b, owner_session=owner)
    app = create_app(
        load_config(root=tmp_path, env_file=None),
        auth=auth,
        connections=connections,
    )
    app.state.projects = projects
    app.state.workspaces = registry
    resolution, decision_id, nonce = await _seed_workspace_approval(app, workspace_a, conn_a)
    transport = httpx.ASGITransport(app=app)
    turn_response = None
    approval_response = None
    cancel_response = None
    turn_returned_while_locked = False
    approval_returned_while_locked = False
    cancel_returned_while_locked = False
    turn_request = None
    approval_request = None
    cancel_request = None
    await shared_turn_lock.acquire()
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
        try:
            turn_request = asyncio.create_task(
                client.post(
                    "/api/turn",
                    json={
                        "text": "queued behind the Gate-paused turn",
                        "expected_context": _workspace_claim(workspace_b),
                    },
                    headers=_workspace_post_headers(owner, workspace_b),
                )
            )
            done, _pending = await asyncio.wait({turn_request}, timeout=1)
            turn_returned_while_locked = turn_request in done
            if turn_returned_while_locked:
                turn_response = turn_request.result()
                turn_id = turn_response.json()["turn_id"]
                approval_request = asyncio.create_task(
                    client.post(
                        f"/api/approvals/{decision_id}/resolve",
                        json={
                            "nonce": nonce,
                            "action": "approve",
                            "expected_context": _workspace_claim(workspace_a),
                        },
                        headers=_workspace_post_headers(owner, workspace_a),
                    )
                )
                cancel_request = asyncio.create_task(
                    client.post(
                        "/api/turn/cancel",
                        json={
                            "turn_id": turn_id,
                            "expected_context": _workspace_claim(workspace_b),
                        },
                        headers=_workspace_post_headers(owner, workspace_b),
                    )
                )
                done, _pending = await asyncio.wait({approval_request, cancel_request}, timeout=1)
                approval_returned_while_locked = approval_request in done
                cancel_returned_while_locked = cancel_request in done
        finally:
            shared_turn_lock.release()
            if turn_request is not None:
                turn_response = await asyncio.wait_for(turn_request, timeout=2)
            if approval_request is not None:
                approval_response = await asyncio.wait_for(approval_request, timeout=2)
            if cancel_request is not None:
                cancel_response = await asyncio.wait_for(cancel_request, timeout=2)

    workspace_b.session.cancel()
    if workspace_b.session._current is not None:
        await asyncio.gather(workspace_b.session._current, return_exceptions=True)
    if not resolution.done():
        app.state.approvals.resolve(decision_id, nonce, "deny", context=workspace_a.context)
    resolved_permission = await asyncio.wait_for(resolution, timeout=1)
    assert turn_returned_while_locked
    assert approval_returned_while_locked
    assert cancel_returned_while_locked
    assert turn_response is not None and turn_response.status_code == 200
    assert approval_response is not None and approval_response.status_code == 200
    assert cancel_response is not None and cancel_response.json()["cancelled"] is True
    assert resolved_permission is Permission.ALLOW


async def test_voice_utterance_rejects_replaced_workspace_context(tmp_path: Path) -> None:
    registry, connections, _projects = await _registry(tmp_path)
    auth = AuthManager(token="tok")
    owner = auth.mint_session()
    workspace = await registry.attach(
        connections.register(_Socket(), owner_session=owner), owner_session=owner
    )
    calls: list[bytes] = []

    class _Voice:
        listener = object()

        async def handle_utterance(self, audio: bytes) -> bool:
            calls.append(audio)
            return True

    workspace.voice = _Voice()
    old_context = workspace.context
    old_revision = workspace.context_revision
    async with registry.transition_lock:
        await workspace.start_new_session()
        registry.refresh_context(workspace)

    config = load_config(root=tmp_path, env_file=None)
    app = create_app(config, auth=auth, connections=connections)
    app.state.workspaces = registry
    transport = httpx.ASGITransport(app=app)
    headers = {
        "cookie": f"{SESSION_COOKIE}={owner}",
        WORKSPACE_HEADER: workspace.workspace_id,
        EXPECTED_SESSION_HEADER: str(old_context.session_id),
        EXPECTED_PROJECT_HEADER: "global",
        EXPECTED_CONTEXT_REVISION_HEADER: str(old_revision),
        "origin": "http://127.0.0.1",
        "content-type": "audio/webm",
    }
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
        response = await client.post(
            "/api/voice/utterance?mode=conversation", content=b"old-context-audio", headers=headers
        )

    assert response.status_code == 409
    assert response.json()["message"] == "workspace context changed; retry from the current screen"
    assert calls == []

    current_headers = {
        **headers,
        EXPECTED_SESSION_HEADER: str(workspace.context.session_id),
        EXPECTED_PROJECT_HEADER: "global",
        EXPECTED_CONTEXT_REVISION_HEADER: str(workspace.context_revision),
    }
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
        current = await client.post(
            "/api/voice/utterance?mode=conversation",
            content=b"current-context-audio",
            headers=current_headers,
        )
    assert current.status_code == 200 and calls == [b"current-context-audio"]


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
    assert registry.resolve(owner_session="owner-a", workspace_id=workspace_a1.workspace_id) is None
    assert (
        registry.resolve(owner_session="owner-b", workspace_id=workspace_b.workspace_id)
        is workspace_b
    )
    assert {context.session_id for context in replaced} == {
        workspace_a1.context.session_id,
        workspace_a2.context.session_id,
    }

    assert registry.drop_all() == 1
    assert registry.resolve(owner_session="owner-b", workspace_id=workspace_b.workspace_id) is None


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


async def test_server_capture_lease_is_exclusive_only_while_microphone_is_open(
    tmp_path: Path,
) -> None:
    registry, connections, _projects = await _registry(tmp_path)
    workspace_a = await registry.attach(
        connections.register(_Socket(), owner_session="same-browser"),
        owner_session="same-browser",
    )
    workspace_b = await registry.attach(
        connections.register(_Socket(), owner_session="same-browser"),
        owner_session="same-browser",
    )

    class _Capture:
        async def capture_utterance(self) -> bytes:
            return b"audio"

    async with registry.voice_activity(workspace_a):
        assert workspace_a.attended_busy
        lease_a = await registry.reserve_server_capture(_Capture())
        with pytest.raises(RuntimeError, match="busy"):
            await registry.reserve_server_capture(_Capture())
        # Uploaded browser audio does not contend for the workstation's physical microphone.
        async with registry.voice_activity(workspace_b):
            assert workspace_b.attended_busy
        assert await lease_a.capture_utterance() == b"audio"

        # Transcription/model work remains admitted in A, but the physical device is already
        # free for another tab as soon as capture_utterance returns.
        assert workspace_a.attended_busy
        lease_b = await registry.reserve_server_capture(_Capture())
        await lease_b.release()

    assert not workspace_a.attended_busy


async def test_meeting_capture_broadcasts_one_global_physical_mic_interval(
    tmp_path: Path,
) -> None:
    registry, connections, _projects = await _registry(tmp_path)
    socket_a, socket_b = _Socket(), _Socket()
    await registry.attach(
        connections.register(socket_a, owner_session="browser-a"),
        owner_session="browser-a",
    )
    await registry.attach(
        connections.register(socket_b, owner_session="browser-b"),
        owner_session="browser-b",
    )
    opened = asyncio.Event()
    close = asyncio.Event()

    class _Capture:
        async def capture_utterance(self) -> bytes:
            opened.set()
            await close.wait()
            return b"audio"

    lease = await registry.reserve_server_capture(_Capture(), meeting=True)
    assert registry.server_capture_active is False
    assert registry.meeting_recording_active is False
    capture = asyncio.create_task(lease.capture_utterance())
    await opened.wait()
    await asyncio.sleep(0)
    assert registry.server_capture_active is True
    assert registry.meeting_recording_active is True
    assert registry.meeting_recording_revision == 1
    assert socket_a.sent[-1] == {
        "kind": "meeting_recording",
        "active": True,
        "epoch": registry.meeting_recording_epoch,
        "revision": 1,
    }
    assert socket_b.sent[-1] == {
        "kind": "meeting_recording",
        "active": True,
        "epoch": registry.meeting_recording_epoch,
        "revision": 1,
    }

    close.set()
    assert await capture == b"audio"
    await asyncio.sleep(0)
    assert registry.server_capture_active is False
    assert registry.meeting_recording_active is False
    assert registry.meeting_recording_revision == 2
    assert socket_a.sent[-1] == {
        "kind": "meeting_recording",
        "active": False,
        "epoch": registry.meeting_recording_epoch,
        "revision": 2,
    }
    assert socket_b.sent[-1] == {
        "kind": "meeting_recording",
        "active": False,
        "epoch": registry.meeting_recording_epoch,
        "revision": 2,
    }


async def test_stalled_socket_cannot_block_meeting_source_or_registry_transitions(
    tmp_path: Path,
) -> None:
    registry, connections, _projects = await _registry(tmp_path)
    stalled_send = asyncio.Event()
    source_opened = asyncio.Event()
    source_close = asyncio.Event()

    class _StalledSocket:
        async def send_json(self, _message: dict) -> None:
            await stalled_send.wait()

    class _Capture:
        async def capture_utterance(self) -> bytes:
            source_opened.set()
            await source_close.wait()
            return b"audio"

    await registry.attach(
        connections.register(_StalledSocket(), owner_session="slow-browser"),
        owner_session="slow-browser",
    )
    lease = await registry.reserve_server_capture(_Capture(), meeting=True)
    capture = asyncio.create_task(lease.capture_utterance())
    await asyncio.wait_for(source_opened.wait(), timeout=0.2)
    assert registry.server_capture_active is True
    assert registry.transition_lock.locked() is False
    with pytest.raises(RuntimeError, match="busy"):
        await asyncio.wait_for(
            registry.reserve_server_capture(_Capture(), meeting=True), timeout=0.2
        )

    source_close.set()
    assert await asyncio.wait_for(capture, timeout=0.2) == b"audio"
    assert registry.server_capture_active is False
    assert registry.transition_lock.locked() is False
    stalled_send.set()
    await asyncio.sleep(0)


async def test_server_capture_lease_is_single_use_and_cannot_release_while_open(
    tmp_path: Path,
) -> None:
    registry, _connections, _projects = await _registry(tmp_path)
    opened = asyncio.Event()
    close = asyncio.Event()
    calls = 0

    class _Capture:
        async def capture_utterance(self) -> bytes:
            nonlocal calls
            calls += 1
            opened.set()
            await close.wait()
            return b"audio"

    lease = await registry.reserve_server_capture(_Capture(), meeting=True)
    first = asyncio.create_task(lease.capture_utterance())
    await opened.wait()
    with pytest.raises(RuntimeError, match="already used"):
        await lease.capture_utterance()
    await lease.release()
    assert registry.server_capture_active is True
    with pytest.raises(RuntimeError, match="busy"):
        await registry.reserve_server_capture(_Capture())
    assert calls == 1

    close.set()
    assert await first == b"audio"
    replacement = await registry.reserve_server_capture(_Capture())
    await replacement.release()


async def test_cancelled_capture_wins_over_late_source_error(tmp_path: Path) -> None:
    registry, _connections, _projects = await _registry(tmp_path)
    opened = asyncio.Event()
    fail = asyncio.Event()

    class _FailingCapture:
        async def capture_utterance(self) -> bytes:
            opened.set()
            await fail.wait()
            raise RuntimeError("late device failure")

    lease = await registry.reserve_server_capture(_FailingCapture(), meeting=True)
    capture = asyncio.create_task(lease.capture_utterance())
    await opened.wait()
    capture.cancel()
    await asyncio.sleep(0)
    fail.set()
    with pytest.raises(asyncio.CancelledError):
        await capture
    assert registry.server_capture_active is False


async def test_cancelled_activation_releases_its_reserved_lease(tmp_path: Path) -> None:
    registry, _connections, _projects = await _registry(tmp_path)

    class _Capture:
        async def capture_utterance(self) -> bytes:
            return b"audio"

    lease = await registry.reserve_server_capture(_Capture(), meeting=True)
    await registry.transition_lock.acquire()
    capture = asyncio.create_task(lease.capture_utterance())
    await asyncio.sleep(0)
    capture.cancel()
    registry.transition_lock.release()
    with pytest.raises(asyncio.CancelledError):
        await capture

    replacement = await registry.reserve_server_capture(_Capture())
    await replacement.release()


async def test_meeting_workflow_event_targets_one_workspace_even_for_same_session(
    tmp_path: Path,
) -> None:
    registry, connections, _projects = await _registry(tmp_path)
    socket_a, socket_b = _Socket(), _Socket()
    workspace_a = await registry.attach(
        connections.register(socket_a, owner_session="same-browser"),
        owner_session="same-browser",
    )
    workspace_b = await registry.attach(
        connections.register(socket_b, owner_session="same-browser"),
        owner_session="same-browser",
    )
    await workspace_a.session.sessions.save_messages(
        workspace_a.context.session_id,
        [{"role": "user", "content": "shared transcript"}],
    )
    assert await workspace_b.resume(workspace_a.context.session_id)
    registry.refresh_context(workspace_b)
    assert workspace_b.context == workspace_a.context

    await registry.publish_workspace(workspace_a, {"kind": "meeting_state", "state": "saving"})
    assert socket_a.sent[-1] == {
        "kind": "meeting_state",
        "state": "saving",
        **workspace_a.context.to_wire(),
        "context_revision": workspace_a.context_revision,
        "workspace_id": workspace_a.workspace_id,
    }
    assert not any(message.get("kind") == "meeting_state" for message in socket_b.sent)


async def test_cancelled_capture_keeps_lease_until_physical_source_closes(tmp_path: Path) -> None:
    registry, _connections, _projects = await _registry(tmp_path)
    started = asyncio.Event()
    physical_closed = asyncio.Event()

    class _ThreadBackedLikeCapture:
        async def capture_utterance(self) -> bytes:
            started.set()
            await physical_closed.wait()
            return b"audio"

    lease = await registry.reserve_server_capture(_ThreadBackedLikeCapture(), meeting=True)
    capture = asyncio.create_task(lease.capture_utterance())
    await started.wait()
    assert registry.server_capture_active is True
    assert registry.meeting_recording_active is True
    capture.cancel()
    await asyncio.sleep(0)

    # Cancellation of an asyncio.to_thread await cannot stop its microphone worker. The lease
    # therefore remains occupied until that bounded worker really returns and closes the device.
    assert not capture.done()
    with pytest.raises(RuntimeError, match="busy"):
        await registry.reserve_server_capture(_ThreadBackedLikeCapture())

    physical_closed.set()
    with pytest.raises(asyncio.CancelledError):
        await capture
    assert registry.server_capture_active is False
    assert registry.meeting_recording_active is False
    replacement = await registry.reserve_server_capture(_ThreadBackedLikeCapture())
    await replacement.release()


async def test_meeting_receipt_is_single_flight_beyond_physical_capture(tmp_path: Path) -> None:
    registry, _connections, _projects = await _registry(tmp_path)
    receipt = "project:7:123e4567-e89b-42d3-a456-426614174000"

    async with registry.meeting_receipt_activity(receipt):
        with pytest.raises(RuntimeError, match="busy"):
            async with registry.meeting_receipt_activity(receipt):
                pass

        # A different logical note is not blocked merely because this receipt is transcribing.
        async with registry.meeting_receipt_activity(
            "project:7:123e4567-e89b-42d3-a456-426614174001"
        ):
            pass

    async with registry.meeting_receipt_activity(receipt):
        pass


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
    knowledge = KnowledgeService(
        KnowledgeStore(workspace_a.session.sessions.db, workspace_a.session.sessions.lock),
        FakeEmbedder(),
        KnowledgeConfig(),
        knowledge_dir=tmp_path / "knowledge",
        root=tmp_path,
    )
    knowledge.ensure_dirs()
    knowledge.bound_unattended = True
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
        knowledge=knowledge,
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
        workspace = workspace_a if workspace_id == workspace_a.workspace_id else workspace_b
        return {
            "cookie": f"{SESSION_COOKIE}={owner}",
            WORKSPACE_HEADER: workspace_id,
            EXPECTED_SESSION_HEADER: str(workspace.context.session_id),
            EXPECTED_PROJECT_HEADER: (
                "global"
                if workspace.context.project_id is None
                else str(workspace.context.project_id)
            ),
            EXPECTED_CONTEXT_REVISION_HEADER: str(workspace.context_revision),
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
    assert (
        client.get(
            "/api/search", params={"q": "project-a", "project_id": project_a}, headers=foreign
        ).status_code
        == 404
    )
    assert [row["text"] for row in client.get("/api/notices", headers=own).json()["notices"]] == [
        "Project A scheduler payload"
    ]
    foreign_notices = client.get("/api/notices", headers=foreign).json()["notices"]
    assert [row["text"] for row in foreign_notices] == ["Project B scheduler payload"]
    assert [row["text"] for row in client.get("/api/daily", headers=own).json()["notices"]] == [
        "Project A scheduler payload"
    ]
    assert [row["text"] for row in client.get("/api/daily", headers=foreign).json()["notices"]] == [
        "Project B scheduler payload"
    ]
    digest_response = client.post("/api/digest/run", headers={**own, "origin": "http://127.0.0.1"})
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
    foreign_services = client.post(
        f"/api/projects/{project_b}/services",
        json={"services": None},
        headers=own_post,
    )
    assert foreign_services.status_code == 404

    # Memory deletion must obey the same P + global boundary. A foreign id cannot become a
    # capability merely because its numeric value was observed elsewhere in the UI.
    own_memory = await memory.store.add(
        type="fact",
        content="A only",
        embedding=[0.1, 0.2],
        embedding_model="fake",
        source="user",
        project_id=project_a,
    )
    foreign_memory = await memory.store.add(
        type="fact",
        content="B only",
        embedding=[0.3, 0.4],
        embedding_model="fake",
        source="user",
        project_id=project_b,
    )
    assert client.post(f"/api/memory/{foreign_memory}/forget", headers=own_post).status_code == 404
    assert (await memory.store.get(foreign_memory)).status == "live"
    assert client.post(f"/api/memory/{own_memory}/forget", headers=own_post).json()["ok"] is True
    assert (await memory.store.get(own_memory)).status == "forgotten"

    # Gate detail, queue, and every mutation route must share the live workspace boundary; a
    # numeric intent/attention id from Project B never becomes read or execution authority in A.
    assert client.get("/api/intents", headers=own).json()["pending"][0]["id"] == own_intent
    assert (
        client.get("/api/intents", params={"project_id": project_b}, headers=own).status_code == 404
    )
    for suffix in ("", "/approve", "/reject", "/undo"):
        method = client.get if not suffix else client.post
        kwargs = {"headers": own if not suffix else own_post}
        assert method(f"/api/intents/{foreign_intent}{suffix}", **kwargs).status_code == 404
    assert (await intents.get(foreign_intent)).state.value == "previewed"
    assert (
        client.post(
            f"/api/attention/{foreign_attention}/resolve",
            json={"action": "dismiss"},
            headers=own_post,
        ).status_code
        == 404
    )
    assert (await attention.get(foreign_attention)).state.value == "open"
    assert (
        client.post(
            f"/api/attention/{own_attention}/resolve", json={"action": "dismiss"}, headers=own_post
        ).json()["ok"]
        is True
    )

    # Connector-write audit rows follow the same server-owned workspace scope. The browser has
    # no project selector and no remote/rollback handles to turn a numeric id into authority.
    own_audit = client.get("/api/connector-writes", headers=own).json()["writes"]
    foreign_audit = client.get("/api/connector-writes", headers=foreign).json()["writes"]
    assert [row["id"] for row in own_audit] == [own_write]
    assert [row["id"] for row in foreign_audit] == [foreign_write]

    # Quarantined graph-suggestion review mutations use the same P + global workspace scope as
    # their review queue. A guessed numeric id from Project B must never resolve its proposal.
    own_post = {**own, "origin": "http://127.0.0.1"}
    assert (
        client.post(
            f"/api/graph/suggestions/{foreign_suggestion}/approve", headers=own_post
        ).status_code
        == 404
    )
    assert (
        client.post(
            f"/api/graph/suggestions/{foreign_suggestion}/reject", headers=own_post
        ).status_code
        == 404
    )
    assert (await graph.get_suggestion(foreign_suggestion)).status == "pending"
    assert (
        client.get(f"/api/graph/suggestions?project_id={project_a}", headers=foreign).status_code
        == 404
    )
    assert (
        client.post(f"/api/graph/suggestions/{own_suggestion}/approve", headers=own_post).json()[
            "ok"
        ]
        is True
    )
    assert (
        client.post(f"/api/graph/suggestions/{global_suggestion}/reject", headers=own_post).json()[
            "ok"
        ]
        is True
    )

    def drive_exposed(payload: dict) -> bool:
        row = next(
            row for row in payload["capabilities"]["connectors"] if row["name"] == "Google Drive"
        )
        return row["exposed_to_chat"]

    for path in ("/api/capabilities", "/api/daily", "/api/hub", "/api/settings"):
        own_payload = client.get(path, headers=own).json()
        foreign_payload = client.get(path, headers=foreign).json()
        own_caps = own_payload if path == "/api/capabilities" else own_payload["capabilities"]
        foreign_caps = (
            foreign_payload if path == "/api/capabilities" else foreign_payload["capabilities"]
        )
        assert drive_exposed({"capabilities": own_caps}) is True, path
        assert drive_exposed({"capabilities": foreign_caps}) is False, path

    # A human-review draft binds to the exact live context it opened in. If another duplicated
    # tab switches this shared workspace before Save, the stale assertion fails closed rather
    # than retagging reviewed Project A content as Project B durable memory.
    expected_context = {
        "session_id": workspace_a.context.session_id,
        "project_id": workspace_a.context.project_id,
        "context_revision": workspace_a.context_revision,
    }
    await workspace_a.session.sessions.save_messages(
        expected_context["session_id"],
        [{"role": "user", "content": "ABA authority anchor"}],
    )
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
    switched = client.post("/api/projects/select", json={"project_id": project_b}, headers=own_post)
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

    # Re-enter the original session after visiting B. The session/project tuple is now identical
    # to the old claim, so only the monotonic context revision can distinguish this ABA cycle.
    current_b_post = {
        **headers(owner_a, workspace_a.workspace_id),
        "origin": "http://127.0.0.1",
    }
    resumed = client.post(
        f"/api/sessions/{expected_context['session_id']}/resume",
        json={},
        headers=current_b_post,
    )
    assert resumed.status_code == 200 and resumed.json()["ok"] is True
    assert workspace_a.context == ExecutionContext(
        session_id=expected_context["session_id"], project_id=project_a
    )
    assert workspace_a.context_revision > expected_context["context_revision"]

    aba_task = await tasks.schedule(
        kind="reminder",
        title="ABA task",
        payload="review",
        schedule_kind="once",
        schedule_spec="2099-01-01T00:00:00Z",
        created_by="user",
        project_id=project_a,
    )
    aba_memory = await memory.store.add(
        type="fact",
        content="ABA memory",
        embedding=[0.5, 0.6],
        embedding_model="fake",
        source="user",
        project_id=project_a,
    )
    aba_intent = await intents.create_draft(
        idempotency_key="project-a-aba-intent",
        provider="google",
        kind="calendar_create",
        request={},
        summary="Project A ABA draft",
        source="agent",
        project_id=project_a,
    )
    await intents.mark_previewed(
        aba_intent,
        preview={"title": "ABA", "fields": [], "diff": [], "notes": [], "warnings": []},
    )
    aba_suggestion = await graph.add_suggestion(
        kind="memory",
        payload={"content": "Project A ABA suggestion"},
        trust_class="model_generated",
        project_id=project_a,
    )
    aba_source = await knowledge.ingest(
        text="Project A ABA source",
        title="ABA source",
        project_id=project_a,
    )

    stale_route_responses = [
        client.post(f"/api/tasks/{aba_task.id}/cancel", headers=own_post),
        client.post(f"/api/memory/{aba_memory}/forget", headers=own_post),
        client.post(f"/api/intents/{aba_intent}/reject", headers=own_post),
        client.post(f"/api/graph/suggestions/{aba_suggestion}/approve", headers=own_post),
        client.post(f"/api/vault/sources/{aba_source.source_id}/approve", headers=own_post),
        client.post(f"/api/vault/sources/{aba_source.source_id}/reject", headers=own_post),
        client.post(
            f"/api/sessions/{expected_context['session_id']}/archive",
            json={"archived": True},
            headers=own_post,
        ),
        client.post(
            f"/api/projects/{project_a}/update",
            json={"name": "ABA hijack"},
            headers=own_post,
        ),
        client.post(f"/api/projects/{project_a}/archive", headers=own_post),
        client.post(
            f"/api/projects/{project_a}/services",
            json={"services": None},
            headers=own_post,
        ),
    ]
    assert {response.status_code for response in stale_route_responses} == {409}
    assert (await tasks.store.get(aba_task.id)).status != "cancelled"
    assert (await memory.store.get(aba_memory)).status == "live"
    assert (await intents.get(aba_intent)).state.value == "previewed"
    assert (await graph.get_suggestion(aba_suggestion)).status == "pending"
    assert (await knowledge.store.get_source(aba_source.source_id)).review_status == "unreviewed"
    assert (await projects.store.get(project_a)).name == "Project A"
    assert (await projects.store.get(project_a)).status == "active"

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
    current_post = {
        **headers(owner_a, workspace_a.workspace_id),
        "origin": "http://127.0.0.1",
    }
    global_scope = client.post(
        "/api/projects/select", json={"project_id": None}, headers=current_post
    )
    assert global_scope.status_code == 200
    assert client.get(f"/api/tasks/{admin_task.id}/runs", headers=own).status_code == 200
    global_post = {
        **headers(owner_a, workspace_a.workspace_id),
        "origin": "http://127.0.0.1",
    }
    cancelled = client.post(f"/api/tasks/{admin_task.id}/cancel", headers=global_post)
    assert cancelled.json()["ok"] is True


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
    missing_workspace = client.get("/api/agents", headers={"cookie": f"{SESSION_COOKIE}={owner_a}"})
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
        assert hello_a["meeting_recording_epoch"] == registry.meeting_recording_epoch
        with client.websocket_connect("/ws", headers=headers) as socket_b:
            assert socket_b.receive_json()["type"] == "hello"
            socket_b.send_json({"type": "hello", "surfaces": []})
            hello_b = socket_b.receive_json()
            assert hello_b["type"] == "workspace"
            assert hello_b["meeting_recording_epoch"] == registry.meeting_recording_epoch
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
            assert response_a.json()["runner_available"] is True
            assert response_a.json()["background_busy"] is True
            assert response_a.json()["global_turn_busy"] is False

            project_a = await projects.store.create(name="Socket A only")
            switched = client.post(
                "/api/projects/select",
                json={"project_id": project_a},
                headers={
                    "cookie": cookie,
                    "origin": "http://127.0.0.1",
                    WORKSPACE_HEADER: hello_a["workspace_id"],
                    EXPECTED_SESSION_HEADER: str(hello_a["session_id"]),
                    EXPECTED_PROJECT_HEADER: "global",
                    EXPECTED_CONTEXT_REVISION_HEADER: str(hello_a["context_revision"]),
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
                src_kind="project",
                src_id=str(project_a),
                dst_kind="folder",
                dst_id=f"{project_a}:private",
                edge_kind="contains",
                origin="derived",
                trust_class="trusted_local",
                created_by="system",
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
                            "context_revision": runner["context_revision"],
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
                json={
                    "expected_context": {
                        "session_id": after_b["session_id"],
                        "project_id": None,
                        "context_revision": after_b["context_revision"],
                    }
                },
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
                    EXPECTED_SESSION_HEADER: str(changed["session_id"]),
                    EXPECTED_PROJECT_HEADER: str(changed["project_id"]),
                    EXPECTED_CONTEXT_REVISION_HEADER: str(changed["context_revision"]),
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


async def test_shared_session_archive_rolls_back_every_replacement_on_insert_failure(
    tmp_path: Path, monkeypatch
) -> None:
    registry, connections, projects = await _registry(tmp_path)
    project_id = await projects.store.create(name="Atomic Session Archive")
    auth = AuthManager(token="tok")
    owner = auth.mint_session()
    socket_a, socket_b = _Socket(), _Socket()
    workspace_a = await registry.attach(
        connections.register(socket_a, owner_session=owner), owner_session=owner
    )
    workspace_b = await registry.attach(
        connections.register(socket_b, owner_session=owner), owner_session=owner
    )
    await workspace_a.select_project(project_id)
    await workspace_b.select_project(project_id)
    registry.refresh_context(workspace_a)
    registry.refresh_context(workspace_b)
    sessions = workspace_a.session.sessions
    assert sessions is not None
    shared_session_id = workspace_a.context.session_id
    await sessions.save_messages(
        shared_session_id,
        [{"role": "user", "content": "shared authority must survive rollback"}],
    )
    assert await workspace_b.resume(shared_session_id)
    registry.refresh_context(workspace_b)
    before = [
        (item.context, item.context_revision, list(item.session.messages))
        for item in (workspace_a, workspace_b)
    ]
    before_count = (await (await sessions.db.execute("SELECT COUNT(*) FROM sessions")).fetchone())[
        0
    ]
    socket_a.sent.clear()
    socket_b.sent.clear()

    original_insert = sessions.create_session_in_transaction
    inserts = 0

    async def fail_second_insert(*args, **kwargs):
        nonlocal inserts
        inserts += 1
        if inserts == 2:
            raise OSError("injected replacement failure")
        return await original_insert(*args, **kwargs)

    monkeypatch.setattr(sessions, "create_session_in_transaction", fail_second_insert)
    app = create_app(
        load_config(root=tmp_path, env_file=None),
        auth=auth,
        connections=connections,
    )
    app.state.projects = projects
    app.state.workspaces = registry
    app.state.services = UiServices(sessions=sessions)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
        response = await client.post(
            f"/api/sessions/{shared_session_id}/archive",
            json={"archived": True, "expected_context": _workspace_claim(workspace_a)},
            headers=_workspace_post_headers(owner, workspace_a),
        )

    assert response.status_code == 503
    meta = await sessions.get_meta(shared_session_id)
    assert meta is not None and meta.archived is False
    assert [
        (item.context, item.context_revision, list(item.session.messages))
        for item in (workspace_a, workspace_b)
    ] == before
    after_count = (await (await sessions.db.execute("SELECT COUNT(*) FROM sessions")).fetchone())[0]
    assert after_count == before_count
    assert socket_a.sent == socket_b.sent == []


async def test_project_archive_rolls_back_all_tabs_and_rows_on_insert_failure(
    tmp_path: Path, monkeypatch
) -> None:
    registry, connections, projects = await _registry(tmp_path)
    project_id = await projects.store.create(name="Atomic Project Archive")
    auth = AuthManager(token="tok")
    owner = auth.mint_session()
    socket_a, socket_b = _Socket(), _Socket()
    workspace_a = await registry.attach(
        connections.register(socket_a, owner_session=owner), owner_session=owner
    )
    workspace_b = await registry.attach(
        connections.register(socket_b, owner_session=owner), owner_session=owner
    )
    await workspace_a.select_project(project_id)
    await workspace_b.select_project(project_id)
    registry.refresh_context(workspace_a)
    registry.refresh_context(workspace_b)
    sessions = workspace_a.session.sessions
    assert sessions is not None
    before = [(item.context, item.context_revision) for item in (workspace_a, workspace_b)]
    before_count = (await (await sessions.db.execute("SELECT COUNT(*) FROM sessions")).fetchone())[
        0
    ]
    socket_a.sent.clear()
    socket_b.sent.clear()

    original_insert = sessions.create_session_in_transaction
    inserts = 0

    async def fail_second_insert(*args, **kwargs):
        nonlocal inserts
        inserts += 1
        if inserts == 2:
            raise OSError("injected replacement failure")
        return await original_insert(*args, **kwargs)

    monkeypatch.setattr(sessions, "create_session_in_transaction", fail_second_insert)
    app = create_app(
        load_config(root=tmp_path, env_file=None),
        auth=auth,
        connections=connections,
    )
    app.state.projects = projects
    app.state.workspaces = registry
    app.state.services = UiServices(sessions=sessions)
    headers = {
        **_workspace_post_headers(owner, workspace_a),
        EXPECTED_SESSION_HEADER: str(workspace_a.context.session_id),
        EXPECTED_PROJECT_HEADER: str(project_id),
        EXPECTED_CONTEXT_REVISION_HEADER: str(workspace_a.context_revision),
    }
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
        response = await client.post(f"/api/projects/{project_id}/archive", headers=headers)

    assert response.status_code == 503
    project = await projects.store.get(project_id)
    assert project is not None and project.status == "active" and project.archived_at is None
    assert [(item.context, item.context_revision) for item in (workspace_a, workspace_b)] == before
    after_count = (await (await sessions.db.execute("SELECT COUNT(*) FROM sessions")).fetchone())[0]
    assert after_count == before_count
    assert socket_a.sent == socket_b.sent == []
