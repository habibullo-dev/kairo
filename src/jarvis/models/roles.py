"""Roles and their default model routes (Phase 10 Task 6).

A *role* is a job an agent does (planner, coder, reviewer, …); a :class:`ModelRoute` is the
(provider, model, effort) it runs on. Defaults are code constants — versioned with the code,
no migration, no injection surface — and are overridden by config → project → per-run
(resolution lives in :mod:`jarvis.models.registry`). Voice STT/TTS routing is deliberately
NOT here — those are audio APIs configured under ``voice`` (``voice.cloud_providers``), not
LLM completion clients.
"""

from __future__ import annotations

from dataclasses import dataclass

from jarvis.models.providers import PROVIDER_CATALOG

#: The LLM-completion roles the orchestration studio (10B) and the registry know about.
#: ``planner`` doubles as the head planner / final reviewer (Fable by default).
ROLES: tuple[str, ...] = (
    "planner",
    "coder",
    "reviewer",
    "security",
    "ux",
    "qa",
    "researcher",
    "docs",
    "judge",
    "utility",
)

#: Roles that MUST be able to drive tools (a write-capable executor). A text-only route
#: (e.g. the OpenAI/Gemini adapters) is rejected for these — enforced in the registry.
TOOL_CAPABLE_ROLES: frozenset[str] = frozenset({"coder"})

#: Known providers — derived from the provider catalog (the single source of truth, Phase 10C).
PROVIDERS: frozenset[str] = frozenset(PROVIDER_CATALOG)

#: Final-authority roles: the head planner/synthesizer/final reviewer, and the eval judge.
#: A route on one of these MUST resolve to a TRUSTED_AUTHORITY_PROVIDERS provider (anthropic
#: this phase) — no config/project/run override can put a cheap worker here. Enforced (pure,
#: catalog-static) in validate_route. Giving another provider final authority is a separate,
#: explicit design change, never a routing override (Phase 10C non-negotiable #1).
FINAL_AUTHORITY_ROLES: frozenset[str] = frozenset({"planner", "judge"})

#: Roles that process raw PRIVATE content (conversation, memory extraction, compaction,
#: digest summarize) and so must stay on a trusted provider this phase.
PRIVATE_CONTEXT_ROLES: frozenset[str] = frozenset({"utility"})


@dataclass(frozen=True)
class ModelRoute:
    """Where a role runs. ``effort`` applies to Anthropic only (ignored by OpenAI);
    ``text_only`` marks a route that cannot drive tools (the OpenAI adapter this phase) —
    valid for analysis roles, rejected for tool-capable ones."""

    provider: str
    model: str
    effort: str = "high"
    text_only: bool = False


#: Default routes. Planner/judge → Fable (the head/reviewer tier); coder → Opus (the
#: write-capable executor); utility/qa/researcher/docs → Sonnet (fast, cheap-enough);
#: reviewer/security/ux → Opus. All Anthropic by default — OpenAI is opt-in per role.
DEFAULT_ROUTES: dict[str, ModelRoute] = {
    "planner": ModelRoute("anthropic", "claude-fable-5", "high"),
    "coder": ModelRoute("anthropic", "claude-opus-4-8", "high"),
    "reviewer": ModelRoute("anthropic", "claude-opus-4-8", "high"),
    "security": ModelRoute("anthropic", "claude-opus-4-8", "high"),
    "ux": ModelRoute("anthropic", "claude-opus-4-8", "medium"),
    "qa": ModelRoute("anthropic", "claude-sonnet-5", "medium"),
    "researcher": ModelRoute("anthropic", "claude-sonnet-5", "medium"),
    "docs": ModelRoute("anthropic", "claude-sonnet-5", "medium"),
    "judge": ModelRoute("anthropic", "claude-fable-5", "high"),
    "utility": ModelRoute("anthropic", "claude-sonnet-5", "medium"),
}


def default_route(role: str) -> ModelRoute:
    """The built-in route for ``role`` (raises KeyError for an unknown role)."""
    return DEFAULT_ROUTES[role]
