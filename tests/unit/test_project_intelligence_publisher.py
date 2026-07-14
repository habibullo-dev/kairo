"""Atomic project report, job, and attention publication."""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path

import pytest

from jarvis.attention import AttentionStore
from jarvis.graph import GraphStore
from jarvis.intelligence import (
    AnalysisJobState,
    AnalysisJobStore,
    ProjectReportStore,
    publish_assessment,
)
from jarvis.knowledge.store import KnowledgeStore
from jarvis.orchestration import OrchestrationStore
from jarvis.persistence.db import connect
from jarvis.projects import ProjectStore, seal_snapshot

_OPEN: list = []


@pytest.fixture(autouse=True)
async def _close() -> None:
    yield
    while _OPEN:
        await _OPEN.pop().close()


async def _fixture(tmp_path: Path):
    db = await connect(tmp_path / "publisher.db")
    _OPEN.append(db)
    lock = asyncio.Lock()
    project_id = await ProjectStore(db, lock).create(name="Imported")
    knowledge = KnowledgeStore(db, lock)
    graph = GraphStore(db, lock)
    jobs = AnalysisJobStore(db, lock)
    reports = ProjectReportStore(db, lock)
    attention = AttentionStore(db, lock)
    orchestrations = OrchestrationStore(db, lock)
    return (
        db,
        project_id,
        knowledge,
        graph,
        jobs,
        reports,
        attention,
        orchestrations,
    )


async def _source(knowledge: KnowledgeStore, project_id: int, path: str, body: bytes) -> int:
    digest = hashlib.sha256(body).hexdigest()
    return await knowledge.add_source(
        kind="file",
        origin=f"chat-upload:{project_id}:{path}",
        title=path,
        content_hash=digest,
        raw_path=f"raw/{digest[:12]}",
        markdown_path=f"markdown/{digest[:12]}.md",
        markdown_hash=digest,
        converter="passthrough",
        converter_version="1",
        byte_size=len(body),
        mime="text/plain",
        review_status="reviewed",
        created_by="user",
        project_id=project_id,
    )


async def _completed_run(
    *,
    project_id: int,
    snapshot_hash: str,
    jobs: AnalysisJobStore,
    orchestrations: OrchestrationStore,
    evidence_ref: str,
):
    queued, _ = await jobs.enqueue(
        project_id=project_id,
        snapshot_hash=snapshot_hash,
        profile_version="project-intelligence-v1",
    )
    claimed = await jobs.claim(queued.id)
    assert claimed is not None

    async def attach(run_id: int) -> None:
        assert await jobs.attach_run_in_transaction(claimed, run_id)

    run_id = await orchestrations.begin_run(
        project_id=project_id,
        workflow="project_assessment",
        title="assessment",
        config={"team": "project_intelligence"},
        context_manifest=[],
        estimated_cost_usd=1.0,
        budget_usd=5.0,
        on_created_in_transaction=attach,
    )
    await orchestrations.complete_run(
        run_id,
        status="ok",
        verdict="accept",
        synthesis_summary="A grounded project baseline.",
        synthesis_findings=[
            {
                "finding_id": "finding-1111111111111111",
                "member": "architecture_backend",
                "title": "Architecture & Backend Analyst",
                "finding_title": "Clear project entry point",
                "finding": "The project has a visible entry point and bounded dependencies.",
                "category": "strength",
                "severity": "info",
                "confidence": "high",
                "evidence_ref": evidence_ref,
            }
        ],
        action_items=[
            {
                "title": "Keep the baseline current",
                "goal": "Re-run after material imports.",
                "priority": "low",
            }
        ],
    )
    run = await orchestrations.get(run_id)
    assert run is not None
    return claimed, run


async def test_atomic_publish_commits_job_report_and_attention(tmp_path: Path) -> None:
    (
        _db,
        project_id,
        knowledge,
        graph,
        jobs,
        reports,
        attention,
        orchestrations,
    ) = await _fixture(tmp_path)
    await _source(knowledge, project_id, "repo/app.py", b"app")
    snapshot = await seal_snapshot(knowledge, graph, project_id)
    claimed, run = await _completed_run(
        project_id=project_id,
        snapshot_hash=snapshot.snapshot_hash,
        jobs=jobs,
        orchestrations=orchestrations,
        evidence_ref="repo/app.py",
    )
    outcome = await publish_assessment(
        job_id=claimed.id,
        run=run,
        knowledge=knowledge,
        graph=graph,
        jobs=jobs,
        reports=reports,
        attention=attention,
        host_coverage={"context_chars": 800},
    )
    assert outcome.state == "published" and outcome.attention_created is True
    assert (await jobs.get(claimed.id)).state is AnalysisJobState.PUBLISHED  # type: ignore[union-attr]
    report = await reports.get(outcome.report_id)  # type: ignore[arg-type]
    item = await attention.get(outcome.attention_id)  # type: ignore[arg-type]
    assert report is not None and report.coverage["context_chars"] == 800
    assert item is not None and item.payload == {
        "report_id": report.id,
        "counts": {
            "strengths": 1,
            "weaknesses": 0,
            "security_candidates": 0,
            "frontend_backend_gaps": 0,
            "test_reliability_gaps": 0,
            "recommendations": 1,
        },
    }


async def test_stale_snapshot_discards_without_report_or_attention(tmp_path: Path) -> None:
    (
        _db,
        project_id,
        knowledge,
        graph,
        jobs,
        reports,
        attention,
        orchestrations,
    ) = await _fixture(tmp_path)
    await _source(knowledge, project_id, "repo/app.py", b"old")
    old = await seal_snapshot(knowledge, graph, project_id)
    claimed, run = await _completed_run(
        project_id=project_id,
        snapshot_hash=old.snapshot_hash,
        jobs=jobs,
        orchestrations=orchestrations,
        evidence_ref="repo/app.py",
    )
    await _source(knowledge, project_id, "repo/new.py", b"new")
    outcome = await publish_assessment(
        job_id=claimed.id,
        run=run,
        knowledge=knowledge,
        graph=graph,
        jobs=jobs,
        reports=reports,
        attention=attention,
    )
    assert outcome.state == "discarded"
    assert outcome.fresh_snapshot is not None
    assert (await jobs.get(claimed.id)).state is AnalysisJobState.DISCARDED  # type: ignore[union-attr]
    assert await reports.list(project_id=project_id) == []
    assert await attention.list(project_id=project_id) == []


async def test_attention_failure_rolls_back_report_and_job(tmp_path: Path) -> None:
    (
        _db,
        project_id,
        knowledge,
        graph,
        jobs,
        reports,
        attention,
        orchestrations,
    ) = await _fixture(tmp_path)
    await _source(knowledge, project_id, "repo/app.py", b"app")
    snapshot = await seal_snapshot(knowledge, graph, project_id)
    claimed, run = await _completed_run(
        project_id=project_id,
        snapshot_hash=snapshot.snapshot_hash,
        jobs=jobs,
        orchestrations=orchestrations,
        evidence_ref="repo/app.py",
    )

    async def fail(**_kwargs: object):
        raise RuntimeError("attention write failed")

    attention.create_if_new_in_transaction = fail  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="attention write failed"):
        await publish_assessment(
            job_id=claimed.id,
            run=run,
            knowledge=knowledge,
            graph=graph,
            jobs=jobs,
            reports=reports,
            attention=attention,
        )
    assert (await jobs.get(claimed.id)).state is AnalysisJobState.RUNNING  # type: ignore[union-attr]
    assert await reports.list(project_id=project_id) == []


async def test_lost_claim_writes_nothing(tmp_path: Path) -> None:
    (
        _db,
        project_id,
        knowledge,
        graph,
        jobs,
        reports,
        attention,
        orchestrations,
    ) = await _fixture(tmp_path)
    await _source(knowledge, project_id, "repo/app.py", b"app")
    snapshot = await seal_snapshot(knowledge, graph, project_id)
    claimed, run = await _completed_run(
        project_id=project_id,
        snapshot_hash=snapshot.snapshot_hash,
        jobs=jobs,
        orchestrations=orchestrations,
        evidence_ref="repo/app.py",
    )
    assert await jobs.finish(claimed.id, AnalysisJobState.FAILED, error="owner stopped")
    outcome = await publish_assessment(
        job_id=claimed.id,
        run=run,
        knowledge=knowledge,
        graph=graph,
        jobs=jobs,
        reports=reports,
        attention=attention,
    )
    assert outcome.state == "lost_claim"
    assert await reports.list(project_id=project_id) == []
    assert await attention.list(project_id=project_id) == []
