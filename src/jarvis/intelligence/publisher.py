"""Atomic publication of snapshot-current project assessments and their attention item."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from jarvis.attention import AttentionKind, AttentionPriority, AttentionStore
from jarvis.graph import GraphStore
from jarvis.intelligence.report import build_report_draft
from jarvis.intelligence.store import (
    AnalysisJobState,
    AnalysisJobStore,
    ProjectReportStore,
)
from jarvis.knowledge.store import KnowledgeStore
from jarvis.orchestration.store import OrchestrationRun
from jarvis.persistence.db import transaction
from jarvis.projects import ProjectSnapshot, seal_snapshot


class LostAnalysisClaim(RuntimeError):
    """The publisher no longer owns the exact running job attempt."""


@dataclass(frozen=True)
class PublishOutcome:
    state: Literal["published", "discarded", "lost_claim"]
    report_id: int | None = None
    attention_id: int | None = None
    attention_created: bool = False
    fresh_snapshot: ProjectSnapshot | None = None


def _same_transaction(*stores: object) -> tuple[object, object]:
    db = getattr(stores[0], "db", None)
    lock = getattr(stores[0], "lock", None)
    if db is None or lock is None:
        raise ValueError("publisher stores require a shared database and lock")
    if any(getattr(store, "db", None) is not db for store in stores[1:]) or any(
        getattr(store, "lock", None) is not lock for store in stores[1:]
    ):
        raise ValueError("publisher stores must share one database connection and lock")
    return db, lock


async def publish_assessment(
    *,
    job_id: int,
    run: OrchestrationRun,
    knowledge: KnowledgeStore,
    graph: GraphStore,
    jobs: AnalysisJobStore,
    reports: ProjectReportStore,
    attention: AttentionStore,
    host_coverage: dict | None = None,
) -> PublishOutcome:
    """Publish report + attention + terminal job state in one SQLite transaction."""
    db, lock = _same_transaction(knowledge, graph, jobs, reports, attention)
    async with transaction(db, lock):  # type: ignore[arg-type]
        job = await jobs.get(job_id)
        if (
            job is None
            or job.state is not AnalysisJobState.RUNNING
            or job.orchestration_run_id != run.id
            or job.project_id != run.project_id
        ):
            return PublishOutcome("lost_claim")

        fresh = await seal_snapshot(knowledge, graph, job.project_id)
        if fresh.snapshot_hash != job.snapshot_hash:
            if not await jobs.transition_in_transaction(
                job,
                AnalysisJobState.DISCARDED,
                error="superseded by a newer project snapshot",
                coverage={**job.coverage, **fresh.coverage},
            ):
                raise LostAnalysisClaim("analysis job changed during stale publication")
            return PublishOutcome("discarded", fresh_snapshot=fresh)

        draft = build_report_draft(run, fresh, host_coverage=host_coverage)
        report, _created = await reports.create_draft_in_transaction(
            job=job, orchestration_run_id=run.id, draft=draft
        )
        if report.status != "current":
            report = await reports.activate_in_transaction(report)
        counts = {
            "strengths": len(draft.strengths),
            "weaknesses": len(draft.weaknesses),
            "security_candidates": len(draft.security_candidates),
            "frontend_backend_gaps": len(draft.fe_be_gaps),
            "test_reliability_gaps": len(draft.test_gaps),
            "recommendations": len(draft.recommendations),
        }
        attention_id, attention_created = await attention.create_if_new_in_transaction(
            kind=AttentionKind.REVIEW,
            source="project_intelligence",
            source_ref=str(report.id),
            project_id=job.project_id,
            priority=AttentionPriority.NORMAL,
            trust_class="model_generated",
            title="Project assessment ready",
            category="project_intelligence",
            payload={"report_id": report.id, "counts": counts},
            evidence=[],
            dedupe_key=f"project-intelligence-report:{report.id}",
        )
        if not await jobs.transition_in_transaction(
            job, AnalysisJobState.PUBLISHED, coverage=draft.coverage
        ):
            raise LostAnalysisClaim("analysis job changed during publication")
        return PublishOutcome(
            "published",
            report_id=report.id,
            attention_id=attention_id,
            attention_created=attention_created,
            fresh_snapshot=fresh,
        )
