"""Service cost caps (Phase 13 Task 8): a metered call is refused BEFORE it is sent when it would
breach the per-run / per-day cap. Keyless. Covers the ServiceBudget arithmetic (fake ledger), the
real ledger sum, the end-to-end cap-halt through an adapter, and the orchestration reservation's
flat per-op service cost."""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest

import jarvis.core  # noqa: F401 - load core first (ledger<->core.context import cycle in isolation)
from jarvis.config import load_config
from jarvis.observability.cost import PricingTable
from jarvis.observability.ledger import CostContext, ServiceBudget, ServiceLedger, _day_start
from jarvis.persistence.db import connect
from jarvis.services.exa import ExaSearchTool
from jarvis.tools.base import ToolContext

_OPEN: list = []


@pytest.fixture(autouse=True)
def _reset_transport():
    yield
    ExaSearchTool.transport = None


@pytest.fixture(autouse=True)
async def _close():
    yield
    while _OPEN:
        await _OPEN.pop().close()


# --- ServiceBudget arithmetic (fake ledger, no DB) --------------------------


class _FakeLedger:
    def __init__(self, *, run: float = 0.0, day: float = 0.0) -> None:
        self._run, self._day = run, day

    async def spent(self, *, run_id=None, since=None) -> float:
        return self._run if run_id is not None else self._day


async def test_day_cap_refuses_when_next_would_breach() -> None:
    b = ServiceBudget(max_usd_per_day=0.01)
    ctx = CostContext(purpose="turn")  # no orchestration run
    assert await b.refusal(_FakeLedger(day=0.008), ctx, 0.001) is None  # 0.009 <= 0.01
    reason = await b.refusal(_FakeLedger(day=0.008), ctx, 0.005)  # 0.013 > 0.01
    assert reason is not None and "daily service cost cap" in reason


async def test_run_cap_only_applies_inside_a_run() -> None:
    b = ServiceBudget(max_usd_per_run=0.02)
    in_run = CostContext(purpose="orchestration", orchestration_run_id=5)
    assert await b.refusal(_FakeLedger(run=0.018), in_run, 0.001) is None  # 0.019 <= 0.02
    assert "run" in (await b.refusal(_FakeLedger(run=0.018), in_run, 0.005) or "")  # 0.023 > 0.02
    # Interactive (no run) ⇒ the per-run cap is skipped entirely, even with huge prior run spend.
    assert await b.refusal(_FakeLedger(run=999), CostContext(purpose="turn"), 0.005) is None


async def test_zero_none_and_no_caps_never_refuse() -> None:
    tiny = ServiceBudget(max_usd_per_day=0.0001)
    ctx = CostContext()
    assert await tiny.refusal(_FakeLedger(day=999), ctx, 0.0) is None  # fixed-zero
    assert await tiny.refusal(_FakeLedger(day=999), ctx, None) is None  # unpriced
    assert await ServiceBudget().refusal(_FakeLedger(day=999), ctx, 100.0) is None  # no caps set


# --- real ledger sum --------------------------------------------------------


async def test_ledger_spent_sums_priced_only(tmp_path: Path) -> None:
    db = await connect(tmp_path / "s.db")
    _OPEN.append(db)
    ledger = ServiceLedger(db, asyncio.Lock(), "test")
    ctx = CostContext(purpose="turn")
    await ledger.record(service="exa", operation="search", units=1, est_cost_usd=0.005, ctx=ctx)
    await ledger.record(service="exa", operation="search", units=1, est_cost_usd=0.005, ctx=ctx)
    await ledger.record(service="x", operation="op", units=1, est_cost_usd=None, ctx=ctx)  # NULL→0
    assert abs(await ledger.spent(since=_day_start()) - 0.01) < 1e-9
    assert await ledger.spent(since="2999-01-01T00:00:00+00:00") == 0.0  # future window ⇒ none


# --- end-to-end cap-halt through the exa adapter ----------------------------


def _exa_cfg(tmp_path: Path, *, cap_day: float) -> object:
    cfgdir = tmp_path / "config"
    cfgdir.mkdir(exist_ok=True)
    (cfgdir / "pricing.yaml").write_text(
        "schema_version: test\nmodels:\n  anthropic:\n"
        "    claude-opus-4-8: {input: 5.0, output: 25.0}\n"
        "services:\n  exa: {unit: search, usd_per_unit: 0.005}\n",
        encoding="utf-8",
    )
    cfg = load_config(root=tmp_path, env_file=None)
    cfg.services.enabled = ["exa"]
    cfg.services.max_usd_per_day = cap_day
    cfg.secrets = cfg.secrets.model_copy(update={"exa_api_key": "k"})
    return cfg


def _ok() -> httpx.Response:
    return httpx.Response(200, json={"results": [{"title": "t", "url": "u", "highlights": ["s"]}]})


async def test_third_call_halts_after_two_consume_the_cap(tmp_path: Path) -> None:
    cfg = _exa_cfg(tmp_path, cap_day=0.01)  # exa is $0.005 ⇒ two calls = $0.01, a third breaches
    db = await connect(tmp_path / "svc.db")
    _OPEN.append(db)
    ledger = ServiceLedger(db, asyncio.Lock(), "test")
    ExaSearchTool.transport = httpx.MockTransport(lambda _req: _ok())

    async def call(q: str):
        tool = ExaSearchTool(ToolContext(config=cfg, service_ledger=ledger))
        return await tool.run(tool.Params(query=q, max_results=3))

    assert not (await call("a")).is_error  # $0.005 spent
    assert not (await call("b")).is_error  # $0.010 spent (== cap, allowed)
    third = await call("c")  # would be $0.015 > $0.010 cap ⇒ refused, never sent
    assert third.is_error and "cap" in third.content
    cur = await db.execute("SELECT COUNT(*) FROM service_calls")
    assert (await cur.fetchone())[0] == 2  # only the two that actually ran were billed


# --- orchestration reservation includes flat per-op service costs -----------


def _pricing(**services) -> PricingTable:
    return PricingTable(
        version="t", effective="", cache_write_multiplier=1.25, cache_read_multiplier=0.1,
        models={"anthropic": {}}, services=services,
    )


def test_reservation_includes_metered_service_flat_cost() -> None:
    from jarvis.orchestration.estimate import _service_cost

    pricing = _pricing(firecrawl={"unit": "page", "usd_per_unit": 0.001})
    cost, unpriced = _service_cost(frozenset({"firecrawl"}), turns=3, pricing=pricing)
    assert cost == pytest.approx(0.003) and unpriced == ()  # 3 worst-case ops × $0.001
    # a fixed-zero local service contributes a known $0; a metered service with no row is unpriced
    zero, _ = _service_cost(frozenset({"searxng"}), turns=3, pricing=pricing)
    assert zero == 0.0
    none, names = _service_cost(frozenset({"exa"}), turns=3, pricing=pricing)  # not in this table
    assert none is None and names == ("exa",)  # unpriced ⇒ fail-closed (reservation blocks)
