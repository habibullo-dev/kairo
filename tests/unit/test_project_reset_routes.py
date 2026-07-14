"""Owner-only, step-up-gated project reset route."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import httpx

from jarvis.config import load_config
from jarvis.persistence import SessionStore
from jarvis.persistence.db import connect
from jarvis.projects import ProjectService, ProjectStore
from jarvis.ui.auth import SESSION_COOKIE, AuthManager
from jarvis.ui.connections import ConnectionManager
from jarvis.ui.owner_auth import Argon2PasswordHasher, OwnerAuthService
from jarvis.ui.server import (
    EXPECTED_CONTEXT_REVISION_HEADER,
    EXPECTED_PROJECT_HEADER,
    EXPECTED_SESSION_HEADER,
    WORKSPACE_HEADER,
    create_app,
)
from jarvis.ui.session import UiSession
from jarvis.ui.workspaces import UiWorkspaceRegistry

PASSWORD = "A unique owner passphrase 2026!"
ORIGIN = {"origin": "http://127.0.0.1"}


class _SessionStub:
    def __init__(self) -> None:
        self.project_id: int | None = None
        self.switches: list[int | None] = []

    def start_new_session(self, project_id: int | None) -> None:
        self.project_id = project_id
        self.switches.append(project_id)


class _Socket:
    async def send_json(self, _message: dict) -> None:
        return None


@asynccontextmanager
async def _client(tmp_path: Path):
    db = await connect(tmp_path / "project-reset-routes.db")
    lock = asyncio.Lock()
    projects = ProjectService(ProjectStore(db, lock))
    owner = OwnerAuthService(
        db,
        lock,
        hasher=Argon2PasswordHasher(time_cost=1, memory_cost=1024, parallelism=1),
    )
    grant = await owner.issue_auth_grant("enroll")
    enrollment = await owner.enroll(grant.token, "habib", PASSWORD)
    app = create_app(
        load_config(root=tmp_path, env_file=None),
        auth=AuthManager(token="launch"),
        owner_auth=owner,
    )
    app.state.projects = projects
    app.state.session = _SessionStub()
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(
            transport=transport, base_url="http://127.0.0.1", follow_redirects=False
        ) as client:
            client.cookies.set(SESSION_COOKIE, enrollment.session.token)
            yield client, app, projects, owner
    finally:
        await db.close()


async def test_reset_requires_exact_name_and_creates_active_successor(tmp_path: Path) -> None:
    async with _client(tmp_path) as (client, app, projects, _owner):
        project_id = await projects.store.create(
            name="Kairo", repos=["C:/src/kairo"], settings={"label": "Coding"}
        )
        await projects.activate(project_id)

        mismatch = await client.post(
            f"/api/projects/{project_id}/reset",
            json={"confirmation": "kairo", "retain_repositories": True},
            headers=ORIGIN,
        )
        assert mismatch.status_code == 400
        assert (await projects.store.get(project_id)).status == "active"

        reset = await client.post(
            f"/api/projects/{project_id}/reset",
            json={"confirmation": "Kairo", "retain_repositories": True},
            headers=ORIGIN,
        )
        assert reset.status_code == 200 and reset.json()["ok"] is True
        successor_id = reset.json()["successor_project_id"]
        assert (await projects.store.get(project_id)).status == "archived"
        successor = await projects.store.get(successor_id)
        assert successor is not None and successor.repos == ("C:/src/kairo",)
        assert projects.current().project_id == successor_id
        assert app.state.session.switches == [successor_id]


async def test_reset_rejects_non_fresh_login_session(tmp_path: Path) -> None:
    async with _client(tmp_path) as (client, _app, projects, owner):
        project_id = await projects.store.create(name="Kairo")
        logged_in = await owner.login("habib", PASSWORD)
        assert logged_in is not None
        await owner.db.execute(
            "UPDATE owner_sessions SET step_up_until = '2000-01-01T00:00:00+00:00'"
        )
        await owner.db.commit()
        client.cookies.clear()
        client.cookies.set(SESSION_COOKIE, logged_in.session.token)

        response = await client.post(
            f"/api/projects/{project_id}/reset",
            json={"confirmation": "Kairo", "retain_repositories": False},
            headers=ORIGIN,
        )
        assert response.status_code == 403
        assert response.json()["message"] == "password step-up required"
        assert (await projects.store.get(project_id)).status == "active"


async def test_reset_rejects_a_stale_same_context_revision_after_aba(tmp_path: Path) -> None:
    async with _client(tmp_path) as (client, app, projects, _owner):
        project_a = await projects.store.create(name="Kairo")
        project_b = await projects.store.create(name="Other")
        connections = ConnectionManager(clock=lambda: 0.0)
        sessions = SessionStore(projects.store.db, projects.store.lock)

        def make_session(workspace) -> UiSession:
            return UiSession(
                loop=SimpleNamespace(),
                connections=connections,
                sessions=sessions,
                project_id=workspace.project.project_id,
            )

        registry = UiWorkspaceRegistry(
            connections=connections,
            make_session=make_session,
            projects=projects,
        )
        owner_session = client.cookies.get(SESSION_COOKIE)
        connection = connections.register(_Socket(), owner_session=owner_session)
        workspace = await registry.attach(connection, owner_session=owner_session)
        app.state.connections = connections
        app.state.workspaces = registry

        async with registry.transition_lock:
            await workspace.select_project(project_a)
            registry.refresh_context(workspace)
            old_context = workspace.context
            old_revision = workspace.context_revision
            await sessions.save_messages(
                old_context.session_id,
                [{"role": "user", "content": "ABA authority anchor"}],
            )
            await workspace.select_project(project_b)
            registry.refresh_context(workspace)
            assert await workspace.resume(old_context.session_id)
            registry.refresh_context(workspace)

        assert workspace.context == old_context
        assert workspace.context_revision > old_revision
        stale_headers = {
            **ORIGIN,
            WORKSPACE_HEADER: workspace.workspace_id,
            EXPECTED_SESSION_HEADER: str(old_context.session_id),
            EXPECTED_PROJECT_HEADER: str(old_context.project_id),
            EXPECTED_CONTEXT_REVISION_HEADER: str(old_revision),
        }
        response = await client.post(
            f"/api/projects/{project_a}/reset",
            json={"confirmation": "Kairo", "retain_repositories": False},
            headers=stale_headers,
        )
        assert response.status_code == 409
        assert (await projects.store.get(project_a)).status == "active"


async def test_reset_rolls_back_successor_and_every_workspace_when_session_insert_fails(
    tmp_path: Path, monkeypatch
) -> None:
    async with _client(tmp_path) as (client, app, projects, _owner):
        project_id = await projects.store.create(name="Atomic Reset")
        connections = ConnectionManager(clock=lambda: 0.0)
        sessions = SessionStore(projects.store.db, projects.store.lock)

        def make_session(workspace) -> UiSession:
            return UiSession(
                loop=SimpleNamespace(),
                connections=connections,
                sessions=sessions,
                project_id=workspace.project.project_id,
            )

        registry = UiWorkspaceRegistry(
            connections=connections,
            make_session=make_session,
            projects=projects,
        )
        owner_session = client.cookies.get(SESSION_COOKIE)
        workspace_a = await registry.attach(
            connections.register(_Socket(), owner_session=owner_session),
            owner_session=owner_session,
        )
        workspace_b = await registry.attach(
            connections.register(_Socket(), owner_session=owner_session),
            owner_session=owner_session,
        )
        await workspace_a.select_project(project_id)
        await workspace_b.select_project(project_id)
        registry.refresh_context(workspace_a)
        registry.refresh_context(workspace_b)
        app.state.connections = connections
        app.state.workspaces = registry
        before_contexts = [
            (item.context, item.context_revision) for item in (workspace_a, workspace_b)
        ]
        before_sessions = (
            await (await projects.store.db.execute("SELECT COUNT(*) FROM sessions")).fetchone()
        )[0]
        before_projects = (
            await (await projects.store.db.execute("SELECT COUNT(*) FROM projects")).fetchone()
        )[0]

        original_insert = sessions.create_session_in_transaction
        inserts = 0

        async def fail_second_insert(*args, **kwargs):
            nonlocal inserts
            inserts += 1
            if inserts == 2:
                raise OSError("injected successor-session failure")
            return await original_insert(*args, **kwargs)

        monkeypatch.setattr(sessions, "create_session_in_transaction", fail_second_insert)
        response = await client.post(
            f"/api/projects/{project_id}/reset",
            json={"confirmation": "Atomic Reset", "retain_repositories": False},
            headers={
                **ORIGIN,
                WORKSPACE_HEADER: workspace_a.workspace_id,
                EXPECTED_SESSION_HEADER: str(workspace_a.context.session_id),
                EXPECTED_PROJECT_HEADER: str(project_id),
                EXPECTED_CONTEXT_REVISION_HEADER: str(workspace_a.context_revision),
            },
        )

        assert response.status_code == 503
        predecessor = await projects.store.get(project_id)
        assert predecessor is not None and predecessor.status == "active"
        assert [
            (item.context, item.context_revision) for item in (workspace_a, workspace_b)
        ] == before_contexts
        after_sessions = (
            await (await projects.store.db.execute("SELECT COUNT(*) FROM sessions")).fetchone()
        )[0]
        after_projects = (
            await (await projects.store.db.execute("SELECT COUNT(*) FROM projects")).fetchone()
        )[0]
        reset_events = (
            await (
                await projects.store.db.execute("SELECT COUNT(*) FROM project_reset_events")
            ).fetchone()
        )[0]
        assert (after_sessions, after_projects, reset_events) == (
            before_sessions,
            before_projects,
            0,
        )


async def test_reset_atomically_moves_all_workspaces_without_live_session_allocation(
    tmp_path: Path, monkeypatch
) -> None:
    async with _client(tmp_path) as (client, app, projects, _owner):
        project_id = await projects.store.create(name="Atomic Reset Success")
        connections = ConnectionManager(clock=lambda: 0.0)
        sessions = SessionStore(projects.store.db, projects.store.lock)

        def make_session(workspace) -> UiSession:
            return UiSession(
                loop=SimpleNamespace(),
                connections=connections,
                sessions=sessions,
                project_id=workspace.project.project_id,
            )

        registry = UiWorkspaceRegistry(
            connections=connections,
            make_session=make_session,
            projects=projects,
        )
        owner_session = client.cookies.get(SESSION_COOKIE)
        workspace_a = await registry.attach(
            connections.register(_Socket(), owner_session=owner_session),
            owner_session=owner_session,
        )
        workspace_b = await registry.attach(
            connections.register(_Socket(), owner_session=owner_session),
            owner_session=owner_session,
        )
        await workspace_a.select_project(project_id)
        await workspace_b.select_project(project_id)
        registry.refresh_context(workspace_a)
        registry.refresh_context(workspace_b)
        app.state.connections = connections
        app.state.workspaces = registry

        async def forbidden_live_allocation(_project_id):
            raise AssertionError("destructive reset must use transaction-owned inserts")

        monkeypatch.setattr(workspace_a.session, "allocate_session", forbidden_live_allocation)
        monkeypatch.setattr(workspace_b.session, "allocate_session", forbidden_live_allocation)
        response = await client.post(
            f"/api/projects/{project_id}/reset",
            json={"confirmation": "Atomic Reset Success", "retain_repositories": False},
            headers={
                **ORIGIN,
                WORKSPACE_HEADER: workspace_a.workspace_id,
                EXPECTED_SESSION_HEADER: str(workspace_a.context.session_id),
                EXPECTED_PROJECT_HEADER: str(project_id),
                EXPECTED_CONTEXT_REVISION_HEADER: str(workspace_a.context_revision),
            },
        )

        assert response.status_code == 200 and response.json()["ok"] is True
        successor_id = response.json()["successor_project_id"]
        assert (await projects.store.get(project_id)).status == "archived"
        assert workspace_a.context.project_id == workspace_b.context.project_id == successor_id
        assert workspace_a.context.session_id != workspace_b.context.session_id
        for workspace in (workspace_a, workspace_b):
            meta = await sessions.get_meta(workspace.context.session_id)
            assert meta is not None and meta.project_id == successor_id and not meta.archived
            assert await sessions.load_messages(workspace.context.session_id) == []
