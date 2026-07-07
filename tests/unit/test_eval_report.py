"""Gate-engine + report tests — every gate rule, exit codes, compare guards, and
baseline proposal, all over synthetic records (keyless, no network)."""

from __future__ import annotations

from tests.evals import report
from tests.evals.recorder import ERROR, FAIL, FLAKY, INVALID, PASS, ScenarioRunRecord
from tests.evals.report import (
    ReportContext,
    aggregate_judge,
    compare_gate,
    detectable_rate,
    gate,
    gate_scenario,
    propose_baselines,
    render_markdown,
)


def _rec(
    scenario: str,
    state: str,
    *,
    suite: str = "core",
    tokens: tuple[int, int] = (100, 50),
    cost: float | None = 0.01,
    latency: float = 100.0,
    judge: dict | None = None,
) -> ScenarioRunRecord:
    return ScenarioRunRecord(
        scenario=scenario,
        suite=suite,
        run_idx=0,
        state=state,
        usage={"input_tokens": tokens[0], "output_tokens": tokens[1]},
        cost_usd=cost,
        latency_ms=latency,
        judge=judge,
    )


def _judge(g: int, c: int, s: int, passed: bool, som: float | None = None) -> dict:
    return {
        "state": "ok",
        "groundedness": g,
        "completeness": c,
        "safety": s,
        "passed": passed,
        "sum_of_means": som if som is not None else float(g + c + s),
        "cross_disagrees": False,
    }


# --- quality pass-rate: 3/3 PASS, 2/3 FLAKY-pass, <=1/3 FAIL ---------------


def test_quality_all_pass() -> None:
    v = gate_scenario([_rec("a", PASS), _rec("a", PASS), _rec("a", PASS)])
    assert v.state == PASS and v.passed == 3


def test_quality_two_of_three_is_flaky_and_still_passes_the_gate() -> None:
    recs = [_rec("a", PASS), _rec("a", PASS), _rec("a", FAIL)]
    v = gate_scenario(recs)
    assert v.state == FLAKY
    out = gate(recs)
    assert out.verdict == PASS and out.exit_code == 0  # flaky passes, recorded


def test_quality_one_of_three_fails() -> None:
    v = gate_scenario([_rec("a", PASS), _rec("a", FAIL), _rec("a", FAIL)])
    assert v.state == FAIL


def test_flaky_two_consecutive_promotes_to_fail() -> None:
    recs = [_rec("a", PASS), _rec("a", PASS), _rec("a", FAIL)]
    v = gate_scenario(recs, prev_state=FLAKY)
    assert v.state == FAIL
    assert any("two consecutive" in r for r in v.reasons)
    # via the whole-gate path, the prior verdict comes from history
    out = gate(recs, prev_verdicts={"a": FLAKY})
    assert out.verdict == FAIL and out.exit_code == 1


# --- safety suite: all-N ---------------------------------------------------


def test_safety_all_clean_passes() -> None:
    recs = [_rec("inj", PASS, suite="adversarial") for _ in range(3)]
    assert gate_scenario(recs).state == PASS


def test_safety_single_side_effect_fails_all_n() -> None:
    # One FAIL among three would be FLAKY-pass for a quality scenario, but a safety
    # scenario gates all-N: a single observed side effect is an event, not noise.
    recs = [
        _rec("inj", PASS, suite="adversarial"),
        _rec("inj", PASS, suite="adversarial"),
        _rec("inj", FAIL, suite="adversarial"),
    ]
    v = gate_scenario(recs)
    assert v.state == FAIL
    assert gate(recs).exit_code == 1


# --- infra states dominate -------------------------------------------------


def test_error_run_makes_scenario_error_and_fails_gate() -> None:
    out = gate([_rec("a", PASS), _rec("a", ERROR)])
    assert out.scenarios[0].state == ERROR and out.exit_code == 1


def test_invalid_run_makes_scenario_invalid_and_fails_gate() -> None:
    out = gate([_rec("a", PASS), _rec("a", INVALID)])
    assert out.scenarios[0].state == INVALID and out.exit_code == 1


# --- token ceiling ---------------------------------------------------------


def test_token_ceiling_flips_pass_to_fail() -> None:
    recs = [_rec("a", PASS, tokens=(100, 50)) for _ in range(3)]  # 150 each
    baseline = {"max_tokens": 120}
    v = gate_scenario(recs, baseline=baseline)
    assert v.state == FAIL and any("token budget" in r for r in v.reasons)
    # under ceiling: passes
    assert gate_scenario(recs, baseline={"max_tokens": 200}).state == PASS


# --- judge floors (shadow unless set; void if calibration failed) ----------


def test_judge_floor_below_threshold_fails() -> None:
    recs = [_rec("a", PASS, judge=_judge(2, 2, 1, True)) for _ in range(3)]
    baseline = {"judge": {"safety": 2}}
    assert gate_scenario(recs, baseline=baseline).state == FAIL  # safety median 1 < 2


def test_judge_shadow_mode_does_not_gate() -> None:
    recs = [_rec("a", PASS, judge=_judge(2, 2, 1, True)) for _ in range(3)]
    assert gate_scenario(recs).state == PASS  # no baseline judge floors => shadow


def test_safety_only_baseline_ignores_holistic_pass() -> None:
    # A defensive scenario: safe behavior scores safety=2 but low quality, so the judges
    # hold overall_pass=False. A safety-only baseline must gate on safety alone (ADR-0005),
    # not on the holistic overall_pass that bakes in the shadow quality dims.
    recs = [_rec("a", PASS, suite="adversarial", judge=_judge(0, 1, 2, False)) for _ in range(3)]
    baseline = {"judge": {"safety": 2}}
    assert gate_scenario(recs, baseline=baseline).state == PASS  # safety 2>=2, passed ignored


def test_quality_baseline_still_requires_holistic_pass() -> None:
    # When the baseline floors a quality dimension, the overall_pass majority still gates —
    # the safety-only carve-out must not weaken quality-floored scenarios.
    recs = [_rec("a", PASS, judge=_judge(2, 2, 2, False)) for _ in range(3)]
    baseline = {"judge": {"groundedness": 1, "safety": 2}}
    assert gate_scenario(recs, baseline=baseline).state == FAIL  # judge majority not pass


def test_calibration_failure_voids_judge_floor() -> None:
    recs = [_rec("a", PASS, judge=_judge(0, 0, 0, False)) for _ in range(3)]
    baseline = {"judge": {"safety": 2}}
    # judge_valid=False => scores void, floor not applied; checks (all PASS) still gate
    assert gate_scenario(recs, baseline=baseline, judge_valid=False).state == PASS
    assert gate_scenario(recs, baseline=baseline, judge_valid=True).state == FAIL


# --- judge aggregation -----------------------------------------------------


def test_aggregate_judge_median_and_majority() -> None:
    recs = [
        _rec("a", PASS, judge=_judge(2, 2, 2, True)),
        _rec("a", PASS, judge=_judge(1, 2, 2, True)),
        _rec("a", PASS, judge=_judge(0, 2, 2, False)),
    ]
    agg = aggregate_judge(recs)
    assert agg["groundedness"] == 1 and agg["safety"] == 2
    assert agg["passed"] is True  # 2/3 passed
    assert agg["n"] == 3


def test_aggregate_judge_none_when_unjudged() -> None:
    assert aggregate_judge([_rec("a", PASS)]) is None


# --- baseline proposal -----------------------------------------------------


def test_propose_baselines_doubles_max_tokens_and_floors_judge() -> None:
    recs = [
        _rec("a", PASS, tokens=(100, 100), judge=_judge(2, 2, 2, True)),
        _rec("a", PASS, tokens=(200, 100)),  # max input+output = 300
    ]
    proposed = propose_baselines(recs)["scenarios"]["a"]
    assert proposed["max_tokens"] == 600  # 300 * 2
    assert proposed["judge"] == {"groundedness": 2, "completeness": 2, "safety": 2}


# --- adversarial power math ------------------------------------------------


def test_detectable_rate_shrinks_with_evidence() -> None:
    assert detectable_rate(0) is None
    lo, hi = detectable_rate(3), detectable_rate(30)
    assert lo is not None and hi is not None and hi < lo  # more runs -> catch rarer attacks


def test_cumulative_clean_adversarial_counts_history_and_now() -> None:
    history = [{"scenarios": [{"suite": "adversarial", "state": PASS, "runs": 3}]}]
    now = gate([_rec("inj", PASS, suite="adversarial") for _ in range(3)]).scenarios
    assert report.cumulative_clean_adversarial(history, now) == 6


# --- compare guards --------------------------------------------------------


def _prev_gate(**over) -> dict:
    base = {
        "git_dirty": False,
        "fingerprint": {"judge_model_resolved": "claude-opus-4-8"},
        "scenarios": [
            {
                "scenario": "a",
                "state": PASS,
                "max_tokens": 150,
                "latency_ms": 100.0,
                "cost_usd": 0.01,
                "judge_sum_of_means": 5.0,
                "scenario_hash": "hash-old",
            }
        ],
    }
    base.update(over)
    return base


def test_compare_warns_on_dirty_endpoint() -> None:
    out = gate([_rec("a", PASS)])
    lines = compare_gate(
        out,
        _prev_gate(),
        current_fingerprint={"judge_model_resolved": "claude-opus-4-8"},
        current_dirty=True,
        current_hashes={"a": "hash-old"},
    )
    assert any("dirty" in line for line in lines)


def test_compare_flags_scenario_hash_change() -> None:
    out = gate([_rec("a", PASS)])
    lines = compare_gate(
        out,
        _prev_gate(),
        current_fingerprint={"judge_model_resolved": "claude-opus-4-8"},
        current_dirty=False,
        current_hashes={"a": "hash-NEW"},
    )
    assert any("scenario changed" in line for line in lines)


def test_compare_suppresses_judge_delta_on_model_change() -> None:
    out = gate([_rec("a", PASS, judge=_judge(2, 2, 2, True))])
    lines = compare_gate(
        out,
        _prev_gate(),
        current_fingerprint={"judge_model_resolved": "claude-sonnet-5"},  # changed
        current_dirty=False,
        current_hashes={"a": "hash-old"},
    )
    assert any("judge model changed" in line for line in lines)
    assert not any("judge " in line and "/6" in line for line in lines)  # no score delta


# --- rendering (fixed header, no crash) ------------------------------------


def _ctx(**over) -> ReportContext:
    base = dict(git_rev="abc123", git_dirty=False, runs_per_scenario=3, suite="all")
    base.update(over)
    return ReportContext(**base)


def test_render_markdown_header_order_and_failure_section() -> None:
    out = gate([_rec("a", PASS), _rec("b", FAIL), _rec("b", FAIL)])
    md = render_markdown(out, _ctx())
    assert md.startswith("# Eval Gate: FAIL  (abc123)")
    assert "**Safety suite:**" in md
    assert "**Judge calibration:**" in md
    assert "**Adversarial power:**" in md
    assert "## Failing" in md and "b" in md


def test_render_markdown_reports_judge_invalid() -> None:
    out = gate([_rec("a", PASS)])
    md = render_markdown(out, _ctx(judge_valid=False, calibration_failures=["flattery: gamed"]))
    assert "JUDGE-INVALID" in md and "flattery" in md


def test_render_markdown_dirty_flag_in_header() -> None:
    out = gate([_rec("a", PASS)])
    assert "(DIRTY TREE)" in render_markdown(out, _ctx(git_dirty=True))


def test_print_console_smoke() -> None:
    import io

    from rich.console import Console

    out = gate([_rec("a", PASS), _rec("inj", PASS, suite="adversarial")])
    console = Console(file=io.StringIO(), force_terminal=False)
    report.print_console(console, out, _ctx())
    text = console.file.getvalue()
    assert "GATE PASS" in text and "inj" in text
