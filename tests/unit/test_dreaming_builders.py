"""Dreaming content builders (Phase 16 Task 6) — proposal-only, tool-less, budget-guarded.

Pins: the ONLY outputs are an artifact + ONE attention proposal (untrusted, model_generated); the
summarize call is TOOL-LESS (can never act); collected material is framed untrusted; the model
follows the Haiku/Sonnet policy; over budget ⇒ halt + one alert, no summarize. Keyless via
FakeClient."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from jarvis.attention import (
    JOBS,
    AttentionState,
    AttentionStore,
    DreamingBudget,
    run_dreaming_job,
)
from jarvis.core.client import FakeClient, text_message
from jarvis.persistence.db import connect

_OPEN: list = []


@pytest.fixture(autouse=True)
async def _close():
    yield
    while _OPEN:
        await _OPEN.pop().close()


async def _store(tmp_path: Path) -> AttentionStore:
    db = await connect(tmp_path / "a.db")
    _OPEN.append(db)
    return AttentionStore(db, asyncio.Lock())


class _RecordingRouter:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def notify(self, **kwargs) -> None:
        self.calls.append(kwargs)


async def test_job_produces_one_untrusted_proposal_via_toolless_summary(tmp_path: Path) -> None:
    store = await _store(tmp_path)
    client = FakeClient([text_message("You closed 3 tasks; consider planning tomorrow.")])
    budget = DreamingBudget(cap_usd=1.5)
    res = await run_dreaming_job(
        JOBS["nightly_review"],
        collected=["Closed task: ship Phase 16", "Spent $0.42 today"],
        summarizer=client,
        budget=budget,
        attention=store,
        window="2026-07-10",
    )
    # exactly one attention proposal, untrusted + model_generated, from dreaming
    items = await store.list(state=AttentionState.OPEN)
    assert len(items) == 1
    p = items[0]
    assert p.source == "dreaming" and p.kind.value == "review"
    assert p.trust_class == "model_generated"  # untrusted by default
    assert p.priority.value == "normal"  # dreaming never pushes urgent
    assert p.payload["summary"].startswith("You closed 3 tasks")
    # the summarize call was TOOL-LESS and used the Haiku default (nightly doesn't escalate)
    call = client.calls[-1]
    assert call["tools"] == [] and call["model"] == "claude-haiku-4-5-20251001"
    # collected material was framed untrusted
    assert "untrusted" in call["messages"][-1]["content"]
    assert res.proposal_id == p.id and res.cost_usd is not None and not res.halted


async def test_self_improvement_escalates_to_sonnet(tmp_path: Path) -> None:
    store = await _store(tmp_path)
    client = FakeClient([text_message("Proposal: split the router module.")])
    await run_dreaming_job(
        JOBS["self_improvement"],
        collected=["engine.py is 1200 lines"],
        summarizer=client,
        budget=DreamingBudget(cap_usd=1.5),
        attention=store,
        window="w",
    )
    assert client.calls[-1]["model"] == "claude-sonnet-5"  # escalation tier
    assert (await store.list(state=AttentionState.OPEN))[0].kind.value == "proposal"


async def test_over_budget_halts_before_summarizing(tmp_path: Path) -> None:
    store = await _store(tmp_path)
    client = FakeClient([])  # must NOT be called
    budget = DreamingBudget(cap_usd=1.0, spent_usd=1.0)  # already at cap
    res = await run_dreaming_job(
        JOBS["nightly_review"], collected=["x"], summarizer=client, budget=budget, attention=store,
        window="d",
    )
    assert res.halted and not client.calls  # no summarize call happened
    items = await store.list(state=AttentionState.OPEN)
    assert len(items) == 1 and items[0].kind.value == "alert"  # exactly one halt alert


async def test_rerun_same_window_is_idempotent(tmp_path: Path) -> None:
    store = await _store(tmp_path)
    router = _RecordingRouter()

    async def _run():
        return await run_dreaming_job(
            JOBS["nightly_review"], collected=["x"],
            summarizer=FakeClient([text_message("s")]), budget=DreamingBudget(cap_usd=1.5),
            attention=store, window="2026-07-10", notification_router=router,
        )

    a = await _run()
    b = await _run()
    assert a.proposal_id == b.proposal_id  # same night ⇒ one proposal, not two
    assert len(await store.list(state=AttentionState.OPEN)) == 1
    # The idempotent retry must not re-send an external nudge for the existing durable row.
    assert len(router.calls) == 1
    assert router.calls[0]["open_counts"] == {"review": 1}
    assert "title" not in router.calls[0] and "payload" not in router.calls[0]
