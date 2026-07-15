"""Events the agent loop emits so an interface can render a turn as it happens.

Keeping these as plain data (not print calls) is what makes interfaces thin: the
REPL (task 8), a future web UI, and tests all consume the same event stream and
decide how to present it. The loop never imports a rendering library.
"""

from __future__ import annotations

from dataclasses import dataclass

from kira.observability.cost import Usage


@dataclass
class TextDelta:
    """Streamed assistant text (a chunk, or a whole message from the fake client)."""

    text: str


@dataclass
class ToolDecision:
    """Every tool call's permission outcome, emitted *before* execution — including
    denied and ASK-denied calls that :class:`ToolStarted` never sees (it fires only
    after ALLOW). Lets an observer record what the model *attempted*, not just what
    ran — the load-bearing signal for adversarial evals. Interfaces ignore it."""

    name: str
    input: dict
    gate_decision: str  # the gate's raw verdict: 'allow' | 'ask' | 'deny'
    resolution: str  # final permission after the approver: 'allow' | 'deny'


@dataclass
class ToolStarted:
    """A tool was approved and is about to run."""

    id: str
    name: str
    input: dict


@dataclass
class ToolFinished:
    """A tool finished (or was denied / unknown). ``preview`` is a short excerpt."""

    id: str
    name: str
    is_error: bool
    preview: str


@dataclass
class TurnCompleted:
    """The turn ended — normally (``end_turn``) or by a guard (``max_iterations``)."""

    text: str
    stop_reason: str


@dataclass
class SubAgentEvent:
    """One event from a running sub-agent's inner loop, forwarded to the *parent's*
    event sink (Phase 6). Nothing a child does is hidden: its tool panels, and
    crucially its :class:`ToolDecision` attempts, reach the same observers the parent's
    do — the load-bearing property for adversarial evals of delegated actions.

    ``inner`` is the child's own :class:`Event`; an observer that doesn't care about
    delegation ignores the whole envelope (the renderer no-ops on it by default)."""

    agent_id: str
    title: str
    inner: Event


@dataclass
class SubAgentCompleted:
    """A sub-agent run finished (Phase 6). Carries the child's token usage and cost so
    observers (REPL session totals, eval token/cost accounting) can sum delegated
    spend — child tokens must never be invisible spend. ``status`` is one of
    ``ok`` / ``error`` / ``timeout`` / ``cancelled`` / ``aborted``; ``cost_usd`` is
    ``None`` when the child model's price is unknown (fail-closed, like the recorder)."""

    agent_id: str
    title: str
    status: str
    usage: Usage
    cost_usd: float | None


Event = (
    TextDelta
    | ToolDecision
    | ToolStarted
    | ToolFinished
    | TurnCompleted
    | SubAgentEvent
    | SubAgentCompleted
)
