"""Team/service cost groupings + per-run breakdown + ROI (Phase 10B Task 17).

Over a temp SQLite with seeded model_calls + service_calls rows: the new team/stage groupings,
the service-spend grouping (unpriced counted separately, never $0), the per-run breakdown, and
the ROI arithmetic (value = baseline_minutes × hourly rate − actual; net None when unpriced).
"""

from __future__ import annotations

import asyncio
import sqlite3
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
    from jarvis.ui.readmodels import (
        orchestration_outcome_accounting,
        orchestration_roi,
        orchestration_run_detail,
    )

    db, lock, store, rid = await _seed(tmp_path)
    await store.complete_run(rid, status="ok", verdict="accept", actual_cost_usd=1.5)
    budgets = BudgetService(db, lock, BudgetsConfig(hourly_rate_usd=100.0))
    rows = await orchestration_roi(store, budgets, project_id=1)
    assert len(rows) == 1
    r = rows[0]
    assert r["run_id"] == rid and r["team"] == "security"
    assert r["outcome"] == "review_accepted"
    # security_review baseline is 45m ⇒ value 75.0; net 75.0 - 1.5
    assert r["value_usd"] == pytest.approx(75.0) and r["net_usd"] == pytest.approx(73.5)
    assert orchestration_outcome_accounting(rows) == {
        "completed_runs": 1,
        "review_accepted_runs": 1,
        "known_actual_model_cost_usd": 1.5,
        "unknown_actual_model_cost_runs": 0,
        "known_model_cost_per_review_accepted_run": 1.5,
    }
    detail = await orchestration_run_detail(store, None, rid, budgets=budgets)
    assert detail["roi"]["outcome"] == "review_accepted"
    assert detail["roi"]["net_usd"] == pytest.approx(73.5)


async def test_orchestration_roi_never_credits_nonaccepted_outcomes() -> None:
    from types import SimpleNamespace

    from jarvis.ui.readmodels import orchestration_outcome_accounting, orchestration_roi

    class Store:
        async def list(self, **_kwargs):
            return [
                SimpleNamespace(
                    id=1,
                    config={"team": "security"},
                    workflow="security_review",
                    status="ok",
                    verdict="accept",
                    actual_cost_usd=1.5,
                ),
                SimpleNamespace(
                    id=2,
                    config={"team": "security"},
                    workflow="security_review",
                    status="rejected",
                    verdict="reject",
                    actual_cost_usd=2.5,
                ),
                SimpleNamespace(
                    id=3,
                    config={"team": "security"},
                    workflow="security_review",
                    status="revise",
                    verdict="revise",
                    actual_cost_usd=None,
                ),
                SimpleNamespace(
                    id=4,
                    config={"team": "security"},
                    workflow="security_review",
                    status="cancelled",
                    verdict=None,
                    actual_cost_usd=0.5,
                ),
                SimpleNamespace(
                    id=5,
                    config={"team": "security"},
                    workflow="security_review",
                    status="running",
                    verdict=None,
                    actual_cost_usd=99.0,
                ),
                SimpleNamespace(
                    id=6,
                    config={"team": "security"},
                    workflow="security_review",
                    status="ok",
                    verdict=None,
                    actual_cost_usd=3.0,
                ),
            ]

    budgets = BudgetService.__new__(BudgetService)
    budgets.config = BudgetsConfig(hourly_rate_usd=100.0)
    rows = await orchestration_roi(Store(), budgets)
    by_id = {row["run_id"]: row for row in rows}
    assert by_id[1]["net_usd"] == pytest.approx(73.5)
    assert by_id[2]["outcome"] == "review_rejected"
    assert by_id[3]["outcome"] == "needs_revision"
    assert by_id[4]["outcome"] == "cancelled"
    assert by_id[5]["outcome"] == "in_progress"
    assert by_id[6]["outcome"] == "completed_unreviewed"
    for run_id in (2, 3, 4, 5, 6):
        assert by_id[run_id]["value_usd"] is None and by_id[run_id]["net_usd"] is None
    assert by_id[2]["actual_cost_usd"] == 2.5  # cost remains visible without an ROI claim
    assert orchestration_outcome_accounting(rows) == {
        "completed_runs": 5,
        "review_accepted_runs": 1,
        "known_actual_model_cost_usd": 7.5,
        "unknown_actual_model_cost_runs": 1,
        "known_model_cost_per_review_accepted_run": None,
    }


async def test_costs_overview_includes_team_and_service(tmp_path: Path) -> None:
    from jarvis.ui.readmodels import costs_overview

    db, lock, _s, _r = await _seed(tmp_path)
    budgets = BudgetService(db, lock, BudgetsConfig())
    overview = await costs_overview(budgets)
    assert "by_team" in overview and "by_service" in overview
    assert any(row["team"] == "security" for row in overview["by_team"])


async def test_model_request_health_separates_completed_and_failed_attempts(tmp_path: Path) -> None:
    from jarvis.ui.readmodels import model_request_health_overview

    db = await connect(tmp_path / "health.db")
    _OPEN.append(db)
    lock = asyncio.Lock()
    await ProjectStore(db, lock).create(name="P")
    ledger = CostLedger(db, lock, load_pricing(None))
    ctx = CostContext(project_id=1, purpose="turn")
    for latency in (30.0, 10.0, 20.0):
        await ledger.record(
            provider="anthropic",
            model="claude-fable-5",
            effort=None,
            usage=Usage(1, 1),
            latency_ms=latency,
            tool_call_count=0,
            ctx=ctx,
        )
    await ledger.record_failure(
        provider="anthropic",
        model="claude-fable-5",
        latency_ms=4.0,
        error=TimeoutError("SECRET-FAILURE-CANARY"),
        ctx=ctx,
    )
    await ledger.record_failure(
        provider="openai",
        model="gpt-5",
        latency_ms=8.0,
        error=ConnectionError("SECRET-FAILURE-CANARY"),
        ctx=ctx,
    )
    health = await model_request_health_overview(db, project_id=1, ledger=ledger)
    assert health["totals"] == {
        "attempts": 5,
        "completed_requests": 3,
        "failed_requests": 2,
        "error_rate": 0.4,
        "measured_completed_latency_requests": 3,
        "unmeasured_completed_latency_requests": 0,
        "p50_completed_latency_ms": 20.0,
        "p95_completed_latency_ms": 30.0,
    }
    assert health["by_provider_model"][0]["provider"] == "anthropic"
    assert health["by_provider_model"][0]["failed_requests"] == 1
    assert health["error_classes"] == [
        {"error_class": "ConnectionError", "failed_requests": 1},
        {"error_class": "TimeoutError", "failed_requests": 1},
    ]
    assert health["recording_degraded"]["degraded"] is False


async def test_model_request_health_is_fail_closed_after_telemetry_loss(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jarvis.ui.readmodels import model_request_health_overview

    db = await connect(tmp_path / "incomplete-health.db")
    _OPEN.append(db)
    lock = asyncio.Lock()
    await ProjectStore(db, lock).create(name="P")
    ledger = CostLedger(db, lock, load_pricing(None))
    original_execute = db.execute
    dropped = False

    async def drop_first_failure(sql: str, parameters=None):
        nonlocal dropped
        if "INSERT INTO model_failures" in sql and not dropped:
            dropped = True
            raise sqlite3.OperationalError("database unavailable")
        return await original_execute(sql, parameters)

    monkeypatch.setattr(db, "execute", drop_first_failure)
    ctx = CostContext(project_id=1, purpose="turn")
    await ledger.record_failure(
        provider="anthropic",
        model="claude-fable-5",
        latency_ms=4.0,
        error=TimeoutError("provider unavailable"),
        ctx=ctx,
    )
    # A later successful write clears the live A5 alarm, but must not make health precise again.
    await ledger.record(
        provider="anthropic",
        model="claude-fable-5",
        effort=None,
        usage=Usage(1, 1),
        latency_ms=10.0,
        tool_call_count=0,
        ctx=ctx,
    )
    health = await model_request_health_overview(db, project_id=1, ledger=ledger)
    assert health["recording_degraded"] == {
        **ledger.status(),
        "telemetry_complete": False,
        "lost_records": 1,
    }
    assert health["totals"]["attempts"] == 1 and health["totals"]["failed_requests"] == 0
    assert health["totals"]["error_rate"] is None
    assert health["totals"]["p50_completed_latency_ms"] is None
