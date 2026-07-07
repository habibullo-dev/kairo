"""Team/service cost groupings + per-run breakdown + ROI (Phase 10B Task 17).

Over a temp SQLite with seeded model_calls + service_calls rows: the new team/stage groupings,
the service-spend grouping (unpriced counted separately, never $0), the per-run breakdown, and
the ROI arithmetic (value = baseline_minutes × hourly rate − actual; net None when unpriced).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

import jarvis.core  # noqa: F401 - load core first (ledger<->core.context cycle in isolation)
from jarvis.config import BudgetsConfig
from jarvis.observability.budget import BudgetService
from jarvis.observability.cost import Usage, load_pricing
from jarvis.observability.ledger import CostContext, CostLedger, ServiceLedger, cost_context
from jarvis.orchestration import OrchestrationStore
from jarvis.persistence.db import connect
from jarvis.projects import ProjectStore

_OPEN: list = []


@pytest.fixture(autouse=True)
async def _close():
    yield
    while _OPEN:
        await _OPEN.pop().close()


async def _seed(tmp_path: Path):
    db = await connect(tmp_path / "c.db")
    _OPEN.append(db)
    lock = asyncio.Lock()
    await ProjectStore(db, lock).create(name="P")  # id 1
    store = OrchestrationStore(db, lock)
    rid = await store.begin_run(
        project_id=1,
        workflow="security_review",
        title="Security · review",
        config={"team": "security"},
        context_manifest=[],
        estimated_cost_usd=0.5,
        budget_usd=5.0,
    )
    pricing = load_pricing(None)
    model_ledger = CostLedger(db, lock, pricing)
    svc_ledger = ServiceLedger(db, lock)

    async def _model(team, role, stage, model, usage):
        ctx = CostContext(
            project_id=1,
            orchestration_run_id=rid,
            team=team,
            agent_role=role,
            stage=stage,
            purpose="orchestration",
        )
        await model_ledger.record(
            provider="anthropic",
            model=model,
            effort=None,
            usage=usage,
            latency_ms=None,
            tool_call_count=0,
            ctx=ctx,
        )

    # two security-team model calls (council + verdict) on different roles/models
    await _model("security", "security", "council", "claude-opus-4-8", Usage(1000, 500))
    await _model("security", "planner", "verdict", "claude-fable-5", Usage(2000, 400))
    # a service call (semgrep, free)
    token = cost_context.set(
        CostContext(
            project_id=1,
            orchestration_run_id=rid,
            team="security",
            agent_role="scanner",
            stage="council",
        )
    )
    try:
        await svc_ledger.record(service="semgrep", operation="scan", units=3, est_cost_usd=0.0)
    finally:
        cost_context.reset(token)
    return db, lock, store, rid


async def test_grouped_by_team_and_stage(tmp_path: Path) -> None:
    db, lock, _store, _rid = await _seed(tmp_path)
    budgets = BudgetService(db, lock, BudgetsConfig())
    by_team = await budgets.grouped("team")
    assert any(row["team"] == "security" and row["calls"] == 2 for row in by_team)
    by_stage = await budgets.grouped("stage")
    stages = {row["stage"] for row in by_stage}
    assert {"council", "verdict"} <= stages


async def test_grouped_by_team_rejects_unknown_column(tmp_path: Path) -> None:
    db, lock, _s, _r = await _seed(tmp_path)
    budgets = BudgetService(db, lock, BudgetsConfig())
    with pytest.raises(ValueError, match="cannot group by"):
        await budgets.grouped("secret_column")  # allowlist blocks SQL injection


async def test_grouped_services(tmp_path: Path) -> None:
    db, lock, _s, _r = await _seed(tmp_path)
    budgets = BudgetService(db, lock, BudgetsConfig())
    by_service = await budgets.grouped_services("service")
    assert by_service == [{"service": "semgrep", "cost_usd": 0.0, "calls": 1, "unpriced": 0}]
    by_team = await budgets.grouped_services("team")
    assert by_team[0]["team"] == "security"
    with pytest.raises(ValueError, match="cannot group services by"):
        await budgets.grouped_services("nope")


async def test_service_unpriced_counted_not_zero(tmp_path: Path) -> None:
    db, lock, _s, _r = await _seed(tmp_path)
    lock2 = lock
    ledger = ServiceLedger(db, lock2)
    token = cost_context.set(CostContext(project_id=1, team="research", agent_role="lead"))
    try:
        await ledger.record(service="exa", operation="search", units=2, est_cost_usd=None)
    finally:
        cost_context.reset(token)
    budgets = BudgetService(db, lock, BudgetsConfig())
    rows = {r["service"]: r for r in await budgets.grouped_services("service")}
    assert rows["exa"]["unpriced"] == 1 and rows["exa"]["cost_usd"] == 0.0  # unknown, not $0


async def test_run_breakdown(tmp_path: Path) -> None:
    db, lock, _store, rid = await _seed(tmp_path)
    budgets = BudgetService(db, lock, BudgetsConfig())
    bd = await budgets.run_breakdown(rid)
    assert bd["total"]["calls"] == 2  # two model calls in this run
    roles = {r["agent_role"] for r in bd["by_role"]}
    assert {"security", "planner"} <= roles
    assert bd["services"] == [{"service": "semgrep", "cost_usd": 0.0, "calls": 1}]


def test_roi_arithmetic() -> None:
    budgets = BudgetService.__new__(BudgetService)  # no DB needed for the pure arithmetic
    budgets.config = BudgetsConfig(hourly_rate_usd=120.0)
    roi = budgets.roi(baseline_minutes=45, actual_cost_usd=2.0)
    assert roi["value_usd"] == pytest.approx(90.0)  # 120 * 45/60
    assert roi["net_usd"] == pytest.approx(88.0)
    # fail-closed: unknown cost ⇒ net is None, never a fabricated saving
    assert budgets.roi(45, None)["net_usd"] is None


async def test_orchestration_roi_read_model(tmp_path: Path) -> None:
    from jarvis.ui.readmodels import orchestration_roi

    db, lock, store, rid = await _seed(tmp_path)
    await store.complete_run(rid, status="ok", actual_cost_usd=1.5)
    budgets = BudgetService(db, lock, BudgetsConfig(hourly_rate_usd=100.0))
    rows = await orchestration_roi(store, budgets, project_id=1)
    assert len(rows) == 1
    r = rows[0]
    assert r["run_id"] == rid and r["team"] == "security"
    # security_review baseline is 45m ⇒ value 75.0; net 75.0 - 1.5
    assert r["value_usd"] == pytest.approx(75.0) and r["net_usd"] == pytest.approx(73.5)


async def test_costs_overview_includes_team_and_service(tmp_path: Path) -> None:
    from jarvis.ui.readmodels import costs_overview

    db, lock, _s, _r = await _seed(tmp_path)
    budgets = BudgetService(db, lock, BudgetsConfig())
    overview = await costs_overview(budgets)
    assert "by_team" in overview and "by_service" in overview
    assert any(row["team"] == "security" for row in overview["by_team"])
