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
import contextlib
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
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
from jarvis.core.execution import ExecutionContext, bind_execution_context
from jarvis.observability import get_logger

if TYPE_CHECKING:
    from jarvis.core.agent import AgentLoop, Event
    from jarvis.core.client import TurnResult
    from jarvis.core.context import ContextManager
    from jarvis.persistence.sessions import SessionStore
    from jarvis.ui.connections import ConnectionManager

EVENT_SCHEMA_VERSION = 2  # v2: Phase-10B orchestration lifecycle events (started/stage/agent/
#                                round/completed), broadcast by the OrchestrationController.


@dataclass(frozen=True)
class PreparedUiSessionResume:
    """Durable resume state loaded under the shared turn lock but not yet made live."""

    session_id: int
    project_id: int | None
    messages: list[dict]
    compaction: tuple[str | None, int] | None


@dataclass(frozen=True)
class PreparedUiSessionNew:
    """Fallible new-chat cleanup completed before a batch lifecycle transaction commits."""

    prior_compaction: tuple[str | None, int] | None


def initial_chat_title(text: str) -> str | None:
    """Return a compact, local first-message title, never a numeric chat placeholder.

    This deliberately avoids a hidden second provider request merely to name a chat: it would
    add cost and send the first message to a model before the user has chosen to run a turn.  A
    later explicit AI-title refinement can build on this stable, human-editable baseline.
    """
    value = " ".join(text.split()).lstrip("# ").strip()
    if not value:
        return None
    lowered = value.casefold()
    for prefix in (
        "please ",
        "can you ",
        "could you ",
        "help me ",
        "i need to ",
        "i want to ",
        "let's ",
        "lets ",
    ):
        if lowered.startswith(prefix):
            value = value[len(prefix) :].strip()
            break
    # A first sentence is usually the request; avoiding a long pasted prompt keeps the shelf
    # skimmable.  Preserve the user's spelling/case and never derive a title from tool output.
    for separator in ("?", "!", ".", "\n"):
        head = value.split(separator, 1)[0].strip()
        if head:
            value = head
            break
    if not value:
        return None
    return value if len(value) <= 72 else f"{value[:71].rstrip()}…"


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
        # A child's prompt, streamed text, tool input, and previews are a separate untrusted
        # channel.  The parent browser needs progress only, so expose the small activity shape
        # consumed by conversation.js — never recursively serialize the child event.
        return {
            **base,
            "type": "subagent_event",
            "agent_id": event.agent_id,
            "title": event.title,
            "inner": _serialize_subagent_activity(event.inner),
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


def _serialize_subagent_activity(event: Event) -> dict:
    """Metadata-only child activity for the attended parent stream.

    The parent still gets enough information to say which tool is running/was approved/failed or
    that the child is drafting/finished.  Unlike :func:`serialize_event`, this MUST NOT carry any
    child body or tool argument/result because the child transcript is deliberately isolated.
    """
    base = {"schema_version": EVENT_SCHEMA_VERSION}
    if isinstance(event, TextDelta):
        return {**base, "type": "text_delta"}
    if isinstance(event, ToolDecision):
        return {
            **base,
            "type": "tool_decision",
            "name": event.name,
            "resolution": event.resolution,
        }
    if isinstance(event, ToolStarted):
        return {**base, "type": "tool_started", "name": event.name}
    if isinstance(event, ToolFinished):
        return {
            **base,
            "type": "tool_finished",
            "name": event.name,
            "is_error": event.is_error,
        }
    if isinstance(event, TurnCompleted):
        return {**base, "type": "turn_completed", "stop_reason": event.stop_reason}
    return {**base, "type": "unknown"}


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
        self._turn_generation = 0
        self._pushes: set[asyncio.Task] = set()  # strong refs so pushes aren't GC'd mid-flight
        # Persistence (Phase 10 Task 2): the UI conversation is a real interactive session —
        # lazily created on the first turn, saved each turn, resumable. ``project_id`` scopes
        # the session for its lifetime (None == global; Task 3 wires the active project).
        self.sessions = sessions
        self.context_manager = context_manager
        self.project_id = project_id
        self.session_id: int | None = None
        # Persistence is an explicit user-facing lifecycle, not an assumption. The value is a
        # small safe vocabulary for the UI; database/OS error details stay in local logs.
        self.persistence_state = "new"  # new | saving | saved | failed
        # Last-turn cost truth is held with the live workspace, never inferred from another
        # session's ledger rows. ``None`` means unavailable/unpriced, not free.
        self.last_turn_cost_usd: float | None = None
        self.last_turn_model: str | None = None
        self.last_turn_provider: str | None = None
        self.turn_budget_usd: float | None = getattr(
            getattr(loop, "chat_limits", None), "hard_stop_usd_per_turn", None
        )

    def _context(self) -> ExecutionContext | None:
        """Return this chat's persisted delivery identity, never a wildcard selector."""
        if self.session_id is None:
            return None
        return ExecutionContext(session_id=self.session_id, project_id=self.project_id)

    async def ensure_session(self) -> int | None:
        """Allocate the durable row before attended work begins.

        Empty rows remain hidden in chat lists, but their stable ids prevent fresh chats from
        sharing the unsafe ``(None, project_id)`` delivery key while an async task is starting.
        """
        if self.sessions is None:
            return None
        async with self.turn_lock:
            if self.session_id is None:
                self.session_id = await self.sessions.create_session(project_id=self.project_id)
            return self.session_id

    async def allocate_session(self, project_id: int | None) -> int | None:
        """Allocate a durable empty row without mutating the live conversation.

        Workspace transitions use this prepare step so storage failure leaves the old session,
        transcript, project, and compaction state intact.
        """
        if self.sessions is None:
            return None
        async with self.turn_lock:
            return await self.sessions.create_session(project_id=project_id)

    def _emit(self, event: Event, context: ExecutionContext | None = None) -> None:
        """Record an event and schedule delivery to its exact persisted context.

        A bare in-process session can still retain its ring buffer, but no event without an
        execution context is allowed to fan out to browser sockets.
        """
        payload = serialize_event(event)
        self.ring.append(payload)
        task = asyncio.create_task(
            self.connections.publish(context or self._context(), {"kind": "event", **payload})
        )
        self._pushes.add(task)
        task.add_done_callback(self._pushes.discard)

    async def handle_text(
        self, text: str, *, context: ExecutionContext | None = None
    ) -> TurnResult:
        """Run one turn to completion under the turn lock. Deterministic entry point for
        tests; the route uses :meth:`submit` to fire-and-forget.

        The session row is created lazily on the first turn (kind interactive, scoped to
        ``project_id``) and the full conversation is persisted after the turn — all inside
        the held turn lock, so a background job can't interleave the DB write."""
        async with self.turn_lock:
            if self.sessions is not None and self.session_id is None:
                self.session_id = await self.sessions.create_session(project_id=self.project_id)
            context = context or self._context()
            turn_messages = [*self.messages, {"role": "user", "content": text}]
            # The loop mutates the list it receives while constructing an API transcript.  Keep
            # this pre-call snapshot so a normal exception never persists an unmatched tool_use
            # or a partial assistant block.
            loop_messages = list(turn_messages)
            try:
                if context is None:
                    # No persisted context means no socket delivery.  This is only the bare/test
                    # composition; the workstation registry eagerly calls ensure_session().
                    result = await self.loop.run_turn(loop_messages, on_event=self._emit)
                else:
                    with bind_execution_context(context):
                        result = await self.loop.run_turn(
                            loop_messages, on_event=lambda event: self._emit(event, context)
                        )
            except asyncio.CancelledError:
                await self._persist_cancelled_turn(turn_messages, text, context)
                raise
            except Exception:
                await self._persist_failed_turn(turn_messages, text, context)
                raise
            self.messages = result.messages
            self.last_turn_cost_usd = getattr(result, "cost_usd", None)
            self.last_turn_model = getattr(result, "model", None)
            self.last_turn_provider = getattr(result, "provider", None)
            self.turn_budget_usd = getattr(result, "budget_usd", self.turn_budget_usd)
            await self._persist(context, initial_title=initial_chat_title(text))
            return result

    async def _persist_cancelled_turn(
        self, turn_messages: list[dict], text: str, context: ExecutionContext | None
    ) -> None:
        """Keep a cancelled request resumable before surfacing its websocket cancellation."""
        saved = getattr(self.loop, "cancelled_messages", None) or turn_messages
        self.messages = [*saved, {"role": "assistant", "content": "(stopped)"}]
        # A cancelled turn has no reliable completed-turn cost or routing metadata.
        self.last_turn_cost_usd = None
        self.last_turn_model = None
        self.last_turn_provider = None
        await self._persist(context, initial_title=initial_chat_title(text))

    async def _persist_failed_turn(
        self, turn_messages: list[dict], text: str, context: ExecutionContext | None
    ) -> None:
        """Make an ordinary failed turn durable without retaining partial model protocol state."""
        self.messages = [
            *turn_messages,
            {"role": "assistant", "content": "(unable to complete this turn)"},
        ]
        # A failed request has no safe completed-turn cost or routing metadata.
        self.last_turn_cost_usd = None
        self.last_turn_model = None
        self.last_turn_provider = None
        await self._persist(context, initial_title=initial_chat_title(text))

    async def _set_persistence_state(self, state: str, context: ExecutionContext | None) -> None:
        self.persistence_state = state
        if context is not None:
            await self.connections.publish(context, {"kind": "session_persistence", "state": state})

    async def _persist(
        self, context: ExecutionContext | None = None, *, initial_title: str | None = None
    ) -> None:
        """Save the conversation + frozen compaction state for the current session. A save
        failure is logged, never fatal — mirrors ``Repl._persist``."""
        session_id = context.session_id if context is not None else self.session_id
        if self.sessions is None or session_id is None:
            return
        await self._set_persistence_state("saving", context)
        try:
            await self.sessions.save_messages(session_id, self.messages)
            if self.context_manager is not None:
                summary, cut = self.context_manager.state()
                await self.sessions.save_compaction(session_id, summary, cut)
            if initial_title:
                # Atomic blank-only update: a person may rename while a turn is saving, and
                # their chosen title must win.  A failure here is non-fatal because the transcript
                # has already been durably saved.
                try:
                    await self.sessions.set_title_if_missing(session_id, initial_title)
                except Exception as exc:  # noqa: BLE001 - title polish must not invalidate a save
                    self.log.warning("ui_initial_title_failed", error_type=type(exc).__name__)
            await self._set_persistence_state("saved", context)
        except Exception as exc:  # noqa: BLE001 - a save failure must not kill the session
            self.log.warning("ui_persist_failed", error_type=type(exc).__name__)
            await self._set_persistence_state("failed", context)

    async def prepare_resume(self, session_id: int) -> PreparedUiSessionResume | None:
        """Load and validate a resume target without mutating the live conversation.

        The shared turn lock protects the durable snapshot.  Callers may then take their own
        short-lived authority lock and commit synchronously; no authority lock ever needs to
        wait for this lock while an attended turn is paused at the Gate.
        """
        if self.sessions is None or self._turn_busy_now():
            return None
        async with self.turn_lock:
            if self._turn_busy_now():
                return None
            meta = await self.sessions.get_meta(session_id)
            if meta is None or meta.kind != "interactive" or meta.archived:
                return None
            history = await self.sessions.load_messages(session_id)
            if not history:
                return None
            if self._turn_busy_now():
                return None
            compaction = (
                await self.sessions.load_compaction(session_id)
                if self.context_manager is not None
                else None
            )
            if self._turn_busy_now():
                return None
            return PreparedUiSessionResume(
                session_id=session_id,
                project_id=meta.project_id,
                messages=history,
                compaction=compaction,
            )

    def commit_resume(
        self,
        prepared: PreparedUiSessionResume,
        *,
        before_commit: Callable[[], None] | None = None,
    ) -> bool:
        """Commit a prepared resume without yielding; roll back compaction if its hook fails."""
        if self.busy:
            return False
        if self.context_manager is not None:
            prior_compaction = self.context_manager.state()
            try:
                self.context_manager.restore(*(prepared.compaction or (None, 0)))
                if before_commit is not None:
                    before_commit()
            except Exception:
                with contextlib.suppress(Exception):
                    self.context_manager.restore(*prior_compaction)
                raise
        elif before_commit is not None:
            before_commit()
        self.messages = prepared.messages
        self.session_id = prepared.session_id
        self.project_id = prepared.project_id
        self.persistence_state = "saved"
        self.last_turn_cost_usd = None
        self.last_turn_model = None
        self.last_turn_provider = None
        return True

    async def resume(
        self, session_id: int, *, before_commit: Callable[[], None] | None = None
    ) -> bool:
        """Load and atomically commit a past interactive session."""
        prepared = await self.prepare_resume(session_id)
        return bool(
            prepared is not None and self.commit_resume(prepared, before_commit=before_commit)
        )

    def _turn_busy_now(self) -> bool:
        """Re-read task state across awaits; it may change while another coroutine runs."""
        return self.busy

    def submit(self, text: str) -> bool:
        """Start a turn in the background (events flow over WS). Returns False if a turn is
        already in flight — one interactive turn at a time, like the REPL prompt."""
        if self._current is not None and not self._current.done():
            return False
        # Freeze scope before yielding.  A route must not be able to retag a queued turn by
        # switching projects or resuming a different chat while this task is underway.
        self._turn_generation += 1
        self._current = asyncio.create_task(self._run(text, self._context()))
        return True

    async def _run(self, text: str, context: ExecutionContext | None) -> None:
        try:
            await self.handle_text(text, context=context)
        except asyncio.CancelledError:
            await self.connections.publish(context, {"kind": "turn_cancelled"})
            raise
        except Exception as exc:  # noqa: BLE001 - a crashed turn is a message, not a dead server
            self.log.warning("ui_turn_error", error_type=type(exc).__name__)
            await self.connections.publish(context, {"kind": "turn_error"})

    def start_new_session(
        self,
        project_id: int | None,
        *,
        session_id: int | None = None,
        before_commit: Callable[[], None] | None = None,
    ) -> None:
        """Begin a fresh conversation under a (possibly new) project scope. A session is
        bound to one project for its life (reflection/promotion attribute to it), so a
        project switch starts over rather than re-tagging the current transcript."""
        prepared = self.prepare_new_session_commit(before_commit=before_commit)
        self.commit_prepared_new_session(
            prepared,
            project_id=project_id,
            session_id=session_id,
        )

    def prepare_new_session_commit(
        self, *, before_commit: Callable[[], None] | None = None
    ) -> PreparedUiSessionNew:
        """Run every fallible synchronous new-chat step without publishing new authority.

        Destructive fan-out routes preflight all affected workspaces before opening their shared
        SQLite transaction. Once that transaction commits, :meth:`commit_prepared_new_session`
        is assignment-only and cannot strand a later workspace on archived authority.
        """
        if self.busy:
            raise RuntimeError("busy")
        prior_compaction = (
            self.context_manager.state() if self.context_manager is not None else None
        )
        if self.context_manager is not None:
            # A compaction summary belongs to one conversation.  It must not survive a new chat.
            try:
                self.context_manager.restore(None, 0)
                if before_commit is not None:
                    before_commit()
            except Exception:
                with contextlib.suppress(Exception):
                    self.context_manager.restore(*prior_compaction)
                raise
        elif before_commit is not None:
            before_commit()
        return PreparedUiSessionNew(prior_compaction=prior_compaction)

    def rollback_prepared_new_session(self, prepared: PreparedUiSessionNew) -> None:
        """Restore compaction when a batch preflight or its database transaction fails."""
        if self.context_manager is not None and prepared.prior_compaction is not None:
            self.context_manager.restore(*prepared.prior_compaction)

    def commit_prepared_new_session(
        self,
        prepared: PreparedUiSessionNew,
        *,
        project_id: int | None,
        session_id: int | None,
    ) -> None:
        """Publish a preflighted new chat through non-fallible in-memory assignments."""
        del prepared  # the token documents that fallible cleanup already completed
        self.messages = []
        self.session_id = session_id
        self.project_id = project_id
        self.persistence_state = "new"
        self.last_turn_cost_usd = None
        self.last_turn_model = None
        self.last_turn_provider = None

    def cancel(self, *, expected_turn_id: int | None = None) -> bool:
        """Cancel the in-flight turn (Ctrl-C parity). Returns True if one was cancelled."""
        if expected_turn_id is not None and expected_turn_id != self.current_turn_id:
            return False
        if self._current is not None and not self._current.done():
            self._current.cancel()
            return True
        return False

    @property
    def current_turn_id(self) -> int | None:
        """Process-local identity for the exact in-flight turn exposed to attended controls."""
        return self._turn_generation if self.busy else None

    @property
    def busy(self) -> bool:
        return self._current is not None and not self._current.done()
