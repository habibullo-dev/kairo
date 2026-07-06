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

#: Resolution actions a client may send.
_ACTIONS = frozenset({"approve", "always", "deny"})


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

    def to_public(self) -> dict:
        """The client payload — full and untruncated (EXACT ACTION · EXACT PAYLOAD). The
        screen is private and authenticated, so the whole input is shown before consent."""
        return {
            "decision_id": self.decision_id,
            "tool": self.call.name,
            "input": self.call.input,
            "reason": self.decision.reason,
            "kind": self.kind,
            "title": self.title,
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
    ) -> Permission:
        """Enqueue an ASK, announce it to live clients, and await the human decision. The
        awaiting coroutine is the AgentLoop turn — it stays paused, exactly like the REPL."""
        did = secrets.token_urlsafe(12)
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        pending = PendingApproval(did, call, decision, kind, title, fut, on_always)
        self._pending[did] = pending
        self.log.info(
            "ui_approval_requested", channel="ui", tool=call.name, kind=kind, decision_id=did
        )
        await self.connections.broadcast({"type": "approval", **pending.to_public()})
        try:
            return await fut
        finally:
            self._pending.pop(did, None)
            self._drop_nonces_for(did)

    async def mint_nonce(self, decision_id: str, conn: Connection) -> str | None:
        """Issue a single-use nonce for a pending approval — only to a *live* connection
        that has acked the modal is shown (amendment 4). Bound to this connection."""
        if decision_id not in self._pending or not self.connections.is_live(conn):
            return None
        nonce = secrets.token_urlsafe(24)
        self._nonces[nonce] = (decision_id, conn.id)
        return nonce

    def resolve(self, decision_id: str, nonce: str, action: str) -> tuple[bool, str]:
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

    def _drop_nonces_for(self, decision_id: str) -> None:
        self._nonces = {n: b for n, b in self._nonces.items() if b[0] != decision_id}

    def _apply(self, pending: PendingApproval, action: str) -> tuple[Permission, str | None]:
        if action == "deny":
            return Permission.DENY, None
        if action == "always":
            return Permission.ALLOW, pending.on_always()
        return Permission.ALLOW, None  # approve once


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
