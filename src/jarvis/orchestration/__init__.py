"""Orchestration Studio (Phase 10B): team profiles, workflows, and context assembly.

Code constants + pure builders — the host engine (Task 13) drives them on
``SubAgentService.spawn`` (ADR-0014: no second agent framework). Nothing here spawns or
calls a model; it defines WHO runs (roster roles / team profiles), in WHAT shape (workflow
stage specs), and WHAT they see (a framed, provenance-checked context bundle).
"""

from __future__ import annotations

from jarvis.orchestration.context import (
    ContextBundle,
    ContextItem,
    ContextPolicyError,
    check_context_policy,
)
from jarvis.orchestration.engine import OrchestrationEngine
from jarvis.orchestration.roles import (
    READ_ONLY_SPAWNABLE,
    Capability,
    RosterRole,
)
from jarvis.orchestration.store import OrchestrationRun, OrchestrationStore
from jarvis.orchestration.teams import TEAM_PROFILES, TeamProfile, resolve_team
from jarvis.orchestration.workflows import (
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
    "ContextBundle",
    "ContextItem",
    "ContextPolicyError",
    "OrchestrationEngine",
    "OrchestrationRun",
    "OrchestrationStore",
    "RosterRole",
    "StageSpec",
    "TeamProfile",
    "WorkflowTemplate",
    "check_context_policy",
    "resolve_team",
    "validate_workflow",
]
