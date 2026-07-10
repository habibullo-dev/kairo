"""Immutable execution scope for attended work.

An :class:`ExecutionContext` is the provenance and delivery key for work that begins in an
interactive UI workspace.  It deliberately carries a persisted session id (never a browser
claim) plus its project scope.  The task-local binding lets downstream collaborators such as
tools, approvals, and child work retain the source scope even while other browser workspaces
change their active chat or project.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass


@dataclass(frozen=True)
class ExecutionContext:
    """The immutable session/project identity of one attended execution.

    ``session_id`` is intentionally required and positive.  A missing id must never become a
    wildcard delivery selector: callers allocate the durable interactive session before they
    begin a turn, voice request, or orchestration run.
    """

    session_id: int
    project_id: int | None

    def __post_init__(self) -> None:
        if not isinstance(self.session_id, int) or self.session_id < 1:
            raise ValueError("execution context requires a positive session_id")
        if self.project_id is not None and (
            not isinstance(self.project_id, int) or self.project_id < 1
        ):
            raise ValueError("project_id must be a positive int or None")

    def to_wire(self) -> dict[str, int | None]:
        """Metadata added to scoped UI envelopes (never a client authority claim)."""
        return {"session_id": self.session_id, "project_id": self.project_id}


_CURRENT_EXECUTION: ContextVar[ExecutionContext | None] = ContextVar(
    "jarvis_execution_context", default=None
)


def current_execution_context() -> ExecutionContext | None:
    """Return the task-local attended execution scope, if one is active."""
    return _CURRENT_EXECUTION.get()


@contextmanager
def bind_execution_context(context: ExecutionContext) -> Iterator[None]:
    """Bind ``context`` for the current task and its spawned child tasks."""
    token = _CURRENT_EXECUTION.set(context)
    try:
        yield
    finally:
        _CURRENT_EXECUTION.reset(token)
