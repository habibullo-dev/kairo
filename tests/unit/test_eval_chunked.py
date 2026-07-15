"""Chunked eval profile (Phase 7, Task 9) — the aggregation, over synthetic records.

The full `--suite all` × N live gate doesn't fit the runtime's ~14-min background cap, so
the chunked profile runs each suite as a *sub-run* staged to disk, then merges ALL staged
records into ONE gate record + ONE history line. The load-bearing property (tested here,
keyless): a gate assembled from several sub-runs is indistinguishable from one produced in
a single process — same merged totals, and exactly one entry for `--compare` / FLAKY
promotion / cumulative-clean accounting to read.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from tests.evals import recorder, runner
from tests.evals.recorder import PASS, ScenarioRunRecord


def _rec(scenario: str, suite: str, *, run_idx: int = 0, state: str = PASS) -> ScenarioRunRecord:
    return ScenarioRunRecord(
        scenario=scenario,
        suite=suite,
        run_idx=run_idx,
        state=state,
        usage={
            "input_tokens": 100,
            "output_tokens": 50,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
        },
        cost_usd=0.01,
        latency_ms=100.0,
        scenario_hash=f"h_{scenario}",
    )


def _stage_two_chunks(stage: Path) -> None:
    """Two suite sub-runs staged as separate chunks — the shape the profile produces
    across separate (capped) invocations."""
    runner.write_chunk(
        stage,
        "core",
        [_rec("q_alpha", "core"), _rec("q_beta", "core")],
        hashes={"q_alpha": "h_q_alpha", "q_beta": "h_q_beta"},
        runs=1,
        judge_valid=None,
        calibration_failures=[],
    )
    runner.write_chunk(
        stage,
        "adversarial",
        [_rec("adv_gamma", "adversarial")],
        hashes={"adv_gamma": "h_adv_gamma"},
        runs=1,
        judge_valid=None,
        calibration_failures=[],
    )


# --- staging round-trip -----------------------------------------------------


def test_chunk_roundtrip(tmp_path: Path) -> None:
    stage = tmp_path / "stage"
    runner.write_chunk(
        stage,
        "core",
        [_rec("q_alpha", "core")],
        hashes={"q_alpha": "h_q_alpha"},
        runs=3,
        judge_valid=True,
        calibration_failures=[],
    )
    assert runner.chunk_staged(stage, "core")
    records, meta = runner.read_chunk(stage, "core")
    assert [r.scenario for r in records] == ["q_alpha"]
    assert meta["runs"] == 3
    assert meta["judge_valid"] is True
    assert meta["hashes"] == {"q_alpha": "h_q_alpha"}


def test_chunk_not_staged_until_meta_written(tmp_path: Path) -> None:
    stage = tmp_path / "stage"
    stage.mkdir()
    # records present but no meta sidecar => not complete (guards a half-written chunk).
    runner._chunk_records_path(stage, "core").write_text("", encoding="utf-8")
    assert runner.chunk_staged(stage, "core") is False


# --- per-scenario resume (survives the ~14-min cap) ------------------------


def test_partial_roundtrip(tmp_path: Path) -> None:
    stage = tmp_path / "stage"
    stage.mkdir()
    runner._save_partial(stage, "core", {"a", "b"}, {"a": "h1", "b": "h2"})
    done, hashes = runner._load_partial(stage, "core")
    assert done == {"a", "b"}
    assert hashes == {"a": "h1", "b": "h2"}


def test_partial_from_stale_rev_is_discarded_with_its_records(tmp_path: Path) -> None:
    stage = tmp_path / "stage"
    stage.mkdir()
    runner._append_chunk_records(stage, "core", [_rec("a", "core")])
    runner._chunk_partial_path(stage, "core").write_text(
        json.dumps({"rev": "deadbeef", "done": ["a"], "hashes": {"a": "h"}}), encoding="utf-8"
    )
    done, hashes = runner._load_partial(stage, "core")
    assert done == set() and hashes == {}  # stale rev ⇒ reset (never mix two commits)
    assert not runner._chunk_records_path(stage, "core").exists()  # its records cleared too


def test_incremental_append_then_meta_completes_chunk(tmp_path: Path) -> None:
    stage = tmp_path / "stage"
    stage.mkdir()
    runner._append_chunk_records(stage, "core", [_rec("a", "core")])
    runner._append_chunk_records(stage, "core", [_rec("b", "core")])
    assert runner.chunk_staged(stage, "core") is False  # incremental records, no meta yet
    runner._write_chunk_meta(
        stage,
        "core",
        hashes={"a": "h", "b": "h"},
        runs=1,
        judge_valid=None,
        calibration_failures=[],
    )
    assert runner.chunk_staged(stage, "core") is True  # meta = completion marker
    records, _meta = runner.read_chunk(stage, "core")
    assert sorted(r.scenario for r in records) == ["a", "b"]


async def test_run_chunk_is_a_noop_when_already_complete(tmp_path: Path) -> None:
    # An already-complete chunk returns immediately — before load_scenarios/calibration or
    # any network — so re-invoking the profile never re-runs a finished suite.
    from jarvis.config import load_config

    stage = tmp_path / "stage"
    runner.write_chunk(
        stage, "core", [_rec("a", "core")], hashes={"a": "h"}, runs=1,
        judge_valid=None, calibration_failures=[],
    )
    config = load_config(root=tmp_path, env_file=None)
    code = await runner.run_chunk(
        config, suite="core", runs=1, no_judge=True, judge_client=None, stage=stage
    )
    assert code == 0


# --- merge (the guards) -----------------------------------------------------


def test_merge_combines_records_and_hashes(tmp_path: Path) -> None:
    stage = tmp_path / "stage"
    _stage_two_chunks(stage)
    all_records, hashes, meta = runner.merge_chunks(stage, ("core", "adversarial"))
    assert sorted(r.scenario for r in all_records) == ["adv_gamma", "q_alpha", "q_beta"]
    assert set(hashes) == {"q_alpha", "q_beta", "adv_gamma"}
    assert meta["rev"] == recorder.git_rev()  # both chunks staged at the same rev


def test_merge_missing_chunk_raises(tmp_path: Path) -> None:
    stage = tmp_path / "stage"
    runner.write_chunk(
        stage,
        "core",
        [_rec("q_alpha", "core")],
        hashes={"q_alpha": "h_q_alpha"},
        runs=1,
        judge_valid=None,
        calibration_failures=[],
    )
    with pytest.raises(ValueError, match="missing staged chunk"):
        runner.merge_chunks(stage, ("core", "adversarial"))


def test_merge_refuses_rev_mismatch(tmp_path: Path) -> None:
    stage = tmp_path / "stage"
    _stage_two_chunks(stage)
    # Simulate one chunk having been produced at a different commit — mixing them would
    # make the gate a lie, so the merge must refuse.
    meta_path = runner._chunk_meta_path(stage, "adversarial")
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["rev"] = "deadbeef"
    meta_path.write_text(json.dumps(meta), encoding="utf-8")
    with pytest.raises(ValueError, match="different revs"):
        runner.merge_chunks(stage, ("core", "adversarial"))


def test_merge_judge_valid_false_wins(tmp_path: Path) -> None:
    stage = tmp_path / "stage"
    runner.write_chunk(
        stage,
        "core",
        [_rec("q_alpha", "core")],
        hashes={},
        runs=1,
        judge_valid=True,
        calibration_failures=[],
    )
    runner.write_chunk(
        stage,
        "adversarial",
        [_rec("adv_gamma", "adversarial")],
        hashes={},
        runs=1,
        judge_valid=False,
        calibration_failures=["bad_fixture"],
    )
    _records, _hashes, meta = runner.merge_chunks(stage, ("core", "adversarial"))
    assert meta["judge_valid"] is False  # any invalid chunk voids the aggregate's judge
    assert meta["calibration_failures"] == ["bad_fixture"]


# --- aggregation: one gate record, one history line -------------------------


def test_aggregate_produces_one_history_line(tmp_path: Path) -> None:
    from jarvis.config import load_config

    stage = tmp_path / "stage"
    _stage_two_chunks(stage)
    config = load_config(root=tmp_path, env_file=None)

    code = asyncio.run(runner.aggregate_staged(config, stage=stage, chunks=("core", "adversarial")))
    assert code == 0  # all synthetic runs PASS

    history = recorder.read_history(config.evals_dir / "history.jsonl")
    assert len(history) == 1  # ONE gate for the whole chunked run, not one per suite
    entry = history[0]
    assert entry["suite"] == "live-chunked"
    # All three scenarios from both chunks are in the single gate record's summaries.
    assert sorted(s["scenario"] for s in entry["scenarios"]) == ["adv_gamma", "q_alpha", "q_beta"]
    assert entry["totals"]["by_state"].get(PASS) == 3
    assert entry["verdict"] == "PASS"


def test_compare_and_cumulative_see_one_entry(tmp_path: Path) -> None:
    from jarvis.config import load_config

    stage = tmp_path / "stage"
    _stage_two_chunks(stage)
    config = load_config(root=tmp_path, env_file=None)

    asyncio.run(runner.aggregate_staged(config, stage=stage, chunks=("core", "adversarial")))
    history = recorder.read_history(config.evals_dir / "history.jsonl")

    # `--compare <rev>` resolves to exactly the one aggregated gate (not several sub-runs).
    found = runner._find_gate(history, recorder.git_rev())
    assert found is not None
    assert found["suite"] == "live-chunked"
    assert len([h for h in history if h["git_rev"] == found["git_rev"]]) == 1


def test_aggregate_missing_chunk_returns_usage_error(tmp_path: Path) -> None:
    from jarvis.config import load_config

    stage = tmp_path / "stage"  # nothing staged
    config = load_config(root=tmp_path, env_file=None)
    code = asyncio.run(runner.aggregate_staged(config, stage=stage, chunks=("core", "adversarial")))
    assert code == 2  # can't gate what wasn't run
    assert recorder.read_history(config.evals_dir / "history.jsonl") == []
