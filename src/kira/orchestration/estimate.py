"""Worst-case cost estimation + the budget decision for an orchestration run (Task 14).

A pure function: given a team, a workflow, the model registry, the pricing table, and the
budget config, it computes a WORST-CASE upper bound on the run's cost and a decision:

* ``block`` — refuse before any fan-out. Triggered by (in order): an unpriced route or metered
  service when ``treat_unpriced_as_blocking`` (fail-closed — never guess a price); a member over
  its per-role cap; the team over its per-team budget; or the worst case over an explicit
  per-run ``budget_usd`` reservation ceiling.
* ``confirm`` — the worst case exceeds ``confirm_above_usd``; the caller must get a human OK
  (the two-step confirm) before running.
* ``ok`` — proceed.

The worst case assumes every member loops to the iteration cap, re-sending the full context
each call, across every revise round — deliberately an over-estimate, because a reservation
that under-counts is worse than one that over-counts. Cache discounts are ignored (conservative).
Flat per-op service costs are included; a metered service with no pricing entry is unpriced.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from kira.config import BudgetsConfig
from kira.models.registry import ModelRegistry
from kira.observability.cost import PricingTable, Usage
from kira.orchestration.roles import Capability
from kira.orchestration.teams import TeamProfile
from kira.orchestration.workflows import WorkflowTemplate
from kira.services.catalog import SERVICE_CATALOG

#: The head route (Fable) that runs synthesis + every verdict.
HEAD_ROLE = "planner"


@dataclass(frozen=True)
class MemberEstimate:
    member_id: str
    route_role: str
    provider: str
    model: str
    turns: int  # worst-case model calls across the whole run
    model_usd: float | None  # None ⇒ the route is unpriced (fail-closed)
    service_usd: float | None  # None ⇒ a held service is unpriced
    unpriced_services: tuple[str, ...]

    @property
    def total(self) -> float | None:
        if self.model_usd is None or self.service_usd is None:
            return None
        return self.model_usd + self.service_usd


@dataclass(frozen=True)
class RunEstimate:
    total_usd: float | None  # None ⇒ at least one component is unpriced
    members: tuple[MemberEstimate, ...]
    head_usd: float | None
    unpriced: tuple[str, ...]  # human-readable ids of unpriced routes/services
    over_role_cap: tuple[str, ...]
    over_team_budget: bool
    soft_warn: bool
    decision: str  # ok | confirm | block
    reason: str


def _member_usage(context_tokens: int, iterations: int, out_per_call: int) -> Usage:
    """Worst case for one member turn: the child loops to its cap; each call re-sends the
    framed context plus everything produced so far, and emits a full output."""
    total_input = sum(context_tokens + i * out_per_call for i in range(max(1, iterations)))
    return Usage(input_tokens=total_input, output_tokens=max(1, iterations) * out_per_call)


def _head_usage(context_tokens: int, n_reports: int, out_per_call: int) -> Usage:
    """One head call: it reads the framed context + the members' reports, emits one output."""
    return Usage(input_tokens=context_tokens + n_reports * out_per_call, output_tokens=out_per_call)


def _service_cost(
    member_services: frozenset[str], turns: int, pricing: PricingTable
) -> tuple[float | None, tuple[str, ...]]:
    """Flat per-op cost of a member's services (one op per turn, worst case). A metered service
    with no pricing entry, or an ``unknown``-priced one, is unpriced ⇒ (None, [names])."""
    total = 0.0
    unpriced: list[str] = []
    for name in sorted(member_services):
        spec = SERVICE_CATALOG.get(name)
        if spec is None or spec.pricing == "unknown":
            unpriced.append(name)
            continue
        if spec.pricing == "fixed_zero":
            continue  # a known-free local tool contributes $0 (not the fail-closed NULL)
        entry = (pricing.services or {}).get(name)  # metered
        if not entry or entry.get("usd_per_unit") is None:
            unpriced.append(name)
            continue
        total += float(entry["usd_per_unit"]) * max(1, turns)
    return (None if unpriced else total), tuple(unpriced)


def estimate_run(
    *,
    team: TeamProfile,
    workflow: WorkflowTemplate,
    registry: ModelRegistry,
    pricing: PricingTable,
    budgets: BudgetsConfig,
    context_tokens: int,
    max_rounds: int,
    iterations: int,
    out_per_call: int,
    project_routes: dict | None = None,
    budget_usd: float | None = None,
) -> RunEstimate:
    """Worst-case estimate + budget decision. Pure — no DB, no model calls."""
    has_exec = any(s.kind == "execution" for s in workflow.stages)
    rounds = max_rounds if has_exec else 1

    council = [m for m in team.members if m.capability is Capability.READ_ONLY]
    review = [
        m for m in team.members if m.capability in (Capability.READ_ONLY, Capability.REVIEW_ONLY)
    ]
    writers = [m for m in team.members if m.capability is Capability.WRITE_CAPABLE][:1]

    # Worst-case turns per member across the whole run.
    turns: dict[str, int] = defaultdict(int)
    for m in council:
        turns[m.id] += 1  # council runs once
    if has_exec:
        for _ in range(rounds):
            for m in writers:
                turns[m.id] += 1  # one execution per round
            for m in review:
                turns[m.id] += 1  # review each round

    by_id = {m.id: m for m in team.members}
    per_turn = _member_usage(context_tokens, iterations, out_per_call)
    members: list[MemberEstimate] = []
    unpriced: list[str] = []
    for mid, n in turns.items():
        member = by_id[mid]
        route = registry.route(member.route_role, project_routes=project_routes)
        one = pricing.cost(route.provider, route.model, per_turn)
        model_usd = None if one is None else one * n
        if model_usd is None:
            unpriced.append(f"{mid} ({route.provider}:{route.model})")
        svc_usd, svc_unpriced = _service_cost(member.services, n, pricing)
        unpriced.extend(f"service:{s}" for s in svc_unpriced)
        members.append(
            MemberEstimate(
                member_id=mid,
                route_role=member.route_role,
                provider=route.provider,
                model=route.model,
                turns=n,
                model_usd=model_usd,
                service_usd=svc_usd,
                unpriced_services=svc_unpriced,
            )
        )

    # Head: synthesis (1) + one verdict per round (or a single verdict for a read-only workflow).
    head_route = registry.route(HEAD_ROLE, project_routes=project_routes)
    n_reports = max(len(council), len(review), 1)
    head_one = pricing.cost(
        head_route.provider, head_route.model, _head_usage(context_tokens, n_reports, out_per_call)
    )
    head_calls = 1 + (rounds if has_exec else 1)
    head_usd = None if head_one is None else head_one * head_calls
    if head_usd is None:
        unpriced.append(f"head ({head_route.provider}:{head_route.model})")

    components = [m.total for m in members] + [head_usd]
    total_usd = None if any(c is None for c in components) else sum(c for c in components)  # type: ignore[misc]

    # Per-role caps: member.max_cost_usd (override) else the global per_role_max_usd.
    over_role: list[str] = []
    for m in members:
        cap = by_id[m.member_id].max_cost_usd or budgets.per_role_max_usd
        if cap and m.total is not None and m.total > cap:
            over_role.append(m.member_id)
    over_team = bool(
        team.team_budget_usd and total_usd is not None and total_usd > team.team_budget_usd
    )

    # Decision (fail-closed first).
    decision, reason = "ok", "within budget"
    if unpriced and budgets.treat_unpriced_as_blocking:
        decision = "block"
        reason = f"unpriced (fail-closed): {', '.join(unpriced)}"
    elif over_role:
        decision = "block"
        reason = f"members over their per-role cap: {', '.join(over_role)}"
    elif over_team:
        decision = "block"
        reason = f"worst case ${total_usd:.2f} over the team budget ${team.team_budget_usd:.2f}"
    elif budget_usd is not None and total_usd is not None and total_usd > budget_usd:
        decision = "block"
        reason = f"worst case ${total_usd:.2f} over the per-run reservation ${budget_usd:.2f}"
    elif total_usd is not None and total_usd > budgets.confirm_above_usd:
        decision = "confirm"
        reason = (
            f"worst case ${total_usd:.2f} exceeds the confirm threshold "
            f"${budgets.confirm_above_usd:.2f}"
        )

    soft_warn = total_usd is not None and total_usd > budgets.soft_warn_usd_per_run
    return RunEstimate(
        total_usd=total_usd,
        members=tuple(members),
        head_usd=head_usd,
        unpriced=tuple(unpriced),
        over_role_cap=tuple(over_role),
        over_team_budget=over_team,
        soft_warn=soft_warn,
        decision=decision,
        reason=reason,
    )
