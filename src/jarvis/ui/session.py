"""UI turn engine + event stream (Phase 8, Task 4).

``UiSession`` is the workstation's equivalent of ``VoiceSession``: a peer of the REPL that
drives the same ``AgentLoop`` through the same seams. It streams every loop event to
connected clients (and a bounded ring buffer for the Trace screen) and shares the REPL/
background-runner turn lock, so a UI turn and a background job never interleave. It adds no
authority — the injected approver is the ``UIApprover`` (Task 3), so every ASK still stops on
the human at the Gate.

Events are serialized to versioned JSON here, so the frontend (and any future consumer) has
one stable shape. ``ToolDecision`` is included — the same denied-visible tap evals rely on —
so the Trace/Gate never hide a refused call. ``SubAgentEvent`` is unwrapped with the child's
title (nothing delegated is hidden).
"""

from __future__ import annotations

import asyncio
from collections import deque
from typing import TYPE_CHECKING

from jarvis.core.events import (
    SubAgentCompleted,
    SubAgentEvent,
    TextDelta,
    ToolDecision,
    ToolFinished,
    ToolStarted,
    TurnCompleted,
)
from jarvis.observability import get_logger

if TYPE_CHECKING:
    from jarvis.core.agent import AgentLoop, Event
    from jarvis.core.client import TurnResult
    from jarvis.core.context import ContextManager
    from jarvis.persistence.sessions import SessionStore
    from jarvis.ui.connections import ConnectionManager

EVENT_SCHEMA_VERSION = 1


def serialize_event(event: Event) -> dict:
    """Map a loop ``Event`` to a versioned JSON payload. Unknown events degrade to a typed
    stub rather than raising — the stream must never crash a turn."""
    base = {"schema_version": EVENT_SCHEMA_VERSION}
    if isinstance(event, TextDelta):
        return {**base, "type": "text_delta", "text": event.text}
    if isinstance(event, ToolDecision):
        return {
            **base,
            "type": "tool_decision",
            "name": event.name,
            "input": event.input,
            "gate_decision": event.gate_decision,
            "resolution": event.resolution,
        }
    if isinstance(event, ToolStarted):
        return {
            **base,
            "type": "tool_started",
            "id": event.id,
            "name": event.name,
            "input": event.input,
        }
    if isinstance(event, ToolFinished):
        return {
            **base,
            "type": "tool_finished",
            "id": event.id,
            "name": event.name,
            "is_error": event.is_error,
            "preview": event.preview,
        }
    if isinstance(event, TurnCompleted):
        return {
            **base,
            "type": "turn_completed",
            "text": event.text,
            "stop_reason": event.stop_reason,
        }
    if isinstance(event, SubAgentEvent):
        # Unwrap: the child's activity renders inline, tagged with its title (never hidden).
        return {
            **base,
            "type": "subagent_event",
            "agent_id": event.agent_id,
            "title": event.title,
            "inner": serialize_event(event.inner),
        }
    if isinstance(event, SubAgentCompleted):
        return {
            **base,
            "type": "subagent_completed",
            "agent_id": event.agent_id,
            "title": event.title,
            "status": event.status,
            "cost_usd": event.cost_usd,
            "usage": {
                "input_tokens": event.usage.input_tokens,
                "output_tokens": event.usage.output_tokens,
            },
        }
    return {**base, "type": "unknown", "repr": type(event).__name__}


class UiSession:
    """Runs one UI turn at a time, streaming events to clients + a ring buffer.

    Injectable (``loop``, ``connections``, ``turn_lock``) so it's testable with a FakeClient
    loop and a fake connection — no server, no keys. The real composition (client, registry,
    gate, memory, shared runner lock) is built by the CLI host in Task 9, mirroring
    ``build_voice_session``."""

    def __init__(
        self,
        *,
        loop: AgentLoop,
        connections: ConnectionManager,
        turn_lock: asyncio.Lock | None = None,
        ring_buffer_events: int = 2000,
        sessions: SessionStore | None = None,
        context_manager: ContextManager | None = None,
        project_id: int | None = None,
        log=None,
    ) -> None:
        self.loop = loop
        self.connections = connections
        # Shared with the REPL/BackgroundRunner when composed: a UI turn is an interactive
        # turn and must not interleave a background job.
        self.turn_lock = turn_lock or asyncio.Lock()
        self.messages: list[dict] = []  # the conversation, accumulated across turns
        self.ring: deque[dict] = deque(maxlen=ring_buffer_events)
        self.log = log or get_logger("jarvis.ui.session")
        self._current: asyncio.Task | None = None
        self._pushes: set[asyncio.Task] = set()  # strong refs so pushes aren't GC'd mid-flight
        # Persistence (Phase 10 Task 2): the UI conversation is a real interactive session —
        # lazily created on the first turn, saved each turn, resumable. ``project_id`` scopes
        # the session for its lifetime (None == global; Task 3 wires the active project).
        self.sessions = sessions
        self.context_manager = context_manager
        self.project_id = project_id
        self.session_id: int | None = None

    def _emit(self, event: Event) -> None:
        """EventSink: record to the ring buffer and best-effort push to live clients. Sync
        (the loop calls it synchronously); the broadcast is scheduled as a task."""
        payload = serialize_event(event)
        self.ring.append(payload)
        task = asyncio.create_task(self.connections.broadcast({"kind": "event", **payload}))
        self._pushes.add(task)
        task.add_done_callback(self._pushes.discard)

    async def handle_text(self, text: str) -> TurnResult:
        """Run one turn to completion under the turn lock. Deterministic entry point for
        tests; the route uses :meth:`submit` to fire-and-forget.

        The session row is created lazily on the first turn (kind interactive, scoped to
        ``project_id``) and the full conversation is persisted after the turn — all inside
        the held turn lock, so a background job can't interleave the DB write."""
        async with self.turn_lock:
            if self.sessions is not None and self.session_id is None:
                self.session_id = await self.sessions.create_session(project_id=self.project_id)
            turn_messages = [*self.messages, {"role": "user", "content": text}]
            result = await self.loop.run_turn(turn_messages, on_event=self._emit)
            self.messages = result.messages
            await self._persist()
            return result

    async def _persist(self) -> None:
        """Save the conversation + frozen compaction state for the current session. A save
        failure is logged, never fatal — mirrors ``Repl._persist``."""
        if self.sessions is None or self.session_id is None:
            return
        try:
            await self.sessions.save_messages(self.session_id, self.messages)
            if self.context_manager is not None:
                summary, cut = self.context_manager.state()
                await self.sessions.save_compaction(self.session_id, summary, cut)
        except Exception as exc:  # noqa: BLE001 - a save failure must not kill the session
            self.log.warning("ui_persist_failed", error=str(exc))

    async def resume(self, session_id: int) -> bool:
        """Load a past session's messages + frozen compaction into the live loop (mirrors
        the REPL ``--resume`` mechanics). Refuses while a turn is in flight. Returns False
        if there's no store or the session has no transcript."""
        if self.sessions is None or self.busy:
            return False
        async with self.turn_lock:
            history = await self.sessions.load_messages(session_id)
            if not history:
                return False
            self.messages = history
            self.session_id = session_id
            if self.context_manager is not None:
                summary, cut = await self.sessions.load_compaction(session_id)
                self.context_manager.restore(summary, cut)
        return True

    def submit(self, text: str) -> bool:
        """Start a turn in the background (events flow over WS). Returns False if a turn is
        already in flight — one interactive turn at a time, like the REPL prompt."""
        if self._current is not None and not self._current.done():
            return False
        self._current = asyncio.create_task(self._run(text))
        return True

    async def _run(self, text: str) -> None:
        try:
            await self.handle_text(text)
        except asyncio.CancelledError:
            await self.connections.broadcast({"kind": "turn_cancelled"})
            raise
        except Exception as exc:  # noqa: BLE001 - a crashed turn is a message, not a dead server
            self.log.warning("ui_turn_error", error=repr(exc))
            await self.connections.broadcast({"kind": "turn_error", "error": str(exc)})

    def start_new_session(self, project_id: int | None) -> None:
        """Begin a fresh conversation under a (possibly new) project scope. A session is
        bound to one project for its life (reflection/promotion attribute to it), so a
        project switch starts over rather than re-tagging the current transcript."""
        self.messages = []
        self.session_id = None
        self.project_id = project_id

    def cancel(self) -> bool:
        """Cancel the in-flight turn (Ctrl-C parity). Returns True if one was cancelled."""
        if self._current is not None and not self._current.done():
            self._current.cancel()
            return True
        return False

    @property
    def busy(self) -> bool:
        return self._current is not None and not self._current.done()
