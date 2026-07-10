"""UI approvals — the Gate queue, replay-proof and one-truth (Phase 8, Task 3).

An ASK from a UI turn (or a sub-agent under one) becomes a *pending approval*: the full
payload + the gate's reason are pushed to connected clients, and the turn awaits a human
decision — exactly like the REPL prompt, but over the network. The network is why this is
hardened (ADR-0008 §3):

* **One truth.** "Always allow (narrow)" runs the shared :func:`persist_always`, so UI and
  REPL persist identically; a sub-agent's "always" runs the same run-scoped ``gate.grant``
  pattern the REPL uses.
* **Replay-proof.** Resolving requires a single-use **nonce** minted only over a *live*
  authenticated WebSocket, after the client acks the modal is on screen, invalidated on use
  and on that connection dropping. A cookie replay from a dead or unwatching client cannot
  approve.

``ApprovalManager`` is framework-free (it takes a ``ConnectionManager`` and asyncio futures),
so the whole matrix is unit-testable without a server.
"""

from __future__ import annotations

import asyncio
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from jarvis.core.execution import ExecutionContext, current_execution_context
from jarvis.observability import get_logger
from jarvis.permissions import persist_always
from jarvis.tools import Permission

if TYPE_CHECKING:
    from jarvis.config import Config
    from jarvis.core.client import ToolCall
    from jarvis.permissions.gate import Decision, PermissionGate
    from jarvis.permissions.subagent import SubAgentGate
    from jarvis.ui.connections import Connection, ConnectionManager

TURN = "turn"
SUBAGENT = "subagent"
VOICE = "voice"

#: Resolution actions a client may send.
_ACTIONS = frozenset({"approve", "always", "deny"})

#: How often the fail-closed watcher re-checks the screen while a voice approval is pending.
_ABORT_POLL_SECONDS = 0.05


@dataclass
class PendingApproval:
    """One ASK awaiting a human decision. ``on_always`` performs the narrow persist/grant
    when the human chooses "Always allow" and returns a short description (or None)."""

    decision_id: str
    call: ToolCall
    decision: Decision
    kind: str  # TURN | SUBAGENT
    title: str | None  # sub-agent title, for labeling
    future: asyncio.Future
    on_always: Callable[[], str | None]
    context: ExecutionContext

    def to_public(self) -> dict:
        """The client payload — full and untruncated (EXACT ACTION · EXACT PAYLOAD). The
        screen is private and authenticated, so the whole input is shown before consent.
        ``persistable`` is False for a tainted-egress ASK (Phase 9) so the client hides the
        "Always allow" affordance — and ``_apply`` refuses to persist it regardless."""
        return {
            "decision_id": self.decision_id,
            "tool": self.call.name,
            "input": self.call.input,
            "reason": self.decision.reason,
            "kind": self.kind,
            "title": self.title,
            "persistable": self.decision.persistable,
        }


class ApprovalManager:
    """The pending-approval queue + nonce lifecycle. Lives on ``app.state`` so the WS
    handler (mint nonce), the resolve route, and the injected approver all share it."""

    def __init__(self, connections: ConnectionManager, *, log=None) -> None:
        self.connections = connections
        self._pending: dict[str, PendingApproval] = {}
        self._nonces: dict[str, tuple[str, str]] = {}  # nonce -> (decision_id, conn_id)
        self.log = log or get_logger("jarvis.ui.approvals")

    def pending(self) -> list[PendingApproval]:
        return list(self._pending.values())

    def pending_for(self, context: ExecutionContext) -> list[PendingApproval]:
        """The Gate read model for one exact attended chat/project."""
        return [pending for pending in self._pending.values() if pending.context == context]

    def get(self, decision_id: str) -> PendingApproval | None:
        return self._pending.get(decision_id)

    async def request(
        self,
        call: ToolCall,
        decision: Decision,
        *,
        kind: str,
        title: str | None,
        on_always: Callable[[], str | None],
        abort_when: Callable[[], bool] | None = None,
    ) -> Permission:
        """Enqueue an ASK, announce it to live clients, and await the human decision. The
        awaiting coroutine is the AgentLoop turn — it stays paused, exactly like the REPL.

        ``abort_when`` is the voice-screen fail-closed hook (Task 6): while awaiting, if the
        predicate becomes True (the watching screen went away), the decision resolves DENY.
        Absent, the request waits indefinitely (the REPL/turn behavior)."""
        context = current_execution_context()
        if context is None:
            # A typed/voice/subagent ASK without a persisted, server-bound source cannot safely
            # select a Gate surface.  Never convert missing provenance into a global prompt.
            self.log.warning("ui_approval_missing_execution_context", channel="ui", tool=call.name)
            return Permission.DENY
        did = secrets.token_urlsafe(12)
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        pending = PendingApproval(did, call, decision, kind, title, fut, on_always, context)
        self._pending[did] = pending
        self.log.info(
            "ui_approval_requested", channel="ui", tool=call.name, kind=kind, decision_id=did
        )
        await self.connections.publish(context, {"type": "approval", **pending.to_public()})
        try:
            if abort_when is None:
                return await fut
            # Poll the abort predicate alongside the decision; the screen vanishing ⇒ DENY.
            while not fut.done():
                if abort_when():
                    if not fut.done():
                        fut.set_result(Permission.DENY)
                        self.log.info("ui_approval_aborted", channel="ui", decision_id=did)
                    break
                await asyncio.wait({fut}, timeout=_ABORT_POLL_SECONDS)
            return await fut
        finally:
            self._pending.pop(did, None)
            self._drop_nonces_for(did)

    async def mint_nonce(self, decision_id: str, conn: Connection) -> str | None:
        """Issue a single-use nonce for a pending approval — only to a *live* connection
        that has acked the modal is shown (amendment 4). Bound to this connection."""
        pending = self._pending.get(decision_id)
        if (
            pending is None
            or not self.connections.is_live(conn)
            or conn.context != pending.context
        ):
            return None
        nonce = secrets.token_urlsafe(24)
        self._nonces[nonce] = (decision_id, conn.id)
        return nonce

    def resolve(
        self,
        decision_id: str,
        nonce: str,
        action: str,
        *,
        context: ExecutionContext | None = None,
    ) -> tuple[bool, str]:
        """Resolve a pending approval. Requires a valid, un-replayed nonce bound to this
        decision and to a *still-live* connection. Single-use: consumed on success."""
        if action not in _ACTIONS:
            return False, "unknown action"
        bound = self._nonces.get(nonce)
        if bound is None or bound[0] != decision_id:
            self.log.warning("ui_approval_bad_nonce", channel="ui", decision_id=decision_id)
            return False, "invalid or replayed nonce"
        conn = self.connections.get(bound[1])
        if conn is None or not self.connections.is_live(conn):
            return False, "approving client is not live"
        pending = self._pending.get(decision_id)
        if pending is None or pending.future.done():
            return False, "no such pending approval"
        if conn.context != pending.context or (context is not None and context != pending.context):
            return False, "approval belongs to a different workspace"
        del self._nonces[nonce]  # single-use — consumed before resolving
        perm, persisted = self._apply(pending, action)
        pending.future.set_result(perm)
        self.log.info(
            "ui_approval_resolved",
            channel="ui",
            tool=pending.call.name,
            permission=str(perm),
            decision_id=decision_id,
            persisted=persisted,
        )
        return True, "resolved"

    def fail(self, decision_id: str, perm: Permission = Permission.DENY) -> None:
        """Resolve a pending approval WITHOUT a click — the fail-closed path (e.g. the voice
        screen's client vanished mid-confirmation ⇒ DENY, Task 6). No nonce involved."""
        pending = self._pending.get(decision_id)
        if pending is not None and not pending.future.done():
            pending.future.set_result(perm)
            self.log.info(
                "ui_approval_failed_closed",
                channel="ui",
                decision_id=decision_id,
                permission=str(perm),
            )

    def invalidate_connection(self, conn: Connection) -> None:
        """Drop every nonce bound to a connection that just disconnected/reconnected."""
        self._nonces = {n: b for n, b in self._nonces.items() if b[1] != conn.id}

    def fail_context(self, context: ExecutionContext) -> None:
        """Fail closed every pending approval from a context that has been replaced."""
        for pending in self.pending_for(context):
            self.fail(pending.decision_id)

    def _drop_nonces_for(self, decision_id: str) -> None:
        self._nonces = {n: b for n, b in self._nonces.items() if b[0] != decision_id}

    def _apply(self, pending: PendingApproval, action: str) -> tuple[Permission, str | None]:
        if action == "deny":
            return Permission.DENY, None
        # A non-persistable decision (tainted egress, Phase 9) never persists — even if a
        # crafted client sends "always", it degrades to approve-once. The client also hides
        # the button, but this is the structural guarantee.
        if action == "always" and pending.decision.persistable:
            return Permission.ALLOW, pending.on_always()
        return Permission.ALLOW, None  # approve once (or a suppressed "always")


class UIApprover:
    """The injected ``Approver`` for a UI turn: every ASK becomes a Gate item, resolved on
    screen. "Always" runs the shared narrow-persist — identical to the REPL (ADR-0008 §3)."""

    def __init__(self, approvals: ApprovalManager, gate: PermissionGate, config: Config) -> None:
        self.approvals = approvals
        self.gate = gate
        self.config = config

    async def __call__(self, call: ToolCall, decision: Decision) -> Permission:
        return await self.approvals.request(
            call,
            decision,
            kind=TURN,
            title=None,
            on_always=lambda: persist_always(self.gate, self.config, call),
        )


def make_ui_subagent_approver(
    approvals: ApprovalManager, gate: SubAgentGate, agent_id: str, title: str
) -> Callable:
    """Approver for one child run (mirrors the REPL's ``_make_subagent_approver``): the
    child's ASK is surfaced labeled with its title, and "always" records a run-scoped
    *pattern* grant on the child's gate (host / dir — never a blanket run_shell/write_file,
    never persisted), not the shared persist_always."""

    async def approve(call: ToolCall, decision: Decision) -> Permission:
        def on_always() -> str | None:
            grant = gate.grant(call.name, call.input)
            return grant.describe() if grant is not None else None

        return await approvals.request(
            call, decision, kind=SUBAGENT, title=title, on_always=on_always
        )

    return approve


class UIScreenApprover:
    """The workstation's implementation of the voice ``ScreenApprover`` (ADR-0008 §5 /
    checkpoint §1.3). It is passed to a ``VoiceApprover`` as the *screen*; voice prepares,
    this screen commits.

    ``available()`` is a **positive** check — a currently-live client with the Gate/approval
    surface mounted (not a one-time hello claim). Anything less ⇒ unavailable ⇒ the
    ``VoiceApprover`` denies (it never assumes a screen). ``confirm()`` enqueues the action as
    a Gate item resolved by the same replay-proof nonce flow as a typed turn — so a spoken
    "yes" has no path to approve — and is **fail-closed**: if the watching surface goes away
    mid-confirmation, the decision resolves DENY. Voice approvals never persist "always"
    (per-instance only)."""

    def __init__(
        self, approvals: ApprovalManager, connections: ConnectionManager, *, surface: str = "gate"
    ) -> None:
        self.approvals = approvals
        self.connections = connections
        self.surface = surface

    def available(self) -> bool:
        context = current_execution_context()
        return context is not None and self.connections.has_live_surface(
            self.surface, context=context
        )

    async def confirm(self, call: ToolCall, decision: Decision) -> Permission:
        return await self.approvals.request(
            call,
            decision,
            kind=VOICE,
            title=None,
            on_always=lambda: None,  # voice never persists "always" — per-instance only
            abort_when=lambda: not self.available(),  # screen vanished ⇒ fail-closed DENY
        )
