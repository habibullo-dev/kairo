"""Cost Center context-reuse read model (S7.5). Keyless — seeds model_calls via CostLedger.

Pins the aggregate rollup (hit tokens, savings, hit-rate, by-provider, top routes) and that an
empty ledger reads as all-zero (never a crash / fabricated value).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

import kira.core  # noqa: F401 - load core first (ledger<->core.context import cycle in isolation)
from kira.observability.cost import Usage, load_pricing
from kira.observability.ledger import CostContext, CostLedger
from kira.persistence.db import connect
from kira.ui.readmodels import cache_reuse_overview

_OPEN: list = []


@pytest.fixture(autouse=True)
async def _close():
    yield
    while _OPEN:
        await _OPEN.pop().close()


async def _ledger(tmp_path: Path) -> CostLedger:
    db = await connect(tmp_path / "c.db")
    _OPEN.append(db)
    return CostLedger(db, asyncio.Lock(), load_pricing(None))


async def _rec(ledger, provider, model, inp, *, hit=None, savings=None, creation=0):
    await ledger.record(
        provider=provider, model=model, effort=None,
        usage=Usage(input_tokens=inp, output_tokens=10, cache_creation_input_tokens=creation),
        latency_ms=1.0, tool_call_count=0, ctx=CostContext(purpose="turn"),
        cached_input_tokens=hit, provider_cache_hit_tokens=hit, estimated_cache_savings_usd=savings,
        provider_cache_mode="automatic_prefix" if hit else None,
    )


async def test_empty_ledger_reads_all_zero(tmp_path: Path) -> None:
    cr = await cache_reuse_overview((await _ledger(tmp_path)).db)
    assert cr["totals"]["hit_tokens"] == 0 and cr["totals"]["hit_rate"] == 0.0
    assert cr["by_provider"] == [] and cr["top_routes"] == []


async def test_aggregates_and_hit_rate(tmp_path: Path) -> None:
    ledger = await _ledger(tmp_path)
    await _rec(ledger, "openai", "gpt-5.2", 1000, hit=800, savings=0.004)
    await _rec(ledger, "openai", "gpt-5.2", 1000, hit=600, savings=0.003)
    await _rec(ledger, "anthropic", "claude", 2000, hit=1500, savings=0.010, creation=200)
    await _rec(ledger, "anthropic", "claude", 500)  # no cache this call (NULL fields)

    cr = await cache_reuse_overview(ledger.db)
    t = cr["totals"]
    assert t["input_tokens"] == 4500
    assert t["hit_tokens"] == 2900  # 800 + 600 + 1500
    assert abs(t["estimated_savings_usd"] - 0.017) < 1e-9
    assert t["cache_write_tokens"] == 200  # anthropic cache_creation
    assert round(t["hit_rate"], 4) == round(2900 / 4500, 4)

    prov = {r["provider"]: r for r in cr["by_provider"]}
    assert prov["openai"]["hit_tokens"] == 1400
    assert prov["anthropic"]["hit_tokens"] == 1500
    # top_routes sorted by estimated savings, biggest first
    assert [r["provider"] for r in cr["top_routes"]] == ["anthropic", "openai"]


async def test_read_model_is_project_scoped(tmp_path: Path) -> None:
    ledger = await _ledger(tmp_path)
    from kira.projects import ProjectStore

    ps = ProjectStore(ledger.db, ledger.lock)
    pid = await ps.create(name="P")
    await ledger.record(
        provider="openai", model="gpt", effort=None,
        usage=Usage(input_tokens=100, output_tokens=1), latency_ms=1.0, tool_call_count=0,
        ctx=CostContext(purpose="turn", project_id=pid), provider_cache_hit_tokens=50,
    )
    scoped = await cache_reuse_overview(ledger.db, project_id=pid)
    assert scoped["totals"]["hit_tokens"] == 50
    other = await cache_reuse_overview(ledger.db, project_id=pid + 999)
    assert other["totals"]["hit_tokens"] == 0  # a different project sees none of it
