"""Worst-case estimate + budget reservation + two-step confirm (Phase 10B Task 14).

The estimator is pure (no DB, no model calls), so most of this is table-driven. Pins: worst-
case turn counts per stage; fail-closed on an unpriced route OR an unpriced metered service;
the four block conditions (unpriced / per-role cap / per-team budget / per-run reservation);
the confirm threshold; flat per-op service costs included. Then the engine integration: a
``block`` records an auditable ``budget_stopped`` row and spawns nothing; a ``confirm`` raises
``ConfirmationRequired`` with NO row opened, and re-running with ``confirmed=True`` proceeds.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from kira.config import BudgetsConfig
from kira.core.client import FakeClient, ToolCall, tool_use_message
from kira.models.registry import ModelRegistry
from kira.observability.cost import Price, PricingTable
from kira.orchestration import (
    WORKFLOWS,
    ConfirmationRequired,
    ContextBundle,
    OrchestrationEngine,
    OrchestrationStore,
    RunEstimate,
    estimate_run,
    resolve_team,
)
from kira.orchestration.context import ContextItem, Provenance
from kira.persistence.db import connect
from kira.projects import ProjectStore
from kira.tools.base import ToolResult

_OPEN: list = []


@pytest.fixture(autouse=True)
async def _close():
    yield
    while _OPEN:
        await _OPEN.pop().close()


def _pricing(services: dict | None = None) -> PricingTable:
    return PricingTable(
        version="t",
        effective="",
        cache_write_multiplier=1.25,
        cache_read_multiplier=0.1,
        models={
            "anthropic": {
                "claude-fable-5": Price(10.0, 50.0),
                "claude-opus-4-8": Price(5.0, 25.0),
                "claude-sonnet-5": Price(3.0, 15.0),
            }
        },
        services=services or {},
    )


_REG = ModelRegistry()
_CTX = ContextBundle(
    items=(
        ContextItem(kind="repo_file", ref="a.py", provenance=Provenance.REPO_CODE, text="x" * 400),
    )
)


def _est(team_id: str, workflow_id: str, *, budgets=None, pricing=None, **kw) -> RunEstimate:
    return estimate_run(
        team=resolve_team(team_id, kw.pop("overrides", None)),
        workflow=WORKFLOWS[workflow_id],
        registry=_REG,
        pricing=pricing or _pricing(),
        budgets=budgets or BudgetsConfig(),
        context_tokens=500,
        max_rounds=3,
        iterations=6,
        out_per_call=2048,
        **kw,
    )


# --- worst-case turn counting -----------------------------------------------


def test_building_workflow_turn_counts() -> None:
    # backend: architect(RO), be_implementer(WRITER), data_analyst(RO). implement is a building
    # workflow (council→synthesis→execution→review→verdict), max_rounds=3.
    est = _est("backend", "implement", budgets=BudgetsConfig(confirm_above_usd=1e9))
    turns = {m.member_id: m.turns for m in est.members}
    # RO members run council once + review each round; the writer runs execution each round.
    assert turns == {"architect": 1 + 3, "data_analyst": 1 + 3, "be_implementer": 3}
    assert est.total_usd is not None and est.total_usd > 0
    assert est.head_usd is not None and est.head_usd > 0  # synthesis + 3 verdicts
    assert est.decision == "ok"  # confirm threshold lifted above any real cost


def test_analysis_workflow_has_no_execution_turns() -> None:
    # review_diff is analysis: council → synthesis → verdict. No execution/review stage ⇒ the
    # writer never runs, RO members run council once.
    est = _est("backend", "review_diff", budgets=BudgetsConfig(confirm_above_usd=1e9))
    turns = {m.member_id: m.turns for m in est.members}
    assert turns == {"architect": 1, "data_analyst": 1}  # be_implementer absent (0 turns)


# --- fail-closed on unpriced (route + service) ------------------------------


def test_unpriced_route_blocks_and_can_be_allowed() -> None:
    # Point a member's role at a model with no price ⇒ unpriced ⇒ fail-closed block.
    routes = {"reviewer": {"model": "claude-unknown-9"}}
    est = _est("backend", "review_diff", project_routes=routes)
    assert est.decision == "block" and est.total_usd is None
    assert any("claude-unknown-9" in u for u in est.unpriced)
    # With treat_unpriced_as_blocking OFF, an unpriced run is allowed (total stays None).
    est2 = _est(
        "backend",
        "review_diff",
        project_routes=routes,
        budgets=BudgetsConfig(treat_unpriced_as_blocking=False, confirm_above_usd=1e9),
    )
    assert est2.decision == "ok" and est2.total_usd is None


def _team_with_writer_service(service: str) -> object:
    # A writer legitimately may hold an egress/metered service (execution-stage authority). Build
    # one directly so the estimate sees an unpriced metered service without violating the
    # read-only service floor.
    from kira.orchestration.roles import Capability, RosterRole
    from kira.orchestration.teams import TeamProfile

    writer = RosterRole(
        "impl", "Impl", "coder",
        frozenset({"write_file"}), frozenset({service}), Capability.WRITE_CAPABLE, "diff_proposal",
    )
    return TeamProfile("t", "T", "d", "x", "#fff", (writer,), ("implement",))


def test_unpriced_metered_service_blocks() -> None:
    # A writer holds "exa" (metered). With no services pricing entry it is unpriced ⇒ fail-closed.
    team = _team_with_writer_service("exa")
    est = estimate_run(
        team=team, workflow=WORKFLOWS["implement"], registry=_REG, pricing=_pricing(),
        budgets=BudgetsConfig(), context_tokens=200, max_rounds=3, iterations=6, out_per_call=2048,
    )
    assert est.decision == "block" and any(u == "service:exa" for u in est.unpriced)
    # Give exa a price ⇒ no longer unpriced; the flat per-op cost is included.
    est2 = estimate_run(
        team=team, workflow=WORKFLOWS["implement"], registry=_REG,
        pricing=_pricing({"exa": {"unit": "search", "usd_per_unit": 0.02}}),
        budgets=BudgetsConfig(confirm_above_usd=1e9), context_tokens=200, max_rounds=3,
        iterations=6, out_per_call=2048,
    )
    assert est2.decision == "ok"
    impl = next(m for m in est2.members if m.member_id == "impl")
    assert impl.service_usd == pytest.approx(0.02 * impl.turns)  # 1 op per turn


# --- the four block conditions + confirm ------------------------------------


def test_per_role_cap_blocks() -> None:
    est = _est("backend", "implement", budgets=BudgetsConfig(per_role_max_usd=0.01))
    assert est.decision == "block" and "be_implementer" in est.over_role_cap


def test_per_team_budget_blocks() -> None:
    est = _est("backend", "implement", overrides={"team_budget_usd": 0.01})
    assert est.decision == "block" and est.over_team_budget is True


def test_per_run_reservation_blocks() -> None:
    est = _est("backend", "implement", budget_usd=0.01)
    assert est.decision == "block" and "per-run reservation" in est.reason


def test_confirm_threshold() -> None:
    est = _est("backend", "implement", budgets=BudgetsConfig(confirm_above_usd=0.001))
    assert est.decision == "confirm" and "confirm threshold" in est.reason


def test_all_ok_under_generous_budget() -> None:
    est = _est("backend", "implement", budgets=BudgetsConfig(confirm_above_usd=1e9))
    assert est.decision == "ok" and est.total_usd is not None


# --- engine integration: reservation + two-step confirm ---------------------


async def _store(tmp_path: Path) -> OrchestrationStore:
    db = await connect(tmp_path / "o.db")
    _OPEN.append(db)
    lock = asyncio.Lock()
    await ProjectStore(db, lock).create(name="P")  # id 1
    return OrchestrationStore(db, lock)


def _synth() -> object:
    return tool_use_message([ToolCall(id="s", name="record_synthesis", input={"summary": "s"})])


def _verdict(v: str) -> object:
    return tool_use_message(
        [ToolCall(id="v", name="record_verdict", input={"verdict": v, "rationale": "r"})]
    )


def _engine(store, *, budgets, head, spawn, turn_lock=None) -> OrchestrationEngine:
    return OrchestrationEngine(
        spawn=spawn,
        store=store,
        head_client=head,
        head_model="claude-fable-5",
        turn_lock=turn_lock or asyncio.Lock(),
        max_rounds=3,
        registry=_REG,
        pricing=_pricing(),
        budgets=budgets,
    )


async def test_engine_reservation_block_records_budget_stopped(tmp_path: Path) -> None:
    store = await _store(tmp_path)
    calls: list[dict] = []

    async def fake_spawn(**kw) -> ToolResult:
        calls.append(kw)
        return ToolResult(content="r", is_error=False)

    engine = _engine(
        store, budgets=BudgetsConfig(per_role_max_usd=0.01), head=FakeClient([]), spawn=fake_spawn
    )
    rid = await engine.run(
        project_id=1,
        team=resolve_team("backend"),
        workflow=WORKFLOWS["implement"],
        context=_CTX,
        title="blocked",
    )
    run = await store.get(rid)
    assert run.status == "budget_stopped" and "per-role cap" in run.synthesis_summary
    assert run.estimated_cost_usd is not None  # the worst-case estimate is recorded
    assert calls == []  # nothing spawned


async def test_engine_confirm_raises_then_runs(tmp_path: Path) -> None:
    store = await _store(tmp_path)
    calls: list[dict] = []

    async def fake_spawn(**kw) -> ToolResult:
        calls.append(kw)
        return ToolResult(content="r", is_error=False)

    # A tiny confirm threshold ⇒ the run needs confirmation; no per-run/role/team caps ⇒ not a
    # block. head is only consumed on the confirmed run.
    budgets = BudgetsConfig(confirm_above_usd=0.001)
    head = FakeClient([_synth(), _verdict("accept")])
    engine = _engine(store, budgets=budgets, head=head, spawn=fake_spawn)

    with pytest.raises(ConfirmationRequired) as ei:
        await engine.run(
            project_id=1,
            team=resolve_team("backend"),
            workflow=WORKFLOWS["implement"],
            context=_CTX,
            title="needs confirm",
        )
    assert ei.value.estimate.decision == "confirm"
    assert await store.list(project_id=1) == []  # NO run row opened on the confirm gate

    rid = await engine.run(
        project_id=1,
        team=resolve_team("backend"),
        workflow=WORKFLOWS["implement"],
        context=_CTX,
        title="confirmed",
        confirmed=True,
    )
    assert (await store.get(rid)).status == "ok"
    assert any(c["stage"] == "execution" for c in calls)  # confirmed ⇒ the run proceeded


async def test_engine_without_estimation_config_skips_reservation(tmp_path: Path) -> None:
    # No registry/pricing/budgets ⇒ estimation is skipped entirely (the Task 13 behavior); a
    # run that WOULD be blocked under a tiny cap runs normally here.
    store = await _store(tmp_path)

    async def fake_spawn(**kw) -> ToolResult:
        return ToolResult(content="r", is_error=False)

    engine = OrchestrationEngine(
        spawn=fake_spawn,
        store=store,
        head_client=FakeClient([_synth(), _verdict("accept")]),
        head_model="claude-fable-5",
        turn_lock=asyncio.Lock(),
    )
    assert engine.estimate(resolve_team("backend"), WORKFLOWS["implement"], _CTX) is None
    rid = await engine.run(
        project_id=1,
        team=resolve_team("backend"),
        workflow=WORKFLOWS["implement"],
        context=_CTX,
        title="no estimation",
    )
    assert (await store.get(rid)).status == "ok"
