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

import asyncio
import contextlib
import secrets
import time
from collections.abc import Awaitable, Callable

from kira.core.execution import ExecutionContext


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
        # Monotonic workspace selection epoch. Two visits to the same session/project pair are
        # different authorities, so queued frames must retain the revision captured at enqueue.
        self.context_revision: int | None = None
        # Starlette's WebSocket state machine and ASGI sender do not serialize concurrent sends.
        # Keep one narrow outbound channel per socket; other connections fan out independently.
        self._send_lock = asyncio.Lock()
        # Set synchronously before unregistering or waiting to close. A send that was already
        # queued on ``_send_lock`` must not outlive logout/recovery and reach a revoked browser.
        self._closing = False
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
        conn._closing = True
        self._conns.pop(conn.id, None)

    def get(self, conn_id: str) -> Connection | None:
        return self._conns.get(conn_id)

    def for_owner_session(self, owner_session: str) -> list[Connection]:
        """Snapshot every socket authenticated by one exact browser bearer."""
        return [conn for conn in self._conns.values() if conn.owner_session == owner_session]

    def all(self) -> list[Connection]:
        """Snapshot all registered sockets for owner-wide recovery invalidation."""
        return list(self._conns.values())

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
        context_revision: int = 1,
    ) -> None:
        """Bind a live socket to a server-owned workspace and its exact delivery context."""
        if conn._closing or conn.id not in self._conns or conn.owner_session != owner_session:
            raise ValueError("connection owner mismatch")
        conn.workspace_id = workspace_id
        conn.context = context
        conn.context_revision = context_revision

    def update_workspace_context(
        self,
        *,
        owner_session: str,
        workspace_id: str,
        context: ExecutionContext,
        context_revision: int = 1,
    ) -> None:
        """Atomically retarget every live socket attached to one browser workspace."""
        for conn in self._conns.values():
            if conn.owner_session == owner_session and conn.workspace_id == workspace_id:
                conn.context = context
                conn.context_revision = context_revision

    def has_live_workspace(self, *, owner_session: str, workspace_id: str) -> bool:
        """Whether this authenticated browser workspace currently has a live socket."""
        return any(
            conn.owner_session == owner_session and conn.workspace_id == workspace_id
            for conn in self.live()
        )

    def is_live(self, conn: Connection) -> bool:
        """True iff registered, not closing, and heartbeated within the liveness window."""
        return (
            not conn._closing
            and conn.id in self._conns
            and (self._clock() - conn.last_beat) <= self.heartbeat_seconds
        )

    def live(self) -> list[Connection]:
        now = self._clock()
        return [
            c
            for c in self._conns.values()
            if not c._closing and (now - c.last_beat) <= self.heartbeat_seconds
        ]

    def has_live_surface(self, surface: str, *, context: ExecutionContext | None = None) -> bool:
        """True iff some *currently live* connection has ``surface`` mounted — the positive
        check behind screen availability (ADR-0008 §5)."""
        return any(
            surface in c.surfaces and (context is None or c.context == context) for c in self.live()
        )

    async def send(self, conn: Connection, message: dict) -> None:
        """Best-effort serialized push to one connection."""
        async with conn._send_lock:
            # Re-check after acquiring the lock: drop/close may have happened while this send was
            # queued behind an earlier frame. The already-running frame may finish; queued work
            # loses authority immediately.
            if conn._closing or conn.id not in self._conns:
                return
            with contextlib.suppress(Exception):
                await conn.ws.send_json(message)

    async def close(self, conn: Connection, *, code: int) -> None:
        """Best-effort close ordered behind any in-flight send on this connection."""
        conn._closing = True
        try:
            async with asyncio.timeout(0.5):
                async with conn._send_lock:
                    with contextlib.suppress(Exception):
                        await conn.ws.close(code=code)
        except TimeoutError:
            pass

    async def _deliver_one(self, conn: Connection, payload: dict) -> None:
        """Bound one best-effort socket send independently of every other recipient."""
        try:
            async with asyncio.timeout(0.5):
                await self.send(conn, payload)
        except TimeoutError:
            pass

    async def _deliver(self, deliveries: list[tuple[Connection, dict]]) -> None:
        """Fan out concurrently so a stalled socket cannot starve another recipient."""
        await asyncio.gather(*(self._deliver_one(conn, payload) for conn, payload in deliveries))

    async def broadcast(self, message: dict) -> None:
        """Push to every live connection (best-effort). Used to announce a new pending
        approval; a client that connects later re-fetches via the REST read model."""
        await self._deliver([(conn, message) for conn in self.live()])

    def publish(self, context: ExecutionContext | None, message: dict) -> Awaitable[None]:
        """Deliver an attended-work envelope only to its exact session/project context.

        An absent context is intentionally dropped.  It must never degrade to ``broadcast``:
        doing so would turn a missing provenance value into a cross-chat disclosure. This method
        intentionally captures recipients synchronously: callers commonly pass its awaitable to
        ``create_task``, and a later A -> B -> A transition must not relabel the queued A frame.
        """
        if context is None:
            return self._deliver([])
        deliveries = [
            (
                conn,
                {
                    **message,
                    **context.to_wire(),
                    "context_revision": conn.context_revision,
                },
            )
            for conn in self.live()
            if conn.context == context and conn.context_revision is not None
        ]
        return self._deliver(deliveries)

    async def publish_project(self, project_id: int | None, message: dict) -> None:
        """Deliver background activity to the exact project scope of each live workspace.

        Unlike a process-wide broadcast, a project-scoped scheduler notice can contain a reminder
        payload, task title, error, or job-result excerpt. ``None`` is the global workspace's
        explicit scope, not a wildcard, so missing provenance never turns into a disclosure.
        """
        await self._deliver(
            [
                (conn, message)
                for conn in self.live()
                if conn.context is not None and conn.context.project_id == project_id
            ]
        )

    def publish_workspace(
        self,
        *,
        owner_session: str,
        workspace_id: str,
        context: ExecutionContext,
        message: dict,
    ) -> Awaitable[None]:
        """Deliver a workspace-lifecycle event (for example a project switch) exactly once.

        ``workspace_id`` is an opaque server-issued routing key, not authority.  It lets the
        browser accept a fresh context replacing its old one without ever accepting another
        tab's project-switch envelope.
        """
        payload = {**message, **context.to_wire(), "workspace_id": workspace_id}
        deliveries = [
            (conn, payload)
            for conn in self.live()
            if conn.owner_session == owner_session and conn.workspace_id == workspace_id
        ]
        return self._deliver(deliveries)
