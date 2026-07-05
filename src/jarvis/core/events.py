"""Events the agent loop emits so an interface can render a turn as it happens.

Keeping these as plain data (not print calls) is what makes interfaces thin: the
REPL (task 8), a future web UI, and tests all consume the same event stream and
decide how to present it. The loop never imports a rendering library.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class TextDelta:
    """Streamed assistant text (a chunk, or a whole message from the fake client)."""

    text: str


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


Event = TextDelta | ToolStarted | ToolFinished | TurnCompleted
