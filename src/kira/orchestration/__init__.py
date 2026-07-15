"""Orchestration Studio (Phase 10B): team profiles, workflows, and context assembly.

Code constants + pure builders — the host engine (Task 13) drives them on
``SubAgentService.spawn`` (ADR-0014: no second agent framework). Nothing here spawns or
calls a model; it defines WHO runs (roster roles / team profiles), in WHAT shape (workflow
stage specs), and WHAT they see (a framed, provenance-checked context bundle).
"""

from __future__ import annotations

from kira.orchestration.context import (
    ContextBundle,
    ContextItem,
    ContextPolicyError,
    check_context_policy,
)
from kira.orchestration.engine import ConfirmationRequired, OrchestrationEngine, TeamWorkflowError
from kira.orchestration.estimate import MemberEstimate, RunEstimate, estimate_run
from kira.orchestration.roles import (
    READ_ONLY_SPAWNABLE,
    Capability,
    RosterRole,
)
from kira.orchestration.store import OrchestrationRun, OrchestrationStore
from kira.orchestration.teams import TEAM_PROFILES, TeamProfile, resolve_team
from kira.orchestration.workflows import (
    WORKFLOWS,
    StageSpec,
    WorkflowTemplate,
    validate_workflow,
)

__all__ = [
    "READ_ONLY_SPAWNABLE",
    "TEAM_PROFILES",
    "WORKFLOWS",
    "Capability",
    "ConfirmationRequired",
    "ContextBundle",
    "ContextItem",
    "ContextPolicyError",
    "MemberEstimate",
    "OrchestrationEngine",
    "OrchestrationRun",
    "OrchestrationStore",
    "RosterRole",
    "RunEstimate",
    "StageSpec",
    "TeamProfile",
    "TeamWorkflowError",
    "WorkflowTemplate",
    "check_context_policy",
    "estimate_run",
    "resolve_team",
    "validate_workflow",
]
