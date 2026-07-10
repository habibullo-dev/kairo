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

import contextlib
import secrets
import time
from collections.abc import Callable

from jarvis.core.execution import ExecutionContext


class Connection:
    """One live WS client: identity, mounted surfaces, and server-bound execution scope."""

    def __init__(
        self, conn_id: str, ws: object, created: float, *, owner_session: str | None = None
    ) -> None:
        self.id = conn_id
        self.ws = ws
        # The authenticated UI cookie that opened this socket.  It is only used server-side to
        # reject an HTTP request that presents another browser's workspace id.
        self.owner_session = owner_session
        # A server-issued browser-workspace id (persists across a reconnect in sessionStorage).
        self.workspace_id: str | None = None
        # Bound only by the server-owned workspace registry.  ``None`` is unbound, never a
        # wildcard selector for sensitive delivery.
        self.context: ExecutionContext | None = None
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

    def register(self, ws: object, *, owner_session: str | None = None) -> Connection:
        conn = Connection(secrets.token_urlsafe(8), ws, self._clock(), owner_session=owner_session)
        self._conns[conn.id] = conn
        return conn

    def drop(self, conn: Connection) -> None:
        self._conns.pop(conn.id, None)

    def get(self, conn_id: str) -> Connection | None:
        return self._conns.get(conn_id)

    def touch(self, conn: Connection) -> None:
        """Record a heartbeat (resets the liveness window)."""
        conn.last_beat = self._clock()

    def set_surface(self, conn: Connection, surface: str, mounted: bool) -> None:
        """Track a surface mount/unmount for this connection."""
        if mounted:
            conn.surfaces.add(surface)
        else:
            conn.surfaces.discard(surface)

    def bind_workspace(
        self,
        conn: Connection,
        *,
        owner_session: str,
        workspace_id: str,
        context: ExecutionContext,
    ) -> None:
        """Bind a live socket to a server-owned workspace and its exact delivery context."""
        if conn.id not in self._conns or conn.owner_session != owner_session:
            raise ValueError("connection owner mismatch")
        conn.workspace_id = workspace_id
        conn.context = context

    def update_workspace_context(
        self, *, owner_session: str, workspace_id: str, context: ExecutionContext
    ) -> None:
        """Atomically retarget every live socket attached to one browser workspace."""
        for conn in self._conns.values():
            if conn.owner_session == owner_session and conn.workspace_id == workspace_id:
                conn.context = context

    def has_live_workspace(self, *, owner_session: str, workspace_id: str) -> bool:
        """Whether this authenticated browser workspace currently has a live socket."""
        return any(
            conn.owner_session == owner_session and conn.workspace_id == workspace_id
            for conn in self.live()
        )

    def is_live(self, conn: Connection) -> bool:
        """True iff this connection is still registered AND heartbeated within the window."""
        return conn.id in self._conns and (self._clock() - conn.last_beat) <= self.heartbeat_seconds

    def live(self) -> list[Connection]:
        now = self._clock()
        return [c for c in self._conns.values() if (now - c.last_beat) <= self.heartbeat_seconds]

    def has_live_surface(self, surface: str, *, context: ExecutionContext | None = None) -> bool:
        """True iff some *currently live* connection has ``surface`` mounted — the positive
        check behind screen availability (ADR-0008 §5)."""
        return any(
            surface in c.surfaces and (context is None or c.context == context)
            for c in self.live()
        )

    async def send(self, conn: Connection, message: dict) -> None:
        """Best-effort push to one connection (a dead socket is swallowed, not fatal)."""
        with contextlib.suppress(Exception):
            await conn.ws.send_json(message)

    async def broadcast(self, message: dict) -> None:
        """Push to every live connection (best-effort). Used to announce a new pending
        approval; a client that connects later re-fetches via the REST read model."""
        for conn in self.live():
            await self.send(conn, message)

    async def publish(self, context: ExecutionContext | None, message: dict) -> None:
        """Deliver an attended-work envelope only to its exact session/project context.

        An absent context is intentionally dropped.  It must never degrade to ``broadcast``:
        doing so would turn a missing provenance value into a cross-chat disclosure.
        """
        if context is None:
            return
        payload = {**message, **context.to_wire()}
        for conn in self.live():
            if conn.context == context:
                await self.send(conn, payload)

    async def publish_workspace(
        self,
        *,
        owner_session: str,
        workspace_id: str,
        context: ExecutionContext,
        message: dict,
    ) -> None:
        """Deliver a workspace-lifecycle event (for example a project switch) exactly once.

        ``workspace_id`` is an opaque server-issued routing key, not authority.  It lets the
        browser accept a fresh context replacing its old one without ever accepting another
        tab's project-switch envelope.
        """
        payload = {**message, **context.to_wire(), "workspace_id": workspace_id}
        for conn in self.live():
            if conn.owner_session == owner_session and conn.workspace_id == workspace_id:
                await self.send(conn, payload)
