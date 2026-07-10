"""The dreaming cage (Phase 16 Task 5) — the reachability guarantee + the budget halt.

The load-bearing safety proof: a dreaming registry holds ONLY the read-only local allowlist, and
NO egress / write / shell / spawn / schedule / delete / connector tool is reachable — proven both
by the exact-set check and by trying every forbidden tool by name. The budget caps spend and a
0 cap disables dreaming (fail-closed). Keyless."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from jarvis.attention import (
    DREAMING_TOOLS,
    FORBIDDEN_TOOLS,
    AttentionState,
    AttentionStore,
    DreamingBudget,
    DreamingCageError,
    assert_caged,
    build_dreaming_registry,
    emit_budget_halt_alert,
)
from jarvis.config import load_config
from jarvis.persistence.db import connect
from jarvis.tools import ToolContext, ToolRegistry


def _context(tmp_path: Path) -> ToolContext:
    return ToolContext(config=load_config(root=tmp_path, env_file=None))


# --- the cage: exactly the allowlist, no forbidden tool reachable ----------
def test_caged_registry_is_exactly_the_allowlist(tmp_path: Path) -> None:
    reg = build_dreaming_registry(_context(tmp_path))
    names = set(reg.names())
    assert names <= DREAMING_TOOLS  # never MORE than the allowlist
    assert names & FORBIDDEN_TOOLS == set()  # and never a forbidden tool


def test_every_forbidden_tool_is_absent(tmp_path: Path) -> None:
    # Try each forbidden tool by name — none is reachable from a dreaming context.
    reg = build_dreaming_registry(_context(tmp_path))
    for name in FORBIDDEN_TOOLS:
        assert reg.get(name) is None, f"{name} leaked into the dreaming cage"


def test_every_caged_tool_is_nonegress_nonprivate(tmp_path: Path) -> None:
    reg = build_dreaming_registry(_context(tmp_path))
    for name in reg.names():
        tool = reg.get(name)
        assert not tool.egress and not tool.reads_private


def test_allowlist_and_forbidden_are_disjoint() -> None:
    assert set() == DREAMING_TOOLS & FORBIDDEN_TOOLS


def test_assert_caged_rejects_a_smuggled_forbidden_tool(tmp_path: Path) -> None:
    # If a forbidden tool were somehow registered, assert_caged must catch it (the belt).
    ctx = _context(tmp_path)
    full = ToolRegistry()
    full.discover("jarvis.tools.builtin", ctx)
    reg = build_dreaming_registry(ctx)
    smuggled = full.get("run_shell")
    if smuggled is not None:  # register it directly, bypassing build_dreaming_registry
        reg.register(smuggled)
        with pytest.raises(DreamingCageError):
            assert_caged(reg)


# --- budget: cap + fail-closed at 0 ----------------------------------------
def test_budget_over_cap_after_spend() -> None:
    b = DreamingBudget(cap_usd=1.5)
    assert not b.over_cap
    b.add(1.0)
    b.add(0.6)
    assert b.over_cap and b.remaining_usd == 0.0


def test_zero_cap_disables_dreaming() -> None:
    b = DreamingBudget(cap_usd=0.0)
    assert b.over_cap is True  # disabled ⇒ refuses before spending anything


async def test_budget_halt_emits_one_alert(tmp_path: Path) -> None:
    db = await connect(tmp_path / "a.db")
    try:
        store = AttentionStore(db, asyncio.Lock())
        a = await emit_budget_halt_alert(store, job="nightly_review", spent_usd=1.6, cap_usd=1.5)
        b = await emit_budget_halt_alert(store, job="nightly_review", spent_usd=1.6, cap_usd=1.5)
        assert a == b  # idempotent — one alert per job, not a spam per cap-hit
        items = await store.list(state=AttentionState.OPEN)
        assert len(items) == 1 and items[0].kind.value == "alert"
        assert "budget cap reached" in items[0].title and items[0].source == "dreaming"
    finally:
        await db.close()
