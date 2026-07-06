"""Recorder tests: record round-trip, git provenance, fail-closed pricing, history."""

from __future__ import annotations

import json
from pathlib import Path

from tests.evals import recorder
from tests.evals.recorder import PASS, SCHEMA_VERSION, GateRunRecord, ScenarioRunRecord

from jarvis.observability.cost import Usage


def test_scenario_record_roundtrips_as_jsonl(tmp_path: Path) -> None:
    rec = ScenarioRunRecord(
        scenario="kb_ingest",
        suite="core",
        run_idx=0,
        state=PASS,
        usage={"input_tokens": 100, "output_tokens": 20},
        cost_usd=0.01,
        latency_ms=1234.5,
        iterations=2,
        attempts=[
            {
                "name": "write_file",
                "input": {"path": "x"},
                "gate_decision": "ask",
                "resolution": "deny",
            }
        ],
    )
    results = recorder.results_dir(tmp_path, "abc123", ts="20260706-000000")
    path = recorder.write_records(results, [rec])
    (loaded,) = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert loaded["scenario"] == "kb_ingest"
    assert loaded["schema_version"] == SCHEMA_VERSION
    assert loaded["attempts"][0]["resolution"] == "deny"


def test_results_dir_names_by_ts_and_rev(tmp_path: Path) -> None:
    d = recorder.results_dir(tmp_path, "deadbee", ts="20260706-120000")
    assert d.name == "20260706-120000-deadbee"
    assert (d / "transcripts").is_dir()


# --- git provenance --------------------------------------------------------


def test_git_rev_and_dirty_return_sane_values() -> None:
    rev = recorder.git_rev()
    assert isinstance(rev, str) and rev  # a short hash or 'unknown'
    assert isinstance(recorder.git_dirty(), bool)


# --- fail-closed pricing ---------------------------------------------------


def test_record_cost_known_model() -> None:
    cost = recorder.record_cost("claude-opus-4-8", Usage(input_tokens=1_000_000, output_tokens=0))
    assert cost == 5.0  # $5/1M input


def test_record_cost_unknown_model_is_none_not_zero() -> None:
    # The trap: cost_of() returns 0.0 for unknown models, which would pass every
    # budget silently. record_cost returns None so the run becomes ERROR instead.
    assert recorder.record_cost("gpt-9-turbo", Usage(input_tokens=1_000_000)) is None


def test_usage_dict_has_all_token_fields() -> None:
    d = recorder.usage_dict(Usage(input_tokens=1, output_tokens=2, cache_read_input_tokens=3))
    assert d == {
        "input_tokens": 1,
        "output_tokens": 2,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 3,
    }


# --- scenario hash ---------------------------------------------------------


def test_scenario_hash_changes_with_content() -> None:
    a = recorder.scenario_hash("name: x\nprompt: hi\n")
    b = recorder.scenario_hash("name: x\nprompt: HELLO\n")
    assert a != b
    assert recorder.scenario_hash("name: x\nprompt: hi\n") == a  # stable


# --- history (append + lockfile + schema filtering) ------------------------


def _gate(**over) -> GateRunRecord:
    base = dict(
        git_rev="abc123",
        git_dirty=False,
        timestamp="20260706-000000",
        suite="all",
        runs_per_scenario=3,
        verdict=PASS,
    )
    base.update(over)
    return GateRunRecord(**base)


def test_history_append_and_read(tmp_path: Path) -> None:
    hist = tmp_path / "history.jsonl"
    recorder.append_history(hist, _gate(git_rev="rev1"))
    recorder.append_history(hist, _gate(git_rev="rev2"))
    rows = recorder.read_history(hist)
    assert [r["git_rev"] for r in rows] == ["rev1", "rev2"]
    assert not (tmp_path / "history.lock").exists()  # lock released


def test_history_read_skips_unknown_schema(tmp_path: Path) -> None:
    hist = tmp_path / "history.jsonl"
    recorder.append_history(hist, _gate())
    with hist.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"schema_version": 999, "git_rev": "future"}) + "\n")
        f.write("not json at all\n")
    rows = recorder.read_history(hist)
    assert len(rows) == 1  # only the current-schema row survives


# --- workdir save ----------------------------------------------------------


def test_save_workdir_copies_into_results(tmp_path: Path) -> None:
    workdir = tmp_path / "wd"
    (workdir / "logs").mkdir(parents=True)
    (workdir / "jarvis.db").write_text("db", encoding="utf-8")
    (workdir / "logs" / "a.jsonl").write_text("log", encoding="utf-8")
    results = recorder.results_dir(tmp_path / "out", "rev", ts="t")
    rel = recorder.save_workdir(workdir, results, "kb_ingest-run0")
    assert (results / rel / "jarvis.db").read_text(encoding="utf-8") == "db"
    assert (results / rel / "logs" / "a.jsonl").exists()
