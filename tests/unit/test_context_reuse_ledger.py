"""Normalized cross-provider cache usage + savings + the ledger write path (S7.4). Keyless.

Pins: each provider's raw usage maps to the right normalized fields (absent ⇒ None, never a
fabricated 0); an unknown provider yields all-None; the savings estimate; and that CostLedger
persists the normalized cache columns (and leaves them NULL when caching is off).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from jarvis.models.context_reuse import estimated_cache_savings, normalize_cache_usage
from jarvis.observability.cost import Usage, load_pricing
from jarvis.observability.ledger import CostContext, CostLedger
from jarvis.persistence.db import connect

_OPEN: list = []


@pytest.fixture(autouse=True)
async def _close():
    yield
    while _OPEN:
        await _OPEN.pop().close()


# --- normalization (pure) --------------------------------------------------


def test_anthropic_usage_maps_creation_and_read() -> None:
    n = normalize_cache_usage(
        "anthropic", {"cache_creation_input_tokens": 200, "cache_read_input_tokens": 1800}
    )
    assert n["cache_creation_tokens"] == 200
    assert n["cache_read_tokens"] == 1800
    assert n["provider_cache_hit_tokens"] == 1800
    assert n["cached_input_tokens"] is None


def test_openai_usage_maps_cached_tokens() -> None:
    n = normalize_cache_usage("openai", {"prompt_tokens_details": {"cached_tokens": 900}})
    assert n["cached_input_tokens"] == 900 and n["provider_cache_hit_tokens"] == 900


def test_deepseek_and_gemini_usage() -> None:
    d = normalize_cache_usage("deepseek", {"prompt_cache_hit_tokens": 512})
    assert d["cached_input_tokens"] == 512 and d["provider_cache_hit_tokens"] == 512
    g = normalize_cache_usage("gemini", {"cachedContentTokenCount": 700})
    assert g["cached_input_tokens"] == 700


def test_absent_fields_are_none_not_zero() -> None:
    n = normalize_cache_usage("anthropic", {})  # no cache usage this call
    assert all(v is None for v in n.values())  # None (honest), never a fabricated 0


def test_unknown_provider_yields_all_none() -> None:
    assert all(v is None for v in normalize_cache_usage("mystery", {"whatever": 1}).values())


def test_estimated_savings() -> None:
    # 1000 hit tokens at $2/1M input ($0.000002/token) on anthropic (0.9 fraction) ⇒ ~$0.0018.
    s = estimated_cache_savings("anthropic", 1000, 0.000002)
    assert s is not None and abs(s - 0.0018) < 1e-9
    assert estimated_cache_savings("anthropic", None, 0.000002) is None  # nothing hit
    assert estimated_cache_savings("anthropic", 1000, None) is None  # unpriced
    assert estimated_cache_savings("mystery", 1000, 0.000002) is None  # unknown provider


# --- ledger write path -----------------------------------------------------


async def _ledger(tmp_path: Path) -> CostLedger:
    db = await connect(tmp_path / "l.db")
    _OPEN.append(db)
    return CostLedger(db, asyncio.Lock(), load_pricing(None))


async def test_ledger_persists_cache_fields(tmp_path: Path) -> None:
    ledger = await _ledger(tmp_path)
    await ledger.record(
        provider="openai", model="gpt-5.2", effort=None,
        usage=Usage(input_tokens=1000, output_tokens=100), latency_ms=5.0, tool_call_count=0,
        ctx=CostContext(purpose="turn"),
        cached_input_tokens=800, provider_cache_mode="automatic_prefix",
        provider_cache_hit_tokens=800, estimated_cache_savings_usd=0.004,
        stable_prefix_hash="abc123",
    )
    cur = await ledger.db.execute(
        "SELECT cached_input_tokens, provider_cache_mode, provider_cache_hit_tokens, "
        "estimated_cache_savings_usd, stable_prefix_hash FROM model_calls"
    )
    assert await cur.fetchone() == (800, "automatic_prefix", 800, 0.004, "abc123")


async def test_ledger_leaves_cache_fields_null_when_off(tmp_path: Path) -> None:
    ledger = await _ledger(tmp_path)
    await ledger.record(
        provider="anthropic", model="claude", effort=None,
        usage=Usage(input_tokens=100, output_tokens=20), latency_ms=1.0, tool_call_count=0,
        ctx=CostContext(purpose="turn"),
    )
    cur = await ledger.db.execute(
        "SELECT cached_input_tokens, provider_cache_mode, stable_prefix_hash FROM model_calls"
    )
    assert await cur.fetchone() == (None, None, None)  # NULL when caching is off — never 0
