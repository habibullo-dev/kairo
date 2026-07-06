"""Live WebSocket connection tracking for the workstation UI (Phase 8).

Liveness is load-bearing safety, not telemetry: an approval (and the voice screen) is
resolvable only from a client whose socket is *currently* connected and heartbeating within
``ui.heartbeat_seconds`` (ADR-0008 §3/§5). A cookie replay from a dead tab cannot approve,
because a dead tab has no live connection here.

The manager also tracks each connection's **currently-mounted surfaces** (streamed as the
client mounts/unmounts screens), so "the Gate surface is available" is a live fact, not a
one-time hello claim (amendment 4). The clock is injectable so tests can advance time and
watch liveness flip without sleeping.
"""

from __future__ import annotations

import secrets
import time
from collections.abc import Callable


class Connection:
    """One live WS client: its mounted surfaces and last heartbeat time."""

    def __init__(self, conn_id: str, ws: object, created: float) -> None:
        self.id = conn_id
        self.ws = ws
        self.surfaces: set[str] = set()
        self.last_beat: float = created


class ConnectionManager:
    """Registry of live WS connections. Owns the clock so liveness is deterministic in
    tests (inject ``clock``); defaults to ``time.monotonic``."""

    def __init__(
        self, *, heartbeat_seconds: float = 15.0, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self.heartbeat_seconds = heartbeat_seconds
        self._clock = clock
        self._conns: dict[str, Connection] = {}

    def register(self, ws: object) -> Connection:
        conn = Connection(secrets.token_urlsafe(8), ws, self._clock())
        self._conns[conn.id] = conn
        return conn

    def drop(self, conn: Connection) -> None:
        self._conns.pop(conn.id, None)

    def touch(self, conn: Connection) -> None:
        """Record a heartbeat (resets the liveness window)."""
        conn.last_beat = self._clock()

    def set_surface(self, conn: Connection, surface: str, mounted: bool) -> None:
        """Track a surface mount/unmount for this connection."""
        if mounted:
            conn.surfaces.add(surface)
        else:
            conn.surfaces.discard(surface)

    def is_live(self, conn: Connection) -> bool:
        """True iff this connection is still registered AND heartbeated within the window."""
        return conn.id in self._conns and (self._clock() - conn.last_beat) <= self.heartbeat_seconds

    def live(self) -> list[Connection]:
        now = self._clock()
        return [c for c in self._conns.values() if (now - c.last_beat) <= self.heartbeat_seconds]

    def has_live_surface(self, surface: str) -> bool:
        """True iff some *currently live* connection has ``surface`` mounted — the positive
        check behind screen availability (ADR-0008 §5)."""
        return any(surface in c.surfaces for c in self.live())
