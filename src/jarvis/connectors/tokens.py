"""OAuth token custody: on-disk state + single-flight refresh.

The refresh token is the crown jewel — a durable credential worth more than the machine —
so it lives only under ``data/connectors/`` (on the hard sensitive-path floor, Task 1) and is
written atomically (temp + ``os.replace``, atomic on Windows and macOS) with best-effort 0600.

``TokenStore.access_token`` returns a valid bearer token, refreshing under an ``asyncio.Lock``
when it is within the skew of expiry — single-flight, so two concurrent callers trigger exactly
one refresh POST. A failed/`invalid_grant` refresh raises :class:`ConnectorAuthError` whose only
surfaced text is the friendly "use `uv run kira connect <provider>`" (A6).
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import tempfile
from collections.abc import Callable
from contextlib import suppress
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from jarvis.connectors.base import ConnectorAuthError
from jarvis.observability import get_logger

if TYPE_CHECKING:
    from jarvis.connectors.oauth import OAuthProvider

_log = get_logger("jarvis.connectors.tokens")

#: Refresh this many seconds *before* the access token actually expires, so a call in flight
#: doesn't race the boundary.
_SKEW_SECONDS = 120


def _utcnow() -> _dt.datetime:
    return _dt.datetime.now(_dt.UTC)


def _chmod_600(path: Path) -> None:
    """Best-effort owner-only perms. Real on POSIX; a no-op on Windows (ACL-based)."""
    with suppress(OSError, NotImplementedError):
        os.chmod(path, 0o600)


@dataclass
class TokenState:
    """A stored OAuth grant. ``expires_at`` / ``obtained_at`` are UTC ISO-8601 strings so the
    file round-trips as plain JSON. Never contains the client secret."""

    provider: str
    access_token: str
    refresh_token: str
    expires_at: str
    obtained_at: str
    scopes: list[str] = field(default_factory=list)
    token_type: str = "Bearer"

    def is_expired(self, *, now: Callable[[], _dt.datetime], skew: int = _SKEW_SECONDS) -> bool:
        try:
            exp = _dt.datetime.fromisoformat(self.expires_at)
        except ValueError:
            return True  # unparseable ⇒ treat as expired (fail-closed toward a refresh)
        return now() >= exp - _dt.timedelta(seconds=skew)


def read_token_state(path: str | Path) -> TokenState | None:
    """Load a :class:`TokenState` from disk, or None if absent/unreadable/malformed."""
    p = Path(path)
    if not p.exists():
        return None
    try:
        return TokenState(**json.loads(p.read_text(encoding="utf-8")))
    except (ValueError, TypeError, OSError):
        return None


def write_token_state(path: str | Path, state: TokenState) -> None:
    """Atomically write ``state`` to ``path`` (temp + os.replace), best-effort 0600."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(asdict(state), indent=2)
    fd, tmp = tempfile.mkstemp(dir=p.parent, prefix=".tok-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
        _chmod_600(Path(tmp))
        os.replace(tmp, p)  # atomic on both platforms
    finally:
        with suppress(OSError):
            if os.path.exists(tmp):
                os.remove(tmp)  # only reachable if replace failed
    _chmod_600(p)


class TokenStore:
    """Per-provider token storage + single-flight refresh.

    ``provider`` carries the token endpoint; ``client_id``/``client_secret`` authenticate the
    refresh. ``http`` (an ``httpx.AsyncClient``) and ``now`` are injectable for tests.
    """

    def __init__(
        self,
        path: str | Path,
        *,
        provider: OAuthProvider,
        client_id: str,
        client_secret: str,
        http: Any = None,
        now: Callable[[], _dt.datetime] = _utcnow,
        skew: int = _SKEW_SECONDS,
    ) -> None:
        self.path = Path(path)
        self.provider = provider
        self.client_id = client_id
        self.client_secret = client_secret
        self._http = http
        self._now = now
        self._skew = skew
        self._state: TokenState | None = None
        self._lock: Any = None  # created lazily so TokenStore is constructible off-loop

    def load(self) -> TokenState | None:
        return read_token_state(self.path)

    def save(self, state: TokenState) -> None:
        write_token_state(self.path, state)
        self._state = state

    def _get_lock(self):
        import asyncio

        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    async def access_token(self) -> str:
        """A valid bearer token, refreshing if within the skew of expiry (single-flight)."""
        state = self._state or self.load()
        if state is not None and not state.is_expired(now=self._now, skew=self._skew):
            self._state = state
            return state.access_token
        async with self._get_lock():
            # Re-check inside the lock: a concurrent caller may have just refreshed.
            state = self._state or self.load()
            if state is not None and not state.is_expired(now=self._now, skew=self._skew):
                return state.access_token
            if state is None or not state.refresh_token:
                raise ConnectorAuthError(self.provider.name)
            refreshed = await self._refresh(state.refresh_token)
            self.save(refreshed)
            return refreshed.access_token

    async def force_refresh(self) -> str:
        """Refresh now regardless of expiry — for a 401 on a token that hasn't expired yet
        (revoked/rotated). Single-flight under the same lock. Raises ConnectorAuthError if
        there is no refresh token."""
        async with self._get_lock():
            state = self._state or self.load()
            if state is None or not state.refresh_token:
                raise ConnectorAuthError(self.provider.name)
            refreshed = await self._refresh(state.refresh_token)
            self.save(refreshed)
            return refreshed.access_token

    async def _refresh(self, refresh_token: str) -> TokenState:
        # Deferred import breaks the tokens<->oauth cycle (oauth imports TokenState at load).
        from jarvis.connectors.oauth import refresh_token_grant

        return await refresh_token_grant(
            self.provider,
            client_id=self.client_id,
            client_secret=self.client_secret,
            refresh_token=refresh_token,
            http=self._http,
            now=self._now,
        )

    def status(self) -> dict[str, Any]:
        """Presence-only snapshot for Hub (never the token itself)."""
        state = self._state or self.load()
        if state is None:
            return {"connected": False, "needs_reconnect": True}
        return {
            "connected": True,
            "scopes": list(state.scopes),
            "expires_at": state.expires_at,
            "needs_reconnect": False,
        }
