"""`kira dream run <job>` CLI surface (Phase 16 Task 9) — arg validation only (the end-to-end
run is covered by test_dreaming_runner). Pins: only known jobs are runnable; a subcommand is
required; there is NO 'schedule' verb (scheduling is Task 10, post-Checkpoint-K)."""

from __future__ import annotations

import pytest

from kira.attention import JOBS
from kira.cli.dream import dream_cli


def test_unknown_job_is_rejected() -> None:
    with pytest.raises(SystemExit):  # argparse choices bar an unknown/hostile job name
        dream_cli(["run", "rm_rf_everything"])


def test_subcommand_is_required() -> None:
    with pytest.raises(SystemExit):
        dream_cli([])


def test_no_schedule_verb_exists() -> None:
    # The attended CLI can ONLY 'run' — it cannot schedule (unattended scheduling is gated behind
    # Checkpoint K). A 'schedule' subcommand must not parse.
    with pytest.raises(SystemExit):
        dream_cli(["schedule", "nightly_review"])


def test_known_jobs_are_the_expected_set() -> None:
    assert set(JOBS) == {
        "nightly_review", "morning_briefing", "bottleneck", "roi_summary", "self_improvement",
    }
