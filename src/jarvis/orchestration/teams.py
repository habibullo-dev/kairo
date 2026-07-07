"""Team profiles — the 8 project-specific AI teams (Phase 10B).

Code constants over the role→route registry. Each team is a roster of :class:`RosterRole`
members with per-member tools/services/capability, default workflows, and a team budget.
Project customization lives in ``projects.settings_json["teams"]`` and is validated against
the SAME invariants (never silently widened). Invariants (pinned): exactly ≤1 write_capable
member per team; every member passes the role tool-floor; member services name real catalog
entries. Members hold no ``spawn_agent`` (it isn't in SPAWNABLE) — teams are groups, not swarms.
"""

from __future__ import annotations

from dataclasses import dataclass

from jarvis.orchestration.roles import Capability, RosterRole, validate_role
from jarvis.services.catalog import SERVICE_CATALOG

_READ = frozenset({"read_file", "list_dir", "glob_search", "query_knowledge_base"})
_WRITE = frozenset({"read_file", "list_dir", "glob_search", "write_file", "run_shell"})


@dataclass(frozen=True)
class TeamProfile:
    id: str
    name: str
    description: str
    icon: str
    color: str
    members: tuple[RosterRole, ...]
    default_workflows: tuple[str, ...]
    team_budget_usd: float | None = None


class TeamError(ValueError):
    """A team profile that violates an invariant (multiple writers, unknown service, …)."""


def _ro(
    id: str,
    title: str,
    route: str,
    *,
    services: frozenset[str] = frozenset(),
    output: str = "report",
) -> RosterRole:
    return RosterRole(id, title, route, _READ, services, Capability.READ_ONLY, output)


def _writer(
    id: str, title: str, route: str, *, services: frozenset[str] = frozenset()
) -> RosterRole:
    return RosterRole(id, title, route, _WRITE, services, Capability.WRITE_CAPABLE, "diff_proposal")


TEAM_PROFILES: dict[str, TeamProfile] = {
    "research": TeamProfile(
        "research",
        "Research",
        "Gather + synthesize external and KB knowledge.",
        "🔎",
        "#3b82f6",
        (
            _ro("lead_researcher", "Lead Researcher", "researcher", services=frozenset({"exa"})),
            _ro("analyst", "Analyst", "utility"),
            _ro("archivist", "Archivist", "docs"),
        ),
        ("research", "council_review"),
    ),
    "frontend": TeamProfile(
        "frontend",
        "Frontend / UX",
        "Design, build, and visually QA the UI.",
        "🎨",
        "#a855f7",
        (
            _ro("ux_lead", "UX Lead", "ux", services=frozenset({"playwright_local"})),
            _writer("fe_implementer", "Implementer", "coder"),
            _ro("visual_qa", "Visual QA", "qa", services=frozenset({"playwright_local"})),
        ),
        ("ux_critique", "implement", "review_diff"),
    ),
    "backend": TeamProfile(
        "backend",
        "Backend / Data",
        "Design and implement services and data flows.",
        "🗄",
        "#0ea5e9",
        (
            _ro("architect", "Architect", "reviewer"),
            _writer("be_implementer", "Implementer", "coder"),
            _ro("data_analyst", "Data Analyst", "utility"),
        ),
        ("implement", "review_diff", "refactor_proposal"),
    ),
    "security": TeamProfile(
        "security",
        "Security",
        "Scan, review, and red-team for vulnerabilities.",
        "🛡",
        "#ef4444",
        (
            _ro(
                "sec_lead", "Security Lead", "security", services=frozenset({"semgrep", "gitleaks"})
            ),
            _ro("scanner", "Scanner", "utility", services=frozenset({"semgrep", "gitleaks"})),
            _ro("redteam", "Red-team Analyst", "security"),
        ),
        ("security_review", "review_diff"),
    ),
    "qa": TeamProfile(
        "qa",
        "QA / Eval",
        "Assert behavior; read eval freshness and regressions.",
        "✅",
        "#22c55e",
        (
            _ro("qa_lead", "QA Lead", "qa", services=frozenset({"playwright_local"})),
            _ro("eval_reader", "Eval Reader", "utility"),
            _ro("ui_tester", "UI Tester", "qa", services=frozenset({"playwright_local"})),
        ),
        ("debug_eval", "review_diff"),
    ),
    "pm": TeamProfile(
        "pm",
        "Product / PM",
        "Specs, PRDs, release notes; read backlog + docs.",
        "📋",
        "#f59e0b",
        (
            _writer("pm_lead", "PM Lead", "docs"),  # writes the spec/PRD (execution stage)
            _ro("spec_writer", "Spec Writer", "docs"),
            _ro("pm_researcher", "Researcher", "researcher"),
        ),
        ("plan_feature", "release_notes"),
    ),
    "ops": TeamProfile(
        "ops",
        "Ops / Cost",
        "Cost/ROI analysis and release operations.",
        "⚙",
        "#64748b",
        (
            _ro("ops_analyst", "Ops Analyst", "utility"),
            _ro("release_notes", "Release Notes", "docs"),
        ),
        ("release_notes", "debug_eval"),
    ),
    "custom": TeamProfile(
        "custom",
        "Custom",
        "A user-defined team (roster in project settings).",
        "✨",
        "#94a3b8",
        (_ro("lead", "Lead", "planner"),),
        ("council_review",),
    ),
}


def validate_team(team: TeamProfile) -> None:
    """Raise :class:`TeamError`/RosterError if the team breaks an invariant."""
    writers = [m for m in team.members if m.capability is Capability.WRITE_CAPABLE]
    if len(writers) > 1:
        raise TeamError(
            f"team {team.id!r} has {len(writers)} write-capable members; at most 1 is allowed"
        )
    if not team.members:
        raise TeamError(f"team {team.id!r} has no members")
    for m in team.members:
        validate_role(m)  # tool floors
        unknown = m.services - set(SERVICE_CATALOG)
        if unknown:
            raise TeamError(f"role {m.id!r}: unknown services {sorted(unknown)}")


def resolve_team(team_id: str, overrides: dict | None = None) -> TeamProfile:
    """The team profile for ``team_id``, validated. ``overrides`` (from a project's
    settings_json["teams"][team_id]) currently supports a ``team_budget_usd`` cap; roster
    overrides are validated against the same invariants when added (never widened)."""
    base = TEAM_PROFILES.get(team_id)
    if base is None:
        raise TeamError(f"unknown team {team_id!r}")
    team = base
    if overrides and "team_budget_usd" in overrides:
        team = TeamProfile(
            base.id,
            base.name,
            base.description,
            base.icon,
            base.color,
            base.members,
            base.default_workflows,
            overrides["team_budget_usd"],
        )
    validate_team(team)
    return team
