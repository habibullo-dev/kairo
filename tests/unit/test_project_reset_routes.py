"""Owner-only, step-up-gated project reset route."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

import httpx

from jarvis.config import load_config
from jarvis.persistence.db import connect
from jarvis.projects import ProjectService, ProjectStore
from jarvis.ui.auth import SESSION_COOKIE, AuthManager
from jarvis.ui.owner_auth import Argon2PasswordHasher, OwnerAuthService
from jarvis.ui.server import create_app

PASSWORD = "A unique owner passphrase 2026!"
ORIGIN = {"origin": "http://127.0.0.1"}


class _SessionStub:
    def __init__(self) -> None:
        self.project_id: int | None = None
        self.switches: list[int | None] = []

    def start_new_session(self, project_id: int | None) -> None:
        self.project_id = project_id
        self.switches.append(project_id)


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
