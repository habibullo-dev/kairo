"""Dreaming run orchestration (Phase 16 Task 7): collect → run one job, chunked, NOT scheduled.

Pins: collectors read only local durable data (tasks + ledger) into plain lines; dream_run wires
collect→run with the budget from config (0 ⇒ disabled ⇒ halt before spending); dedup window = the
day; nothing schedules. Keyless via FakeClient + fakes."""

from __future__ import annotations

import asyncio
import datetime as _dt
from pathlib import Path
from types import SimpleNamespace

import pytest

from jarvis.attention import AttentionState, AttentionStore, collect, dream_run
from jarvis.config import load_config
from jarvis.core.client import FakeClient, text_message
from jarvis.persistence.db import connect

_NOW = _dt.datetime(2026, 7, 10, 21, 0, tzinfo=_dt.UTC)
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


def _task(title, *, next_run_at=None, kind="job", fails=0):
    return SimpleNamespace(
        title=title, next_run_at=next_run_at, kind=kind, consecutive_failures=fails
    )


class _FakeTasks:
    def __init__(self, rows): self._rows = rows
    async def list(self, **_kw): return self._rows


class _FakeLedger:
    def __init__(self, spend): self._spend = spend
    async def spent(self, **_kw): return self._spend


# --- collectors are deterministic, local-only -------------------------------
async def test_collect_morning_briefing_is_due_tasks() -> None:
    tasks = _FakeTasks([
        _task("Standup", next_run_at="2026-07-10T09:00:00+00:00"),
        _task("Next week review", next_run_at="2026-07-17T09:00:00+00:00"),
    ])
    lines = await collect("morning_briefing", tasks=tasks, now=_NOW)
    assert lines == ["Due today: Standup (job)"]  # only today's; not next week's


async def test_collect_nightly_review_has_spend_and_failing_jobs() -> None:
    tasks = _FakeTasks([_task("Flaky sync", fails=3), _task("Healthy", fails=0)])
    lines = await collect("nightly_review", tasks=tasks, ledger=_FakeLedger(0.42), now=_NOW)
    assert "Model spend today: $0.42" in lines
    assert any("Flaky sync" in ln and "3 consecutive" in ln for ln in lines)
    assert not any("Healthy" in ln for ln in lines)


# --- dream_run wires collect→run with the config budget ---------------------
async def test_dream_run_produces_a_proposal(tmp_path: Path) -> None:
    store = await _store(tmp_path)
    cfg = load_config(root=tmp_path, env_file=None)  # attention.dreaming_budget_usd default 1.5
    res = await dream_run(
        "morning_briefing",
        config=cfg,
        attention=store,
        summarizer=FakeClient([text_message("Just standup today.")]),
        tasks=_FakeTasks([_task("Standup", next_run_at="2026-07-10T09:00:00+00:00")]),
        now=_NOW,
    )
    assert res.proposal_id is not None and not res.halted
    items = await store.list(state=AttentionState.OPEN)
    assert len(items) == 1 and items[0].source == "dreaming"


async def test_zero_budget_disables_dreaming(tmp_path: Path) -> None:
    store = await _store(tmp_path)
    cfg = load_config(root=tmp_path, env_file=None)
    cfg.attention = cfg.attention.model_copy(update={"dreaming_budget_usd": 0.0})  # disabled
    client = FakeClient([])  # must not be called
    res = await dream_run(
        "nightly_review", config=cfg, attention=store, summarizer=client,
        tasks=_FakeTasks([]), ledger=_FakeLedger(0.0), now=_NOW,
    )
    assert res.halted and not client.calls  # fail-closed: no summarize when disabled
    assert (await store.list(state=AttentionState.OPEN))[0].kind.value == "alert"


async def test_unknown_job_raises(tmp_path: Path) -> None:
    store = await _store(tmp_path)
    cfg = load_config(root=tmp_path, env_file=None)
    with pytest.raises(ValueError):
        await dream_run("evil_job", config=cfg, attention=store,
                        summarizer=FakeClient([]), now=_NOW)
