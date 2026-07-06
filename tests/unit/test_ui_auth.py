"""UI auth + connection policy (Phase 8, Task 2) — pure units, no framework.

The private-admin-console rules (ADR-0008 §2/§3/§5) are deliberately plain functions +
in-memory state so every rule is testable without a server: Host/Origin loopback checks,
constant-time token exchange, opaque sessions, and the liveness/surface tracking that later
makes an approval resolvable only from a live, watching client.
"""

from __future__ import annotations

from jarvis.ui.auth import AuthManager, host_allowed, origin_allowed
from jarvis.ui.connections import ConnectionManager

# --- Host allowlist (anti DNS-rebinding) -----------------------------------


def test_host_allowed_loopback() -> None:
    for h in ("127.0.0.1", "127.0.0.1:8787", "localhost", "localhost:8787", "[::1]:8787", "::1"):
        assert host_allowed(h), h


def test_host_rejected_foreign() -> None:
    # A rebound attacker name still sends its own Host header — refused (fail-closed).
    for h in ("", "evil.com", "attacker.test:8787", "0.0.0.0", "192.168.1.10", "10.0.0.5"):
        assert not host_allowed(h), h


# --- Origin check (anti-CSRF) ----------------------------------------------


def test_origin_allowed_loopback() -> None:
    assert origin_allowed("http://127.0.0.1:8787")
    assert origin_allowed("http://localhost:8787")


def test_origin_rejected_foreign_or_empty() -> None:
    assert not origin_allowed("")  # empty ⇒ refused (fail-closed) for the calls that need it
    assert not origin_allowed("http://evil.com")
    assert not origin_allowed("https://attacker.test:8787")


# --- token / session -------------------------------------------------------


def test_token_check_constant_time_and_correct() -> None:
    auth = AuthManager(token="tok-GOOD")
    assert auth.check_token("tok-GOOD")
    assert not auth.check_token("tok-BAD")
    assert not auth.check_token("")
    assert not auth.check_token(None)


def test_session_mint_and_validate() -> None:
    auth = AuthManager(token="t")
    sid = auth.mint_session()
    assert auth.is_valid_session(sid)
    assert not auth.is_valid_session("not-a-session")
    assert not auth.is_valid_session(None)
    auth.revoke(sid)
    assert not auth.is_valid_session(sid)


def test_generated_token_is_long() -> None:
    # Default token is unguessable (≥128-bit); token_urlsafe(32) ≈ 43 url-safe chars.
    assert len(AuthManager().launch_token) >= 32


# --- connection liveness + surfaces (the substrate for approval/screen) ----


def test_liveness_window_flips_with_clock() -> None:
    now = [100.0]
    cm = ConnectionManager(heartbeat_seconds=15.0, clock=lambda: now[0])
    conn = cm.register(object())
    assert cm.is_live(conn)
    now[0] = 114.0
    assert cm.is_live(conn)  # within the window
    now[0] = 116.0
    assert not cm.is_live(conn)  # past it ⇒ dead (a stale tab cannot approve)
    cm.touch(conn)  # a heartbeat revives it
    assert cm.is_live(conn)


def test_dropped_connection_is_not_live() -> None:
    cm = ConnectionManager(heartbeat_seconds=15.0, clock=lambda: 0.0)
    conn = cm.register(object())
    cm.drop(conn)
    assert not cm.is_live(conn)


def test_has_live_surface_requires_live_and_mounted() -> None:
    now = [0.0]
    cm = ConnectionManager(heartbeat_seconds=10.0, clock=lambda: now[0])
    conn = cm.register(object())
    cm.set_surface(conn, "gate", mounted=True)
    assert cm.has_live_surface("gate")
    now[0] = 20.0  # heartbeat stale ⇒ surface no longer counts (client isn't watching)
    assert not cm.has_live_surface("gate")
    now[0] = 0.0
    cm.touch(conn)
    cm.set_surface(conn, "gate", mounted=False)  # unmounted ⇒ not available
    assert not cm.has_live_surface("gate")
