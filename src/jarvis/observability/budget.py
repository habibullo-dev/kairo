"""Budgets + cost rollups over the model_calls ledger (Phase 10 Task 8).

Read models (periodised + grouped spend, for the Costs screen) plus budget checks (soft warn
/ hard stop per run, project monthly, per-role cap). Every rollup surfaces ``unpriced`` — the
count of NULL-cost rows — separately, so an unpriced model is shown as *unknown*, never
silently summed as $0 (the ledger's fail-closed contract carries through to the UI).

Time windows use the local calendar (day / ISO-week / month), matching how a person reads
"today"/"this month". Orchestration-run enforcement (reserve-before-fan-out, between-stage
hard stop) lives in the engine (10B) and calls :meth:`BudgetService.run_spend`.
"""

from __future__ import annotations

import asyncio
import datetime as _dt

import aiosqlite

from jarvis.config import BudgetsConfig


def _local_now() -> _dt.datetime:
    return _dt.datetime.now().astimezone()


def _period_start(now: _dt.datetime, period: str) -> _dt.datetime:
    """Local-calendar start of ``day`` / ``week`` (Monday) / ``month`` containing ``now``."""
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if period == "day":
        return midnight
    if period == "week":
        return midnight - _dt.timedelta(days=now.weekday())
    if period == "month":
        return midnight.replace(day=1)
    raise ValueError(f"unknown period: {period!r}")


class BudgetService:
    """Cost rollups + budget-limit checks. Reads the ledger; never writes it."""

    def __init__(
        self,
        db: aiosqlite.Connection,
        lock: asyncio.Lock | None = None,
        config: BudgetsConfig | None = None,
    ) -> None:
        self.db = db
        self.lock = lock or asyncio.Lock()
        self.config = config or BudgetsConfig()

    async def _sum(self, where: str, params: tuple) -> dict:
        """(cost_usd, calls, unpriced) over model_calls matching ``where``. Unpriced (NULL
        cost) rows are counted separately, never summed as $0."""
        cur = await self.db.execute(
            "SELECT COALESCE(SUM(cost_usd), 0.0), COUNT(*), "
            "SUM(CASE WHEN cost_usd IS NULL THEN 1 ELSE 0 END) "
            f"FROM model_calls {where}",
            params,
        )
        row = await cur.fetchone()
        return {"cost_usd": round(row[0], 6), "calls": row[1], "unpriced": row[2] or 0}

    async def period_spend(self, period: str, *, project_id: int | None = None) -> dict:
        start = _period_start(_local_now(), period).astimezone(_dt.UTC).isoformat()
        where = "WHERE ts >= ?"
        params: list[object] = [start]
        if project_id is not None:
            where += " AND project_id = ?"
            params.append(project_id)
        return await self._sum(where, tuple(params))

    #: Columns a caller may GROUP BY on model_calls — an allowlist (never interpolate raw input).
    _MODEL_GROUP_COLS = frozenset(
        {"purpose", "agent_role", "model", "provider", "team", "stage", "project_id"}
    )
    #: Columns a caller may GROUP BY on service_calls (Task 17).
    _SERVICE_GROUP_COLS = frozenset({"service", "team", "agent_role", "stage"})

    async def grouped(
        self, by: str, *, project_id: int | None = None, since: str | None = None
    ) -> list[dict]:
        """Spend grouped by one of ``purpose`` / ``agent_role`` / ``model`` / ``provider`` /
        ``team`` / ``stage`` — the 'why this cost' breakdown. Rows ordered by cost descending."""
        if by not in self._MODEL_GROUP_COLS:
            raise ValueError(f"cannot group by {by!r}")
        where_parts: list[str] = []
        params: list[object] = []
        if project_id is not None:
            where_parts.append("project_id = ?")
            params.append(project_id)
        if since is not None:
            where_parts.append("ts >= ?")
            params.append(since)
        where = ("WHERE " + " AND ".join(where_parts)) if where_parts else ""
        cur = await self.db.execute(
            f"SELECT {by}, COALESCE(SUM(cost_usd), 0.0), COUNT(*), "
            "SUM(CASE WHEN cost_usd IS NULL THEN 1 ELSE 0 END) "
            f"FROM model_calls {where} GROUP BY {by} ORDER BY 2 DESC",
            tuple(params),
        )
        return [
            {by: r[0], "cost_usd": round(r[1], 6), "calls": r[2], "unpriced": r[3] or 0}
            for r in await cur.fetchall()
        ]

    async def grouped_services(
        self, by: str = "service", *, project_id: int | None = None, since: str | None = None
    ) -> list[dict]:
        """Service spend (over ``service_calls``) grouped by ``service`` / ``team`` /
        ``agent_role`` / ``stage``. Unpriced (NULL est_cost) rows are counted separately — a
        metered service with no pricing is shown as unknown, never summed as $0 (Task 17)."""
        if by not in self._SERVICE_GROUP_COLS:
            raise ValueError(f"cannot group services by {by!r}")
        where_parts: list[str] = []
        params: list[object] = []
        if project_id is not None:
            where_parts.append("project_id = ?")
            params.append(project_id)
        if since is not None:
            where_parts.append("ts >= ?")
            params.append(since)
        where = ("WHERE " + " AND ".join(where_parts)) if where_parts else ""
        cur = await self.db.execute(
            f"SELECT {by}, COALESCE(SUM(est_cost_usd), 0.0), COUNT(*), "
            "SUM(CASE WHEN est_cost_usd IS NULL THEN 1 ELSE 0 END) "
            f"FROM service_calls {where} GROUP BY {by} ORDER BY 3 DESC",
            tuple(params),
        )
        return [
            {by: r[0], "cost_usd": round(r[1], 6), "calls": r[2], "unpriced": r[3] or 0}
            for r in await cur.fetchall()
        ]

    async def run_spend(self, orchestration_run_id: int) -> dict:
        """Total spend for one orchestration run (the engine checks this between stages)."""
        return await self._sum("WHERE orchestration_run_id = ?", (orchestration_run_id,))

    async def run_breakdown(self, orchestration_run_id: int) -> dict:
        """Per-run cost attribution for the Studio detail / ROI: LLM spend by role and by stage,
        plus service invocations by service. Metadata only."""
        rid = ("WHERE orchestration_run_id = ?", (orchestration_run_id,))

        async def _group(col: str) -> list[dict]:
            cur = await self.db.execute(
                f"SELECT {col}, COALESCE(SUM(cost_usd), 0.0), COUNT(*), "
                "SUM(CASE WHEN cost_usd IS NULL THEN 1 ELSE 0 END) "
                f"FROM model_calls {rid[0]} GROUP BY {col} ORDER BY 2 DESC",
                rid[1],
            )
            return [
                {col: r[0], "cost_usd": round(r[1], 6), "calls": r[2], "unpriced": r[3] or 0}
                for r in await cur.fetchall()
            ]

        cur = await self.db.execute(
            "SELECT service, COALESCE(SUM(est_cost_usd), 0.0), COUNT(*) "
            f"FROM service_calls {rid[0]} GROUP BY service ORDER BY 3 DESC",
            rid[1],
        )
        services = [
            {"service": r[0], "cost_usd": round(r[1], 6), "calls": r[2]}
            for r in await cur.fetchall()
        ]
        return {
            "total": await self.run_spend(orchestration_run_id),
            "by_role": await _group("agent_role"),
            "by_stage": await _group("stage"),
            "services": services,
        }

    def roi(self, baseline_minutes: int, actual_cost_usd: float | None) -> dict:
        """ROI for one run: the human-time value it stood in for, minus what it cost. Value =
        baseline_minutes × the configured hourly rate; ``net`` is None when the cost is unknown
        (fail-closed — never claim a savings we can't price)."""
        value = round(self.config.hourly_rate_usd * baseline_minutes / 60.0, 4)
        net = None if actual_cost_usd is None else round(value - actual_cost_usd, 4)
        return {
            "baseline_minutes": baseline_minutes,
            "value_usd": value,
            "actual_cost_usd": actual_cost_usd,
            "net_usd": net,
        }

    def check_run(self, spent_usd: float) -> str:
        """Gate an orchestration run's accumulated spend against the per-run limits:
        ``ok`` | ``soft`` (warn, keep going) | ``hard`` (stop). Soft below hard."""
        if self.config.hard_stop_usd_per_run and spent_usd >= self.config.hard_stop_usd_per_run:
            return "hard"
        if self.config.soft_warn_usd_per_run and spent_usd >= self.config.soft_warn_usd_per_run:
            return "soft"
        return "ok"

    async def project_month_exceeded(self, project_id: int) -> bool:
        """True if this project's month-to-date spend is at/over its monthly cap (0/None ⇒
        no cap). Checked before starting a run."""
        cap = self.config.project_monthly_usd
        if not cap:
            return False
        spend = await self.period_spend("month", project_id=project_id)
        return spend["cost_usd"] >= cap

    async def status(self, *, project_id: int | None = None) -> dict:
        """The Costs-screen summary: today/week/month spend + the configured limits + the ROI
        inputs. Everything read-only over the ledger."""
        return {
            "today": await self.period_spend("day", project_id=project_id),
            "week": await self.period_spend("week", project_id=project_id),
            "month": await self.period_spend("month", project_id=project_id),
            "limits": {
                "soft_warn_usd_per_run": self.config.soft_warn_usd_per_run,
                "hard_stop_usd_per_run": self.config.hard_stop_usd_per_run,
                "project_monthly_usd": self.config.project_monthly_usd,
                "per_role_max_usd": self.config.per_role_max_usd,
                "confirm_above_usd": self.config.confirm_above_usd,
            },
            "hourly_rate_usd": self.config.hourly_rate_usd,
        }
