"""Workflow templates — the stage sequences the engine runs (Phase 10B).

A workflow is a sequence of stages: **council** (N parallel read-only members on the same
framed context) → **synthesis** (the head model merges) → optional **execution** (the single
write-capable member, under the turn lock) → **review** (parallel read-only over the produced
artifact) → **verdict** (accept/reject/revise). Code constants; the engine (Task 13) maps a
team's members onto these stages by capability. Invariant (pinned): at most ONE execution
stage per workflow, and council/review stages are read-only by construction.
"""

from __future__ import annotations

from dataclasses import dataclass

_STAGE_KINDS = frozenset({"council", "synthesis", "execution", "review", "verdict"})


@dataclass(frozen=True)
class StageSpec:
    """One stage. ``kind`` drives which team members run it (council→read_only,
    execution→the write_capable member, review→review_only, synthesis/verdict→the head)."""

    name: str
    kind: str


@dataclass(frozen=True)
class WorkflowTemplate:
    id: str
    title: str
    stages: tuple[StageSpec, ...]
    baseline_minutes: int  # ROI: how long the equivalent human work would take


class WorkflowError(ValueError):
    """A workflow that breaks an invariant (unknown stage kind, >1 execution stage)."""


def validate_workflow(wf: WorkflowTemplate) -> None:
    kinds = [s.kind for s in wf.stages]
    unknown = set(kinds) - _STAGE_KINDS
    if unknown:
        raise WorkflowError(f"workflow {wf.id!r}: unknown stage kinds {sorted(unknown)}")
    if kinds.count("execution") > 1:
        raise WorkflowError(
            f"workflow {wf.id!r}: at most one execution stage (has {kinds.count('execution')})"
        )
    if not kinds:
        raise WorkflowError(f"workflow {wf.id!r}: no stages")


def _analysis(id: str, title: str, minutes: int) -> WorkflowTemplate:
    """A read-only workflow: council → synthesis → verdict (no write)."""
    return WorkflowTemplate(
        id,
        title,
        (
            StageSpec("Council", "council"),
            StageSpec("Synthesis", "synthesis"),
            StageSpec("Verdict", "verdict"),
        ),
        minutes,
    )


def _building(id: str, title: str, minutes: int) -> WorkflowTemplate:
    """A workflow that produces an artifact: council → synthesis → execution → review → verdict."""
    return WorkflowTemplate(
        id,
        title,
        (
            StageSpec("Council", "council"),
            StageSpec("Synthesis", "synthesis"),
            StageSpec("Execution", "execution"),
            StageSpec("Review", "review"),
            StageSpec("Verdict", "verdict"),
        ),
        minutes,
    )


WORKFLOWS: dict[str, WorkflowTemplate] = {
    w.id: w
    for w in (
        _building("plan_feature", "Plan a feature (spec)", 90),
        _building("implement", "Implement a change", 120),
        _analysis("review_diff", "Review a diff", 30),
        _analysis("security_review", "Security review", 45),
        _analysis("ux_critique", "UX critique", 30),
        _analysis("research", "Research a question", 60),
        _analysis("release_notes", "Draft release notes", 20),
        _analysis("debug_eval", "Debug an eval failure", 45),
        _analysis("refactor_proposal", "Propose a refactor", 40),
        _analysis("council_review", "Council review (generic)", 30),
    )
}
