"""UI server core (Phase 8, Task 2) — the auth/transport floor via FastAPI's TestClient.

Load-bearing safety, all keyless: the auth matrix (no/wrong token, wrong Host, wrong Origin,
no session), the clean-URL token exchange, the hardening-header sweep (Referrer-Policy
present, NO CORS anywhere), the token never appearing in a response, and WS-handshake auth.
The base_url is loopback so the Host header passes the anti-rebinding guard; a foreign Host
is injected explicitly to prove the guard bites.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from jarvis.config import load_config
from jarvis.ui.auth import DEFAULT_SESSION_TTL_SECONDS, SESSION_COOKIE, AuthManager
from jarvis.ui.connections import ConnectionManager
from jarvis.ui.server import _handle_ws_message, create_app

TOKEN = "tok-GOOD-canary"


def _app(tmp_path: Path, *, token: str = TOKEN, base_url: str = "http://127.0.0.1"):
    config = load_config(root=tmp_path, env_file=None)
    auth = AuthManager(token=token)
    app = create_app(config, auth=auth)
    client = TestClient(app, base_url=base_url)
    return client, app, auth


def _cookie(auth: AuthManager) -> dict[str, str]:
    """A header dict carrying a freshly-minted valid session cookie (explicit, no jar)."""
    return {"cookie": f"{SESSION_COOKIE}={auth.mint_session()}"}


def _assert_hardened(resp) -> None:
    assert resp.headers.get("referrer-policy") == "no-referrer"
    assert "default-src 'self'" in resp.headers.get("content-security-policy", "")
    # NO CORS, ever — not even a wildcard.
    assert not any(k.lower().startswith("access-control-") for k in resp.headers)


# --- open vs. session-gated -------------------------------------------------


def test_health_is_open(tmp_path: Path) -> None:
    client, _app_, _auth = _app(tmp_path)
    r = client.get("/api/health")
    assert r.status_code == 200 and r.json()["app"] == "kairo"
    _assert_hardened(r)


def test_root_requires_session(tmp_path: Path) -> None:
    client, _app_, _auth = _app(tmp_path)
    r = client.get("/")
    assert r.status_code == 401
    _assert_hardened(r)  # even the refusal is hardened


def test_authed_root_served(tmp_path: Path) -> None:
    client, _app_, auth = _app(tmp_path)
    r = client.get("/", headers=_cookie(auth))
    assert r.status_code == 200 and "Kairo Workstation" in r.text


# --- token exchange: clean URL, cookie, no-store ----------------------------


def test_token_exchange_redirects_clean_and_sets_cookie(tmp_path: Path) -> None:
    client, _app_, _auth = _app(tmp_path)
    r = client.get(f"/?token={TOKEN}", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/"  # CLEAN — no ?token= in history
    assert TOKEN not in r.headers["location"]
    assert f"{SESSION_COOKIE}=" in r.headers.get("set-cookie", "")
    assert "httponly" in r.headers["set-cookie"].lower()
    assert "samesite=strict" in r.headers["set-cookie"].lower()
    assert f"Max-Age={DEFAULT_SESSION_TTL_SECONDS}" in r.headers["set-cookie"]
    assert r.headers.get("cache-control") == "no-store"
    _assert_hardened(r)


def test_bad_token_refused(tmp_path: Path) -> None:
    client, _app_, _auth = _app(tmp_path)
    r = client.get("/?token=WRONG", follow_redirects=False)
    assert r.status_code == 401
    _assert_hardened(r)


def test_exchange_roundtrip_lets_you_in(tmp_path: Path) -> None:
    # A real browser flow: hit the tokened url, follow the redirect, land authenticated.
    client, _app_, _auth = _app(tmp_path)
    r = client.get(f"/?token={TOKEN}")  # follow_redirects=True (default)
    assert r.status_code == 200 and "Kairo Workstation" in r.text


# --- Host allowlist (anti DNS-rebinding) ------------------------------------


def test_foreign_host_rejected(tmp_path: Path) -> None:
    client, _app_, _auth = _app(tmp_path)
    r = client.get("/api/health", headers={"host": "attacker.test"})
    assert r.status_code == 400
    _assert_hardened(r)


# --- Origin check on mutations (anti-CSRF) ----------------------------------


def test_mutation_foreign_origin_rejected(tmp_path: Path) -> None:
    client, _app_, auth = _app(tmp_path)
    r = client.post("/api/anything", headers={**_cookie(auth), "origin": "http://evil.com"})
    assert r.status_code == 403  # blocked before routing/auth — CSRF wall
    _assert_hardened(r)


def test_mutation_same_loopback_host_different_port_rejected(tmp_path: Path) -> None:
    # SameSite cookies are not port-scoped, so another local port must not drive a Gate mutation.
    client, _app_, auth = _app(tmp_path)
    r = client.post(
        "/api/anything", headers={**_cookie(auth), "origin": "http://127.0.0.1:3000"}
    )
    assert r.status_code == 403


def test_mutation_exact_loopback_origin_with_port_reaches_route(tmp_path: Path) -> None:
    client, _app_, auth = _app(tmp_path, base_url="http://127.0.0.1:8787")
    r = client.post(
        "/api/turn/cancel",
        headers={**_cookie(auth), "origin": "http://127.0.0.1:8787"},
    )
    assert r.status_code == 200


def test_mutation_loopback_origin_but_no_session(tmp_path: Path) -> None:
    client, _app_, _auth = _app(tmp_path)
    r = client.post("/api/anything", headers={"origin": "http://127.0.0.1"})
    assert r.status_code == 401  # origin ok, but no session ⇒ still refused


# --- header sweep + secret absence ------------------------------------------


def test_hardening_headers_and_no_secret_on_every_response(tmp_path: Path) -> None:
    client, _app_, auth = _app(tmp_path)
    responses = [
        client.get("/api/health"),
        client.get("/"),  # 401
        client.get(f"/?token={TOKEN}", follow_redirects=False),  # 303
        client.get("/?token=WRONG", follow_redirects=False),  # 401
        client.get("/api/health", headers={"host": "attacker.test"}),  # 400
        client.get("/", headers=_cookie(auth)),  # 200
    ]
    for r in responses:
        _assert_hardened(r)
        # The launch token never appears in any body or header (Task 2 harness; the full
        # registered-route sweep lands in Task 5).
        blob = r.text + "\n" + "\n".join(f"{k}: {v}" for k, v in r.headers.items())
        assert TOKEN not in blob


# --- WebSocket handshake auth -----------------------------------------------


# The TestClient forces Host: testserver on WS handshakes, so each test sets a loopback
# Host explicitly — otherwise the anti-rebinding guard would reject for the wrong reason.
def _ws_headers(auth: AuthManager) -> dict[str, str]:
    return {
        "host": "127.0.0.1",
        "origin": "http://127.0.0.1",
        "cookie": f"{SESSION_COOKIE}={auth.mint_session()}",
    }


def test_ws_without_session_refused(tmp_path: Path) -> None:
    client, _app_, _auth = _app(tmp_path)
    headers = {"host": "127.0.0.1", "origin": "http://127.0.0.1"}  # loopback, but NO cookie
    with pytest.raises(WebSocketDisconnect):  # noqa: SIM117 - the connect itself must fail
        with client.websocket_connect("/ws", headers=headers) as ws:
            ws.receive_json()


def test_ws_foreign_origin_refused(tmp_path: Path) -> None:
    client, _app_, auth = _app(tmp_path)
    bad = {**_ws_headers(auth), "origin": "http://evil.com"}  # valid session, foreign origin
    with pytest.raises(WebSocketDisconnect):  # noqa: SIM117
        with client.websocket_connect("/ws", headers=bad) as ws:
            ws.receive_json()


def test_ws_same_loopback_host_different_port_refused(tmp_path: Path) -> None:
    client, _app_, auth = _app(tmp_path)
    bad = {**_ws_headers(auth), "origin": "http://127.0.0.1:3000"}
    with pytest.raises(WebSocketDisconnect):  # noqa: SIM117
        with client.websocket_connect("/ws", headers=bad) as ws:
            ws.receive_json()


def test_ws_exact_loopback_origin_with_port_gets_hello(tmp_path: Path) -> None:
    client, _app_, auth = _app(tmp_path, base_url="http://127.0.0.1:8787")
    headers = {
        "host": "127.0.0.1:8787",
        "origin": "http://127.0.0.1:8787",
        "cookie": f"{SESSION_COOKIE}={auth.mint_session()}",
    }
    with client.websocket_connect("/ws", headers=headers) as ws:
        assert ws.receive_json()["type"] == "hello"


def test_ws_foreign_host_refused(tmp_path: Path) -> None:
    client, _app_, auth = _app(tmp_path)
    bad = {**_ws_headers(auth), "host": "attacker.test"}  # valid session, rebound host
    with pytest.raises(WebSocketDisconnect):  # noqa: SIM117
        with client.websocket_connect("/ws", headers=bad) as ws:
            ws.receive_json()


def test_ws_authed_gets_server_hello(tmp_path: Path) -> None:
    client, _app_, auth = _app(tmp_path)
    with client.websocket_connect("/ws", headers=_ws_headers(auth)) as ws:
        hello = ws.receive_json()
        assert hello["type"] == "hello" and "heartbeat_seconds" in hello


# --- WS message dispatch (pure; deterministic, no socket race) --------------


def test_ws_message_dispatch_tracks_surfaces_and_heartbeat() -> None:
    now = [0.0]
    cm = ConnectionManager(heartbeat_seconds=10.0, clock=lambda: now[0])
    conn = cm.register(object())
    _handle_ws_message(cm, conn, {"type": "hello", "surfaces": ["daily"]})
    assert conn.surfaces == {"daily"}
    _handle_ws_message(cm, conn, {"type": "surface", "surface": "gate", "mounted": True})
    assert cm.has_live_surface("gate")
    _handle_ws_message(cm, conn, {"type": "surface", "surface": "gate", "mounted": False})
    assert not cm.has_live_surface("gate")
    now[0] = 5.0
    _handle_ws_message(cm, conn, {"type": "heartbeat"})
    assert conn.last_beat == 5.0
    _handle_ws_message(cm, conn, {"type": "bogus"})  # unknown type ignored, no raise
