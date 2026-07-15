"""Intent-lifecycle routes over HTTP (Phase 12 Task 8): the human-only approval queue.

Keyless TestClient with a real IntentStore/journal + a FAKE Google client (no network, no live
account). Pins: approve EXECUTES the stored write (the only path that writes), reject/undo close
the loop, a non-pending approve 409s, and the parameterized GET leaks no secret.

POSTs carry a loopback Origin (mutating routes are Origin-checked, anti-CSRF).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from kira.actions.intents import IntentKind, IntentStore
from kira.actions.journal import ConnectorWriteJournal
from kira.actions.requests import CalendarCreateRequest, request_to_dict
from kira.config import load_config
from kira.connectors.base import ConnectorRegistry
from kira.persistence.db import connect
from kira.ui.auth import SESSION_COOKIE, AuthManager
from kira.ui.readmodels import UiServices
from kira.ui.server import create_app

_OPEN: list = []


@pytest.fixture(autouse=True)
async def _close():
    yield
    while _OPEN:
        await _OPEN.pop().close()


class _FakeClient:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    async def post_json(self, url: str, *, json_body: dict, params: dict | None = None) -> dict:
        self.calls.append(("POST", url))
        return {"id": "evt-1", "htmlLink": "https://cal/evt-1"}

    async def delete(self, url: str, *, params: dict | None = None) -> None:
        self.calls.append(("DELETE", url))

    async def get_json(self, url: str, *, params: dict | None = None) -> dict:
        return {"id": "evt-1", "summary": "x", "start": {"dateTime": "t", "timeZone": "UTC"}}

    async def patch_json(self, url: str, *, json_body: dict, params: dict | None = None) -> dict:
        return {"id": "evt-1"}


async def _client(tmp_path: Path):
    cfg = load_config(root=tmp_path, env_file=None)
    db = await connect(tmp_path / "i.db")
    _OPEN.append(db)
    intents = IntentStore(db)
    journal = ConnectorWriteJournal(db)
    fake = _FakeClient()
    auth = AuthManager(token="tok")
    app = create_app(cfg, auth=auth)
    app.state.services = UiServices(
        intents=intents, write_journal=journal, connectors=ConnectorRegistry(google=fake)
    )
    return TestClient(app, base_url="http://127.0.0.1"), auth, intents, fake


def _hdr(auth: AuthManager, *, post: bool = False) -> dict[str, str]:
    h = {"cookie": f"{SESSION_COOKIE}={auth.mint_session()}"}
    if post:
        h["origin"] = "http://127.0.0.1"
    return h


async def _previewed(intents: IntentStore, key: str = "k1") -> int:
    req = request_to_dict(
        CalendarCreateRequest(
            summary="Standup", start="2026-02-01T10:00:00", end="2026-02-01T10:15:00",
            timezone="America/New_York", attendees=("alice@example.com",),
        )
    )
    iid = await intents.create_draft(
        idempotency_key=key, provider="google", kind=IntentKind.CALENDAR_CREATE,
        request=req, summary="Create event: Standup", source="agent",
    )
    await intents.mark_previewed(
        iid, preview={"title": "Create event: Standup", "fields": [], "diff": [],
                      "notes": [], "warnings": []},
    )
    return iid


async def test_get_intents_lists_pending(tmp_path: Path) -> None:
    client, auth, intents, _fake = await _client(tmp_path)
    await _previewed(intents)
    r = client.get("/api/intents", headers=_hdr(auth))
    assert r.status_code == 200
    body = r.json()
    assert len(body["pending"]) == 1 and body["pending"][0]["kind"] == "calendar_create"
    assert body["recent"] == []


async def test_approve_executes_the_write(tmp_path: Path) -> None:
    client, auth, intents, fake = await _client(tmp_path)
    iid = await _previewed(intents)
    r = client.post(f"/api/intents/{iid}/approve", headers=_hdr(auth, post=True))
    assert r.status_code == 200 and r.json()["ok"] is True and r.json()["state"] == "executed"
    assert any(c[0] == "POST" and c[1].endswith("/events") for c in fake.calls)  # the write fired
    assert (await intents.get(iid)).state.value == "executed"


async def test_connector_write_audit_is_metadata_only(tmp_path: Path) -> None:
    client, auth, intents, _fake = await _client(tmp_path)
    iid = await _previewed(intents)
    client.post(f"/api/intents/{iid}/approve", headers=_hdr(auth, post=True))

    response = client.get("/api/connector-writes", headers=_hdr(auth))
    assert response.status_code == 200
    writes = response.json()["writes"]
    assert len(writes) == 1
    assert set(writes[0]) == {"id", "provider", "verb", "scope", "project_id", "status", "at"}
    # No remote/rollback/trace/egress handle or request/preview body crosses the read boundary.
    forbidden = {"remote_id", "rollback_ref", "egress_ref", "trace_id", "request", "preview"}
    assert not (forbidden & writes[0].keys())


async def test_approve_twice_conflicts(tmp_path: Path) -> None:
    client, auth, intents, _fake = await _client(tmp_path)
    iid = await _previewed(intents)
    client.post(f"/api/intents/{iid}/approve", headers=_hdr(auth, post=True))
    again = client.post(f"/api/intents/{iid}/approve", headers=_hdr(auth, post=True))
    assert again.status_code == 409  # already executed — not pending


async def test_reject_removes_from_pending(tmp_path: Path) -> None:
    client, auth, intents, fake = await _client(tmp_path)
    iid = await _previewed(intents)
    r = client.post(f"/api/intents/{iid}/reject", headers=_hdr(auth, post=True))
    assert r.status_code == 200 and r.json()["ok"] is True
    assert (await intents.get(iid)).state.value == "rejected"
    assert fake.calls == []  # nothing executed


async def test_undo_after_execute(tmp_path: Path) -> None:
    client, auth, intents, fake = await _client(tmp_path)
    iid = await _previewed(intents)
    client.post(f"/api/intents/{iid}/approve", headers=_hdr(auth, post=True))
    r = client.post(f"/api/intents/{iid}/undo", headers=_hdr(auth, post=True))
    assert r.status_code == 200 and r.json()["ok"] is True
    assert any(c[0] == "DELETE" for c in fake.calls)  # the create was cancelled


async def test_intent_detail_leaks_no_secret(tmp_path: Path) -> None:
    # Manual secret sweep for the PARAMETERIZED GET (the auto-sweep skips {param} routes).
    cfg = load_config(root=tmp_path, env_file=None)
    cfg.secrets = cfg.secrets.model_copy(update={"anthropic_api_key": "SECRET-CANARY-INTENT"})
    db = await connect(tmp_path / "s.db")
    _OPEN.append(db)
    intents = IntentStore(db)
    auth = AuthManager(token="SECRET-CANARY-TOKEN")
    app = create_app(cfg, auth=auth)
    app.state.services = UiServices(intents=intents, write_journal=ConnectorWriteJournal(db))
    iid = await _previewed(intents)
    client = TestClient(app, base_url="http://127.0.0.1")
    cookie = {"cookie": f"{SESSION_COOKIE}={auth.mint_session()}"}
    r = client.get(f"/api/intents/{iid}", headers=cookie)
    assert r.status_code == 200
    blob = r.text + str(dict(r.headers))
    assert "SECRET-CANARY-INTENT" not in blob
    assert "SECRET-CANARY-TOKEN" not in blob


async def test_intents_route_503_without_store(tmp_path: Path) -> None:
    cfg = load_config(root=tmp_path, env_file=None)
    auth = AuthManager(token="tok")
    app = create_app(cfg, auth=auth)
    app.state.services = UiServices()  # no intents store composed
    client = TestClient(app, base_url="http://127.0.0.1")
    assert client.get("/api/intents", headers=_hdr(auth)).status_code == 503
    assert client.get("/api/connector-writes", headers=_hdr(auth)).status_code == 503
