"""The Phase-15.5 conversation-lifecycle routes: model select + new / rename / archive chat.

All four are human-authority UI-state ops (the sessions/pin mold — no tool, no executor, no Gate
reach): model selection is Anthropic-only (fail-closed 400 otherwise); new starts a fresh scoped
chat (409 while a turn runs); rename/archive are chat metadata (archive is a status flip, never a
delete). Keyless TestClient with a real SessionStore + a minimal UiSession over a temp DB."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from jarvis.config import load_config
from jarvis.core import AgentLoop, FakeClient, build_system
from jarvis.permissions import PermissionGate, Policy
from jarvis.persistence import SessionStore
from jarvis.persistence.db import connect
from jarvis.tools import ToolContext, ToolExecutor, ToolRegistry
from jarvis.ui.auth import SESSION_COOKIE, AuthManager
from jarvis.ui.readmodels import UiServices
from jarvis.ui.server import create_app
from jarvis.ui.session import UiSession
from jarvis.ui.state import InteractiveModelState

_OPEN: list = []
_TS = "2026-03-01T00:00:00+00:00"


@pytest.fixture(autouse=True)
async def _close():
    yield
    while _OPEN:
        await _OPEN.pop().close()


async def _client(tmp_path: Path):
    cfg = load_config(root=tmp_path, env_file=None)
    db = await connect(tmp_path / "s.db")
    _OPEN.append(db)
    lock = asyncio.Lock()
    store = SessionStore(db, lock)
    sid = await store.create_session(title="First chat", kind="interactive")
    await store.save_messages(sid, [{"role": "user", "content": "hi"}])
    reg = ToolRegistry()
    reg.discover("jarvis.tools.builtin", ToolContext(config=cfg))
    auth = AuthManager(token="tok")
    app = create_app(cfg, auth=auth)
    loop = AgentLoop(
        client=FakeClient([]), registry=reg, executor=ToolExecutor(),
        gate=PermissionGate(Policy(), tmp_path), config=cfg, system=build_system(),
    )
    app.state.session = UiSession(loop=loop, connections=app.state.connections, sessions=store)
    app.state.services = UiServices(sessions=store)
    app.state.interactive_models = InteractiveModelState(cfg.models.main)
    return TestClient(app, base_url="http://127.0.0.1"), auth, store, sid


def _hdr(auth: AuthManager, *, post: bool = False) -> dict[str, str]:
    h = {"cookie": f"{SESSION_COOKIE}={auth.mint_session()}"}
    if post:
        h["origin"] = "http://127.0.0.1"  # loopback Origin (anti-CSRF), checked before auth
    return h


# --- model selection -------------------------------------------------------
async def test_model_route_switches_within_allowlist_and_reflects_in_models(tmp_path: Path) -> None:
    client, auth, _store, _sid = await _client(tmp_path)
    r = client.post("/api/model", json={"model": "claude-sonnet-5"}, headers=_hdr(auth, post=True))
    assert r.status_code == 200 and r.json()["model"] == "claude-sonnet-5"
    models = client.get("/api/models", headers=_hdr(auth)).json()
    assert models["current"] == "claude-sonnet-5"
    assert next(m for m in models["models"] if m["current"])["id"] == "claude-sonnet-5"
    # externals are listed but NOT selectable (private-context pin)
    assert models["external"] and all(not e["selectable"] for e in models["external"])


async def test_model_route_rejects_non_anthropic(tmp_path: Path) -> None:
    client, auth, _store, _sid = await _client(tmp_path)
    r = client.post("/api/model", json={"model": "gpt-5.2"}, headers=_hdr(auth, post=True))
    assert r.status_code == 400
    assert client.get("/api/models", headers=_hdr(auth)).json()["current"] != "gpt-5.2"


# --- new chat --------------------------------------------------------------
async def test_new_chat_resets_the_live_session(tmp_path: Path) -> None:
    client, auth, _store, _sid = await _client(tmp_path)
    client_app_session = client.app.state.session
    client_app_session.session_id = 42  # simulate being inside an existing chat
    r = client.post("/api/sessions/new", headers=_hdr(auth, post=True))
    assert r.status_code == 200 and r.json()["ok"] is True
    assert client_app_session.session_id is None  # a fresh conversation (lazily created next turn)


async def test_new_chat_409_while_a_turn_is_in_flight(tmp_path: Path) -> None:
    client, auth, _store, _sid = await _client(tmp_path)

    class _Busy:  # a turn is running — the loop state must not change mid-turn
        busy = True

        def start_new_session(self, _pid):
            raise AssertionError("must not start a new session while busy")

    client.app.state.session = _Busy()
    r = client.post("/api/sessions/new", headers=_hdr(auth, post=True))
    assert r.status_code == 409


# --- rename / archive (metadata; archive never deletes) --------------------
async def test_rename_route(tmp_path: Path) -> None:
    client, auth, store, sid = await _client(tmp_path)
    r = client.post(f"/api/sessions/{sid}/rename", json={"title": "Renamed"},
                    headers=_hdr(auth, post=True))
    assert r.status_code == 200 and r.json()["ok"] is True
    assert (await store.get_meta(sid)).title == "Renamed"
    blank = client.post(f"/api/sessions/{sid}/rename", json={"title": "   "},
                        headers=_hdr(auth, post=True))
    assert blank.status_code == 400  # a blank title is refused


async def test_archive_route_hides_without_deleting(tmp_path: Path) -> None:
    client, auth, store, sid = await _client(tmp_path)
    r = client.post(f"/api/sessions/{sid}/archive", json={"archived": True},
                    headers=_hdr(auth, post=True))
    assert r.status_code == 200 and r.json()["ok"] is True
    meta = await store.get_meta(sid)
    assert meta is not None and meta.archived is True and meta.message_count == 1  # kept
    assert sid not in {m.id for m in await store.list_sessions()}  # gone from the default list
    client.post(f"/api/sessions/{sid}/archive", json={"archived": False},
                headers=_hdr(auth, post=True))
    assert sid in {m.id for m in await store.list_sessions()}  # reversible
