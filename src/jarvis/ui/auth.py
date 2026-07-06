"""Authentication for the workstation UI (Phase 8) — the private-admin-console contract.

A localhost port is NOT a private surface: it is reachable by any local process, and via
CSRF / DNS rebinding by any web page the browser visits. So the UI earns TTY-equivalent
authority or refuses to serve (ADR-0008 §2):

* a **per-launch token**, exchanged once for an opaque server-side **session**;
* a **Host allowlist** (anti DNS-rebinding) and an **Origin check** (anti-CSRF);
* the token is compared in constant time, never logged, never echoed by a route.

This module is pure policy + state (no framework types), so every rule is unit-testable.
"""

from __future__ import annotations

import secrets
from urllib.parse import urlsplit

from jarvis.config import _LOOPBACK_HOSTS

#: Name of the session cookie set after a successful token exchange.
SESSION_COOKIE = "kairo_session"


def _host_only(authority: str) -> str:
    """Strip an optional ``:port`` (and IPv6 brackets) from a Host authority.

    ``127.0.0.1:8787`` -> ``127.0.0.1`` · ``[::1]:8787`` -> ``::1`` · ``localhost`` -> as-is.
    """
    authority = authority.strip()
    if authority.startswith("["):  # bracketed IPv6, e.g. [::1]:8787
        return authority[1:].split("]", 1)[0]
    if authority.count(":") == 1:  # host:port (IPv4 or hostname) — strip the port
        return authority.rsplit(":", 1)[0]
    return authority  # bare host, or bare IPv6 (multiple colons, no port) — leave as-is


def host_allowed(host_header: str) -> bool:
    """True iff the Host header names a loopback host. Empty/foreign ⇒ False (fail-closed).

    This is the DNS-rebinding defense: a page at ``http://attacker.test`` that has rebound
    its name to 127.0.0.1 still sends ``Host: attacker.test``, which is refused here."""
    return bool(host_header) and _host_only(host_header) in _LOOPBACK_HOSTS


def origin_allowed(origin_header: str) -> bool:
    """True iff the Origin is loopback. Empty Origin ⇒ False (fail-closed) — the calls that
    require an Origin (mutations, the WS handshake) must present a loopback one; this is the
    CSRF defense even if a cookie's scope somehow leaks."""
    if not origin_header:
        return False
    return (urlsplit(origin_header).hostname or "") in _LOOPBACK_HOSTS


class AuthManager:
    """Per-launch token + an in-memory set of live session ids.

    The token is minted once per process and printed once (Task 9); a browser exchanges it
    for an opaque session id held only here (never persisted). Sessions live for the process
    lifetime — a single-user local app, so there is no session store to secure on disk."""

    def __init__(self, token: str | None = None) -> None:
        #: ≥128-bit URL-safe launch token (token_urlsafe(32) ≈ 256 bits).
        self.launch_token = token or secrets.token_urlsafe(32)
        self._sessions: set[str] = set()

    def check_token(self, token: str | None) -> bool:
        """Constant-time compare against the launch token (no early-exit timing leak)."""
        if not token:
            return False
        return secrets.compare_digest(token, self.launch_token)

    def mint_session(self) -> str:
        """Create and record a new opaque session id (returned to set as a cookie)."""
        sid = secrets.token_urlsafe(32)
        self._sessions.add(sid)
        return sid

    def is_valid_session(self, sid: str | None) -> bool:
        return bool(sid) and sid in self._sessions

    def revoke(self, sid: str) -> None:
        self._sessions.discard(sid)
