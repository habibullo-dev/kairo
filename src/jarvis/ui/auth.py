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


def _parse_host_authority(host_header: str) -> tuple[str, int | None] | None:
    """Parse a Host authority into ``(host, explicit_port)`` without guessing a scheme.

    Bracketed IPv6 is handled explicitly; a bare IPv6 loopback remains accepted for compatibility
    with the existing local-host contract, but only bracketed IPv6 may carry a port. Invalid ports
    and malformed suffixes are rejected at the common Host boundary.
    """
    authority = host_header.strip()
    if not authority:
        return None
    if authority.startswith("["):
        end = authority.find("]")
        if end < 1:
            return None
        host, rest = authority[1:end], authority[end + 1 :]
        if not rest:
            return host.lower(), None
        if not rest.startswith(":"):
            return None
        port_text = rest[1:]
    elif authority.count(":") == 0:
        return authority.lower(), None
    elif authority.count(":") == 1:
        host, port_text = authority.rsplit(":", 1)
    else:
        # A bare IPv6 address has no port. Brackets are required when a port is present.
        return authority.lower(), None

    if not host or not port_text.isdecimal():
        return None
    port = int(port_text)
    if not 1 <= port <= 65535:
        return None
    return host.lower(), port


def host_allowed(host_header: str) -> bool:
    """True iff the Host header names a loopback host. Empty/foreign ⇒ False (fail-closed).

    This is the DNS-rebinding defense: a page at ``http://attacker.test`` that has rebound
    its name to 127.0.0.1 still sends ``Host: attacker.test``, which is refused here."""
    parsed = _parse_host_authority(host_header)
    return parsed is not None and parsed[0] in _LOOPBACK_HOSTS


def _http_scheme(scheme: str) -> str | None:
    """Normalize an HTTP or WebSocket request scheme to its Origin scheme."""
    return {"http": "http", "ws": "http", "https": "https", "wss": "https"}.get(
        scheme.lower()
    )


def _target_authority(host_header: str, scheme: str) -> tuple[str, int] | None:
    """Parse the actual request authority into a normalized ``(host, port)`` pair.

    ``Host`` is an HTTP authority rather than a URL, so parse bracketed IPv6 explicitly and
    preserve the existing acceptance of a bare IPv6 loopback address. Invalid or non-loopback
    authorities fail closed here even though ``host_allowed`` remains the first perimeter check.
    """
    target_scheme = _http_scheme(scheme)
    if target_scheme is None:
        return None
    default_port = 443 if target_scheme == "https" else 80
    parsed = _parse_host_authority(host_header)
    if parsed is None:
        return None
    host, port = parsed
    return host, port if port is not None else default_port


def origin_allowed(origin_header: str, *, host_header: str, scheme: str) -> bool:
    """True iff ``Origin`` is the exact loopback origin serving this request.

    The session cookie is ``SameSite=Strict``, whose browser notion of a site does not isolate
    ports. Accepting any loopback Origin would therefore let a page on another local port drive
    an authenticated Gate request. Mutations and WebSocket handshakes instead require the same
    scheme, host, and normalized port as the request's own Host authority. Malformed or
    path-bearing Origins are refused rather than normalized.
    """
    if (
        not origin_header
        or "?" in origin_header
        or "#" in origin_header
        or not host_allowed(host_header)
    ):
        return False
    target_scheme = _http_scheme(scheme)
    target = _target_authority(host_header, scheme)
    if target_scheme is None or target is None:
        return False
    target_host, _target_port = target
    if target_host not in _LOOPBACK_HOSTS:
        return False
    try:
        origin = urlsplit(origin_header)
        origin_port = origin.port
    except ValueError:
        return False
    if (
        origin.scheme != target_scheme
        or not origin.netloc
        or origin.username is not None
        or origin.password is not None
        or origin.path
        or origin.query
        or origin.fragment
    ):
        return False
    origin_host = (origin.hostname or "").lower()
    if origin_host not in _LOOPBACK_HOSTS:
        return False
    default_port = 443 if origin.scheme == "https" else 80
    return (origin_host, origin_port if origin_port is not None else default_port) == target


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
