"""BudgetService: rollups + limit checks over the ledger (Phase 10 Task 8).

Keyless: seed model_calls rows directly, then assert period sums, grouped breakdowns, the
unpriced-is-not-$0 rule, and the run/monthly/soft-hard checks."""

from __future__ import annotations

import asyncio
import datetime as _dt
from pathlib import Path

import pytest

from kira.config import BudgetsConfig
from kira.observability.budget import BudgetService, _local_now, _period_start
from kira.persistence.db import connect
from kira.projects import ProjectStore

_OPEN: list = []


@pytest.fixture(autouse=True)
async def _close():
    yield
    while _OPEN:
        await _OPEN.pop().close()


async def _svc(tmp_path: Path, *, config: BudgetsConfig | None = None) -> BudgetService:
    db = await connect(tmp_path / "b.db")
    _OPEN.append(db)
    lock = asyncio.Lock()
    projects = ProjectStore(db, lock)
    await projects.create(name="P1")  # id 1
    await projects.create(name="P2")  # id 2
    return BudgetService(db, lock, config or BudgetsConfig())


async def _seed(
    svc: BudgetService,
    *,
    cost,
    ts: str | None = None,
    project_id: int | None = None,
    purpose: str = "turn",
    role: str | None = None,
    model: str = "claude-opus-4-8",
    run_id: int | None = None,
) -> None:
    ts = ts or _local_now().astimezone(_dt.UTC).isoformat()
    await svc.db.execute(
        "INSERT INTO model_calls (ts, project_id, orchestration_run_id, agent_role, purpose, "
        "provider, model, cost_usd, created_at) VALUES (?, ?, ?, ?, ?, 'anthropic', ?, ?, ?)",
        (ts, project_id, run_id, role, purpose, model, cost, ts),
    )
    await svc.db.commit()


async def test_period_sums(tmp_path: Path) -> None:
    svc = await _svc(tmp_path)
    await _seed(svc, cost=1.0)
    await _seed(svc, cost=2.0)
    # a row from last month must not count in "today"
    old = (_local_now() - _dt.timedelta(days=60)).astimezone(_dt.UTC).isoformat()
    await _seed(svc, cost=99.0, ts=old)
    today = await svc.period_spend("day")
    assert abs(today["cost_usd"] - 3.0) < 1e-9 and today["calls"] == 2


async def test_unpriced_not_summed_as_zero(tmp_path: Path) -> None:
    svc = await _svc(tmp_path)
    await _seed(svc, cost=1.5)
    await _seed(svc, cost=None)  # unpriced
    spend = await svc.period_spend("month")
    assert abs(spend["cost_usd"] - 1.5) < 1e-9  # the NULL row isn't 0.0'd into the sum
    assert spend["unpriced"] == 1  # surfaced as unknown


async def test_scope_by_project(tmp_path: Path) -> None:
    svc = await _svc(tmp_path)
    await _seed(svc, cost=1.0, project_id=1)
    await _seed(svc, cost=2.0, project_id=2)
    assert abs((await svc.period_spend("month", project_id=1))["cost_usd"] - 1.0) < 1e-9


async def test_grouped_breakdown(tmp_path: Path) -> None:
    svc = await _svc(tmp_path)
    await _seed(svc, cost=1.0, purpose="turn")
    await _seed(svc, cost=3.0, purpose="orchestration", role="coder")
    by_purpose = await svc.grouped("purpose")
    assert by_purpose[0]["purpose"] == "orchestration" and by_purpose[0]["cost_usd"] == 3.0
    by_role = await svc.grouped("agent_role")
    assert any(r["agent_role"] == "coder" and r["cost_usd"] == 3.0 for r in by_role)


async def test_grouped_rejects_bad_column(tmp_path: Path) -> None:
    svc = await _svc(tmp_path)
    with pytest.raises(ValueError, match="cannot group by"):
        await svc.grouped("cost_usd; DROP TABLE model_calls")  # not in the allowlist


async def test_check_run_soft_hard(tmp_path: Path) -> None:
    cfg = BudgetsConfig(soft_warn_usd_per_run=1.0, hard_stop_usd_per_run=5.0)
    svc = await _svc(tmp_path, config=cfg)
    assert svc.check_run(0.5) == "ok"
    assert svc.check_run(1.5) == "soft"
    assert svc.check_run(5.0) == "hard"


async def _seed_run(svc: BudgetService, run_id: int) -> None:
    # model_calls.orchestration_run_id FKs to orchestration_runs — create the run row first.
    ts = _local_now().astimezone(_dt.UTC).isoformat()
    await svc.db.execute(
        "INSERT INTO orchestration_runs (id, project_id, workflow, title, config_json, "
        "context_manifest_json, status, started_at, created_at) "
        "VALUES (?, 1, 'wf', 't', '{}', '{}', 'running', ?, ?)",
        (run_id, ts, ts),
    )
    await svc.db.commit()


async def test_run_spend(tmp_path: Path) -> None:
    svc = await _svc(tmp_path)
    await _seed_run(svc, 1)
    await _seed_run(svc, 2)
    await _seed(svc, cost=1.0, run_id=1, purpose="orchestration")
    await _seed(svc, cost=2.0, run_id=1, purpose="orchestration")
    await _seed(svc, cost=9.0, run_id=2, purpose="orchestration")
    assert abs((await svc.run_spend(1))["cost_usd"] - 3.0) < 1e-9


async def test_project_month_cap(tmp_path: Path) -> None:
    svc = await _svc(tmp_path, config=BudgetsConfig(project_monthly_usd=5.0))
    await _seed(svc, cost=4.0, project_id=1)
    assert await svc.project_month_exceeded(1) is False
    await _seed(svc, cost=2.0, project_id=1)  # now 6.0 >= 5.0
    assert await svc.project_month_exceeded(1) is True
    # no cap configured ⇒ never exceeded
    svc2 = await _svc(tmp_path, config=BudgetsConfig(project_monthly_usd=None))
    assert await svc2.project_month_exceeded(1) is False


async def test_status_shape(tmp_path: Path) -> None:
    svc = await _svc(tmp_path)
    st = await svc.status()
    assert set(st) >= {"today", "week", "month", "limits", "hourly_rate_usd"}


def test_period_start_windows() -> None:
    now = _dt.datetime(2026, 7, 15, 14, 30).astimezone()  # a Wednesday
    assert _period_start(now, "day").hour == 0
    assert _period_start(now, "month").day == 1
    assert _period_start(now, "week").weekday() == 0  # Monday
