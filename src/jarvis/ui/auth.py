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

import contextlib
import hashlib
import json
import math
import os
import secrets
import time
from collections.abc import Callable
from pathlib import Path
from urllib.parse import urlsplit

from jarvis.config import _LOOPBACK_HOSTS

#: Canonical session cookie plus the previous-brand alias accepted during migration.
SESSION_COOKIE = "kira_session"
LEGACY_SESSION_COOKIE = "kairo_session"

# A workstation login should survive ordinary browser/server restarts without becoming a
# permanent bearer credential.  Thirty days keeps the daily experience seamless while retaining
# an automatic revocation boundary.  Stored records contain only SHA-256 digests of the random
# cookie values, so the file cannot itself be replayed as a browser session.
DEFAULT_SESSION_TTL_SECONDS = 30 * 24 * 60 * 60


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
    """Per-launch token plus opaque, optionally durable browser sessions.

    The token is minted once per process and printed once (Task 9).  A browser exchanges it for
    an opaque session id.  Production supplies ``session_store_path`` so only a digest + expiry
    survives an ordinary Kira restart; tests and bare app construction remain in-memory by
    default.  Raw session ids and the launch token are never written to disk.
    """

    def __init__(
        self,
        token: str | None = None,
        *,
        session_store_path: Path | None = None,
        session_ttl_seconds: int = DEFAULT_SESSION_TTL_SECONDS,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if session_ttl_seconds <= 0:
            raise ValueError("session_ttl_seconds must be positive")
        #: ≥128-bit URL-safe launch token (token_urlsafe(32) ≈ 256 bits).
        self.launch_token = token or secrets.token_urlsafe(32)
        self.session_ttl_seconds = int(session_ttl_seconds)
        self._session_store_path = session_store_path
        self._clock = clock
        self._sessions: dict[str, float] = self._load_sessions()
        self._launch_token_consumed = False

    @staticmethod
    def _digest(sid: str) -> str:
        return hashlib.sha256(sid.encode("utf-8")).hexdigest()

    @staticmethod
    def _valid_digest(value: object) -> bool:
        if not isinstance(value, str) or len(value) != 64:
            return False
        return all(char in "0123456789abcdef" for char in value)

    def _load_sessions(self) -> dict[str, float]:
        """Load unexpired digest records; malformed/unreadable state fails closed."""
        path = self._session_store_path
        if path is None:
            return {}
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return {}
        records = raw.get("sessions") if isinstance(raw, dict) else None
        if not isinstance(records, dict):
            return {}
        now = self._clock()
        return {
            digest: float(expiry)
            for digest, expiry in records.items()
            if self._valid_digest(digest)
            and isinstance(expiry, (int, float))
            and not isinstance(expiry, bool)
            and float(expiry) > now
        }

    def _persist_sessions(self) -> None:
        """Atomically persist digest-only state; a disk failure leaves the live session usable."""
        path = self._session_store_path
        if path is None:
            return
        payload = json.dumps(
            {"version": 1, "sessions": self._sessions},
            sort_keys=True,
            separators=(",", ":"),
        )
        tmp = path.with_name(f".{path.name}.tmp")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(payload, encoding="utf-8")
            os.chmod(tmp, 0o600)
            tmp.replace(path)
        except OSError:
            # Authentication still fails closed after a restart: without a successfully persisted
            # digest, the browser must perform the one-time token exchange again.
            with contextlib.suppress(OSError):
                tmp.unlink(missing_ok=True)

    def _prune_expired(self) -> None:
        now = self._clock()
        expired = [digest for digest, expiry in self._sessions.items() if expiry <= now]
        if expired:
            for digest in expired:
                self._sessions.pop(digest, None)
            self._persist_sessions()

    def check_token(self, token: str | None) -> bool:
        """Constant-time compare against the launch token (no early-exit timing leak)."""
        if not token:
            return False
        return secrets.compare_digest(token, self.launch_token)

    def consume_token(self, token: str | None) -> bool:
        """Consume the per-process launch token exactly once.

        Owner-mode HTTP uses this only to mint a short-lived enrollment/recovery grant.  Legacy
        callers retain ``check_token`` until the owner UI handoff is activated.
        """
        valid = self.check_token(token)
        if self._launch_token_consumed or not valid:
            return False
        self._launch_token_consumed = True
        return True

    def mint_session(self) -> str:
        """Create and record a new opaque session id (returned only as an HttpOnly cookie)."""
        self._prune_expired()
        sid = secrets.token_urlsafe(32)
        self._sessions[self._digest(sid)] = self._clock() + self.session_ttl_seconds
        self._persist_sessions()
        return sid

    def is_valid_session(self, sid: str | None) -> bool:
        if not sid:
            return False
        digest = self._digest(sid)
        expiry = self._sessions.get(digest)
        if expiry is None:
            return False
        if expiry <= self._clock():
            self._sessions.pop(digest, None)
            self._persist_sessions()
            return False
        return True

    def session_cookie_max_age(self, sid: str | None) -> int | None:
        """Return the remaining browser lifetime for a valid legacy-named session.

        Reusing the exact bearer under the canonical cookie name must not extend its durable
        server-side expiry.  ``None`` deliberately covers unknown and expired sessions.
        """
        if not sid:
            return None
        digest = self._digest(sid)
        expiry = self._sessions.get(digest)
        if expiry is None:
            return None
        remaining = expiry - self._clock()
        if remaining <= 0:
            self._sessions.pop(digest, None)
            self._persist_sessions()
            return None
        return max(1, math.ceil(remaining))

    def revoke(self, sid: str) -> None:
        if self._sessions.pop(self._digest(sid), None) is not None:
            self._persist_sessions()
