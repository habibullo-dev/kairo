"""Durable, snapshot-bound project-intelligence state (schema v30)."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path

import pytest

from kira.intelligence import (
    AnalysisJobState,
    AnalysisJobStore,
    ProjectReportStore,
)
from kira.orchestration import OrchestrationStore
from kira.persistence.db import connect
from kira.persistence.migrations import latest_version
from kira.projects import ProjectStore

_OPEN: list = []


@pytest.fixture(autouse=True)
async def _close() -> None:
    yield
    while _OPEN:
        await _OPEN.pop().close()


async def _stores(tmp_path: Path):
    db = await connect(tmp_path / "intelligence.db")
    _OPEN.append(db)
    lock = asyncio.Lock()
    project_id = await ProjectStore(db, lock).create(name="Kira")
    return db, AnalysisJobStore(db, lock), ProjectReportStore(db, lock), project_id


async def test_v30_schema_is_additive_and_current(tmp_path: Path) -> None:
    db, _jobs, _reports, _project_id = await _stores(tmp_path)
    version = await (await db.execute("PRAGMA user_version")).fetchone()
    assert version == (latest_version(),)
    names = {
        row[0]
        for row in await (
            await db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name IN ('analysis_jobs', 'project_reports')"
            )
        ).fetchall()
    }
    assert names == {"analysis_jobs", "project_reports"}


async def test_enqueue_is_idempotent_per_snapshot_and_profile(tmp_path: Path) -> None:
    _db, jobs, _reports, project_id = await _stores(tmp_path)
    first, created = await jobs.enqueue(
        project_id=project_id,
        snapshot_hash="abc123",
        profile_version="project-intel-v1",
        graph_watermark=12,
        coverage={"files_total": 9},
    )
    same, created_again = await jobs.enqueue(
        project_id=project_id,
        snapshot_hash="ABC123",  # normalized identity
        profile_version="project-intel-v1",
        graph_watermark=999,
    )
    newer_profile, newer_created = await jobs.enqueue(
        project_id=project_id,
        snapshot_hash="abc123",
        profile_version="project-intel-v2",
    )
    assert (created, created_again, newer_created) == (True, False, True)
    assert same.id == first.id and same.graph_watermark == 12
    assert newer_profile.id != first.id
    assert (await jobs.latest(project_id)).id == newer_profile.id  # type: ignore[union-attr]


async def test_job_lifecycle_claim_requeue_and_finish_is_guarded(tmp_path: Path) -> None:
    _db, jobs, _reports, project_id = await _stores(tmp_path)
    queued, _ = await jobs.enqueue(
        project_id=project_id, snapshot_hash="snap", profile_version="v1"
    )
    running = await jobs.claim(queued.id)
    assert running is not None
    assert running.state is AnalysisJobState.RUNNING and running.attempts == 1
    assert await jobs.claim(queued.id) is None

    assert await jobs.requeue(queued.id, error="interrupted") is True
    running = await jobs.claim(queued.id)
    assert running is not None and running.attempts == 2
    assert await jobs.finish(
        queued.id, AnalysisJobState.FAILED, error="provider unavailable"
    ) is True
    terminal = await jobs.get(queued.id)
    assert terminal is not None and terminal.state is AnalysisJobState.FAILED
    assert await jobs.requeue(queued.id) is False


async def test_report_publish_is_idempotent_and_marks_prior_snapshot_stale(tmp_path: Path) -> None:
    _db, _jobs, reports, project_id = await _stores(tmp_path)
    first, created = await reports.create(
        project_id=project_id,
        snapshot_hash="one",
        profile_version="v1",
        summary="First baseline",
        coverage={"files_total": 4, "files_analyzed": 3},
        weaknesses=[{"title": "Candidate gap"}],
    )
    same, created_again = await reports.create(
        project_id=project_id,
        snapshot_hash="one",
        profile_version="v1",
        summary="A retry must not replace the durable report",
    )
    second, second_created = await reports.create(
        project_id=project_id,
        snapshot_hash="two",
        profile_version="v1",
        summary="Second baseline",
        security_candidates=[{"title": "Needs validation", "validated": False}],
    )
    stale_retry, stale_retry_created = await reports.create(
        project_id=project_id,
        snapshot_hash="one",
        profile_version="v1",
        summary="Retrying an old snapshot must not demote the current report",
    )

    assert (created, created_again, second_created, stale_retry_created) == (
        True,
        False,
        True,
        False,
    )
    assert same.id == first.id and same.summary == "First baseline"
    assert stale_retry.id == first.id
    history = await reports.list(project_id=project_id)
    assert [(r.id, r.status) for r in history] == [
        (second.id, "current"),
        (first.id, "stale"),
    ]
    assert (await reports.latest(project_id)) == second


async def test_store_rejects_invalid_identity_and_nonterminal_finish(tmp_path: Path) -> None:
    _db, jobs, reports, project_id = await _stores(tmp_path)
    with pytest.raises(ValueError):
        await jobs.enqueue(project_id=project_id, snapshot_hash="", profile_version="v1")
    with pytest.raises(ValueError):
        await reports.create(
            project_id=project_id,
            snapshot_hash="snap",
            profile_version="v1",
            summary="",
        )
    queued, _ = await jobs.enqueue(
        project_id=project_id, snapshot_hash="snap", profile_version="v1"
    )
    await jobs.claim(queued.id)
    with pytest.raises(ValueError):
        await jobs.finish(queued.id, AnalysisJobState.QUEUED)


async def test_corrupt_report_json_types_fail_closed(tmp_path: Path) -> None:
    db, _jobs, reports, project_id = await _stores(tmp_path)
    report, _ = await reports.create(
        project_id=project_id,
        snapshot_hash="typed",
        profile_version="v1",
        summary="Typed report",
        coverage={"files_total": 1},
        strengths=[{"title": "Good"}],
    )
    await db.execute(
        "UPDATE project_reports SET coverage_json='[]', strengths_json='{}', "
        "weaknesses_json='\"not-a-list\"' WHERE id=?",
        (report.id,),
    )
    await db.commit()
    loaded = await reports.get(report.id)
    assert loaded is not None
    assert loaded.coverage == {}
    assert loaded.strengths == []
    assert loaded.weaknesses == []


async def test_run_creation_and_analysis_job_attachment_are_atomic(tmp_path: Path) -> None:
    db, jobs, _reports, project_id = await _stores(tmp_path)
    orchestrations = OrchestrationStore(db, jobs.lock)
    queued, _ = await jobs.enqueue(
        project_id=project_id, snapshot_hash="atomic", profile_version="v1"
    )
    claimed = await jobs.claim(queued.id)
    assert claimed is not None

    async def attach(run_id: int) -> None:
        if not await jobs.attach_run_in_transaction(claimed, run_id):
            raise RuntimeError("lost analysis-job claim")

    run_id = await orchestrations.begin_run(
        project_id=project_id,
        workflow="project_assessment",
        title="atomic assessment",
        config={"team": "project_intelligence"},
        context_manifest=[],
        estimated_cost_usd=1.0,
        budget_usd=5.0,
        on_created_in_transaction=attach,
    )
    attached = await jobs.get(claimed.id)
    assert attached is not None and attached.orchestration_run_id == run_id
    assert await orchestrations.get(run_id) is not None

    second, _ = await jobs.enqueue(
        project_id=project_id, snapshot_hash="rollback", profile_version="v1"
    )
    second_claim = await jobs.claim(second.id)
    assert second_claim is not None
    stale_claim = replace(second_claim, attempts=second_claim.attempts + 1)

    async def refuse(run_id: int) -> None:
        if not await jobs.attach_run_in_transaction(stale_claim, run_id):
            raise RuntimeError("lost analysis-job claim")

    with pytest.raises(RuntimeError, match="lost analysis-job claim"):
        await orchestrations.begin_run(
            project_id=project_id,
            workflow="project_assessment",
            title="must roll back",
            config={"team": "project_intelligence"},
            context_manifest=[],
            estimated_cost_usd=1.0,
            budget_usd=5.0,
            on_created_in_transaction=refuse,
        )
    assert (await jobs.get(second.id)).orchestration_run_id is None  # type: ignore[union-attr]
    assert len(await orchestrations.list(project_id=project_id)) == 1
