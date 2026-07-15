"""Project routes over HTTP (Phase 10 Task 3): create / list / update / archive / select,
and the switch-starts-a-new-session contract. Keyless TestClient with a real ProjectService
over a temp DB and a session stub recording start_new_session calls.

POSTs carry a loopback Origin (mutating routes are Origin-checked, anti-CSRF)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from jarvis.config import load_config
from jarvis.persistence.db import connect
from jarvis.projects import ProjectService, ProjectStore
from jarvis.ui.auth import SESSION_COOKIE, AuthManager
from jarvis.ui.server import create_app

_OPEN: list = []


@pytest.fixture(autouse=True)
async def _close():
    yield
    while _OPEN:
        await _OPEN.pop().close()


class _SessionStub:
    """Records start_new_session / resume so route side effects are observable."""

    def __init__(self) -> None:
        self.project_id: int | None = None
        self.session_id: int | None = 99
        self.messages = [{"role": "user", "content": "old"}]
        self.busy = False
        self.switches: list = []

    def start_new_session(self, project_id: int | None) -> None:
        self.switches.append(project_id)
        self.project_id = project_id
        self.session_id = None
        self.messages = []


async def _client(tmp_path: Path):
    cfg = load_config(root=tmp_path, env_file=None)
    db = await connect(tmp_path / "p.db")
    _OPEN.append(db)
    projects = ProjectService(ProjectStore(db, asyncio.Lock()))
    auth = AuthManager(token="tok")
    app = create_app(cfg, auth=auth)
    app.state.projects = projects
    app.state.session = _SessionStub()
    return TestClient(app, base_url="http://127.0.0.1"), app, auth, projects


def _hdr(auth: AuthManager, *, post: bool = False) -> dict[str, str]:
    h = {"cookie": f"{SESSION_COOKIE}={auth.mint_session()}"}
    if post:
        h["origin"] = "http://127.0.0.1"
    return h


async def test_create_list_and_select(tmp_path: Path) -> None:
    client, app, auth, projects = await _client(tmp_path)
    # create
    r = client.post("/api/projects", json={"name": "Kira Web"}, headers=_hdr(auth, post=True))
    assert r.status_code == 200 and r.json()["ok"] is True
    pid = r.json()["id"]
    # list shows it, active is still global
    data = client.get("/api/projects", headers=_hdr(auth)).json()
    assert any(p["id"] == pid and p["name"] == "Kira Web" for p in data["projects"])
    assert data["active_project_id"] is None
    # select it → service active + session switched to a fresh conversation
    r = client.post("/api/projects/select", json={"project_id": pid}, headers=_hdr(auth, post=True))
    assert r.status_code == 200 and r.json()["active_project_id"] == pid
    assert projects.current().project_id == pid
    assert app.state.session.switches == [pid]  # started a new session under the project
    assert app.state.session.messages == []  # fresh conversation


async def test_select_unknown_is_404(tmp_path: Path) -> None:
    client, _app, auth, _p = await _client(tmp_path)
    r = client.post("/api/projects/select", json={"project_id": 999}, headers=_hdr(auth, post=True))
    assert r.status_code == 404


async def test_select_global_resets(tmp_path: Path) -> None:
    client, app, auth, projects = await _client(tmp_path)
    pid = client.post("/api/projects", json={"name": "P"}, headers=_hdr(auth, post=True)).json()[
        "id"
    ]
    client.post("/api/projects/select", json={"project_id": pid}, headers=_hdr(auth, post=True))
    r = client.post(
        "/api/projects/select", json={"project_id": None}, headers=_hdr(auth, post=True)
    )
    assert r.json()["active_project_id"] is None
    assert projects.current().project_id is None
    assert app.state.session.switches[-1] is None


async def test_update_and_slug_is_immutable(tmp_path: Path) -> None:
    client, _app, auth, projects = await _client(tmp_path)
    pid = client.post("/api/projects", json={"name": "P"}, headers=_hdr(auth, post=True)).json()[
        "id"
    ]
    await projects.activate(pid)  # the legacy REPL context must refresh without a scope switch
    ok = client.post(
        f"/api/projects/{pid}/update",
        json={"name": "Renamed", "description": "now described", "color": "#abc"},
        headers=_hdr(auth, post=True),
    )
    assert ok.status_code == 200 and ok.json()["ok"] is True
    # slug is not in the route's whitelist, so it's silently ignored (never changed) — the
    # security property is that a client cannot rewrite a project's stable slug via update.
    r = client.post(
        f"/api/projects/{pid}/update", json={"slug": "hijack"}, headers=_hdr(auth, post=True)
    )
    assert r.status_code == 200
    after = await projects.store.get(pid)
    assert after.slug == "p" and after.description == "now described"
    assert projects.current().name == "Renamed"  # metadata changed; project scope/session did not


async def test_update_rejects_malformed_json_columns(tmp_path: Path) -> None:
    client, _app, auth, _projects = await _client(tmp_path)
    pid = client.post("/api/projects", json={"name": "P"}, headers=_hdr(auth, post=True)).json()[
        "id"
    ]
    assert client.post(
        f"/api/projects/{pid}/update", json={"repos": "not-a-list"}, headers=_hdr(auth, post=True)
    ).status_code == 400
    assert client.post(
        f"/api/projects/{pid}/update", json={"settings": []}, headers=_hdr(auth, post=True)
    ).status_code == 400
    assert client.post(
        f"/api/projects/{pid}/update", json={"settings": {}}, headers=_hdr(auth, post=True)
    ).status_code == 400
    assert client.post(
        f"/api/projects/{pid}/update", json=[], headers=_hdr(auth, post=True)
    ).status_code == 400


async def test_archive_active_falls_back_to_global(tmp_path: Path) -> None:
    client, app, auth, projects = await _client(tmp_path)
    pid = client.post("/api/projects", json={"name": "P"}, headers=_hdr(auth, post=True)).json()[
        "id"
    ]
    client.post("/api/projects/select", json={"project_id": pid}, headers=_hdr(auth, post=True))
    r = client.post(f"/api/projects/{pid}/archive", headers=_hdr(auth, post=True))
    assert r.status_code == 200 and r.json()["ok"] is True
    # archiving the active project drops back to global + starts a fresh session
    assert projects.current().project_id is None
    assert app.state.session.switches[-1] is None
    # archived project no longer listed among active
    data = client.get("/api/projects", headers=_hdr(auth)).json()
    assert all(p["id"] != pid for p in data["projects"])


async def test_projects_503_without_service(tmp_path: Path) -> None:
    cfg = load_config(root=tmp_path, env_file=None)
    auth = AuthManager(token="tok")
    app = create_app(cfg, auth=auth)  # no projects service wired
    client = TestClient(app, base_url="http://127.0.0.1")
    assert client.get("/api/projects", headers=_hdr(auth)).status_code == 503
