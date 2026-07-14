"""Durable automatic project-assessment coordination."""

from __future__ import annotations

import asyncio
import hashlib
import time
from collections.abc import Awaitable, Callable
from pathlib import Path

import pytest

from jarvis.attention import AttentionStore
from jarvis.config import BudgetsConfig, ProjectIntelligenceConfig
from jarvis.graph import GraphStore
from jarvis.intelligence import (
    PROFILE_VERSION,
    AnalysisJobState,
    AnalysisJobStore,
    ProjectIntelligenceCoordinator,
    ProjectReportStore,
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


async def _eventually(predicate, *, deadline_seconds: float = 3.0) -> None:
    deadline = asyncio.get_running_loop().time() + deadline_seconds
    while not predicate():
        if asyncio.get_running_loop().time() >= deadline:
            raise TimeoutError("condition was not reached")
        await asyncio.sleep(0.01)


class RecordingRouter:
    def __init__(self, jobs: AnalysisJobStore) -> None:
        self.jobs = jobs
        self.calls: list[dict] = []
        self.committed_when_called = False
        self.event = asyncio.Event()

    async def notify(self, **kwargs: object):
        published = await self.jobs.list(state=AnalysisJobState.PUBLISHED)
        self.committed_when_called = bool(published)
        self.calls.append(dict(kwargs))
        self.event.set()
        return None


class FakeAssessmentRunner:
    def __init__(
        self, orchestration: OrchestrationStore, outcomes: list[str] | None = None
    ) -> None:
        self.orchestration = orchestration
        self.outcomes = list(outcomes or ["ok"])
        self.calls: list[dict] = []
        self.active = 0
        self.max_active = 0
        self.before_complete: Callable[[int], Awaitable[None]] | None = None
        self.entered = asyncio.Event()
        self.release: asyncio.Event | None = None

    async def run_automatic_project_assessment(
        self,
        *,
        project_id: int,
        context,
        budget_usd: float,
        on_event=None,
        on_created_in_transaction=None,
    ) -> int:
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        self.calls.append({"project_id": project_id, "context": context, "budget": budget_usd})
        try:
            run_id = await self.orchestration.begin_run(
                project_id=project_id,
                workflow="project_assessment",
                title="Project Intelligence · Project health assessment",
                config={"team": "project_intelligence"},
                context_manifest=context.manifest(),
                estimated_cost_usd=budget_usd,
                budget_usd=budget_usd,
                on_created_in_transaction=on_created_in_transaction,
            )
            if on_event is not None:
                await on_event({"kind": "orchestration_started", "run_id": run_id})
            self.entered.set()
            if self.release is not None:
                await self.release.wait()
            if self.before_complete is not None:
                await self.before_complete(len(self.calls))
            outcome = self.outcomes.pop(0) if self.outcomes else "ok"
            if outcome == "ok":
                await self.orchestration.complete_run(
                    run_id,
                    status="ok",
                    verdict="accept",
                    synthesis_summary="A grounded project baseline.",
                    synthesis_findings=[
                        {
                            "finding_id": f"finding-{run_id:016x}",
                            "member": "architecture_backend",
                            "title": "Architecture & Backend Analyst",
                            "finding_title": "Visible project entry point",
                            "finding": "The project has a bounded, visible source entry point.",
                            "category": "strength",
                            "severity": "info",
                            "confidence": "high",
                            "evidence_ref": "repo/app.py",
                        }
                    ],
                    action_items=[
                        {
                            "title": "Keep the assessment current",
                            "goal": "Re-run after material imports.",
                            "priority": "low",
                        }
                    ],
                )
            else:
                await self.orchestration.complete_run(
                    run_id,
                    status=outcome,
                    verdict=None,
                    synthesis_summary="provider detail must not enter job state",
                )
            return run_id
        finally:
            self.active -= 1


async def _fixture(
    tmp_path: Path,
    *,
    policy: ProjectIntelligenceConfig | None = None,
    budgets: BudgetsConfig | None = None,
    outcomes: list[str] | None = None,
):
    db = await connect(tmp_path / "coordinator.db")
    _OPEN.append(db)
    lock = asyncio.Lock()
    project_id = await ProjectStore(db, lock).create(name="Imported")
    knowledge = KnowledgeStore(db, lock)
    graph = GraphStore(db, lock)
    jobs = AnalysisJobStore(db, lock)
    reports = ProjectReportStore(db, lock)
    attention = AttentionStore(db, lock)
    orchestration = OrchestrationStore(db, lock)
    runner = FakeAssessmentRunner(orchestration, outcomes)
    router = RecordingRouter(jobs)
    coordinator = ProjectIntelligenceCoordinator(
        policy=policy or ProjectIntelligenceConfig(enabled=True),
        budgets=budgets or BudgetsConfig(),
        knowledge=knowledge,
        graph=graph,
        jobs=jobs,
        reports=reports,
        attention=attention,
        orchestration=orchestration,
        runner=runner,
        notification_router=router,
        retry_delay_seconds=0.01,
    )
    return (
        project_id,
        knowledge,
        jobs,
        reports,
        attention,
        orchestration,
        runner,
        router,
        coordinator,
    )


async def test_policy_gate_and_snapshot_enqueue_are_idempotent(tmp_path: Path) -> None:
    fixture = await _fixture(
        tmp_path,
        policy=ProjectIntelligenceConfig(enabled=False, analyze_after_import=True),
    )
    project_id, knowledge, jobs, *_rest, coordinator = fixture
    await _source(knowledge, project_id, "repo/app.py", b"app")
    disabled = await coordinator.enqueue_project(project_id)
    assert disabled.enabled is False
    assert await jobs.list() == []

    coordinator.policy = ProjectIntelligenceConfig(enabled=True, analyze_after_import=True)
    first = await coordinator.enqueue_project(project_id)
    duplicate = await coordinator.enqueue_project(project_id)
    assert first.created is True and duplicate.created is False
    assert first.job_id == duplicate.job_id
    job = await jobs.get(first.job_id)  # type: ignore[arg-type]
    assert job is not None and job.profile_version == PROFILE_VERSION


async def test_worker_uses_graph_first_private_context_hard_cap_and_post_commit_notify(
    tmp_path: Path,
) -> None:
    fixture = await _fixture(
        tmp_path,
        policy=ProjectIntelligenceConfig(enabled=True, max_cost_usd=8.0),
        budgets=BudgetsConfig(hard_stop_usd_per_run=2.0),
    )
    project_id, knowledge, jobs, reports, _attention, _orch, runner, router, coordinator = fixture
    await _source(knowledge, project_id, "repo/app.py", b"app")
    queued = await coordinator.enqueue_project(project_id)
    await coordinator.start()
    await asyncio.wait_for(router.event.wait(), timeout=3)
    await coordinator.stop()

    assert len(runner.calls) == 1 and runner.max_active == 1
    call = runner.calls[0]
    assert call["budget"] == 2.0
    context = call["context"]
    assert context.items[1].provenance.value == "repo_code"
    assert context.items[1].ref.startswith("snapshot:")
    assert "repo/app.py" in context.items[1].text
    job = await jobs.get(queued.job_id)  # type: ignore[arg-type]
    assert job is not None and job.state is AnalysisJobState.PUBLISHED
    assert await reports.latest(project_id) is not None
    assert router.committed_when_called is True and len(router.calls) == 1


async def test_stale_completed_result_discards_and_queues_fresh_snapshot(tmp_path: Path) -> None:
    fixture = await _fixture(tmp_path, outcomes=["ok", "ok"])
    project_id, knowledge, jobs, _reports, _attention, _orch, runner, router, coordinator = fixture
    await _source(knowledge, project_id, "repo/app.py", b"app")

    async def mutate_once(call_number: int) -> None:
        if call_number == 1:
            await _source(knowledge, project_id, "repo/new.py", b"new")

    runner.before_complete = mutate_once
    await coordinator.enqueue_project(project_id)
    await coordinator.start()
    await asyncio.wait_for(router.event.wait(), timeout=3)
    await coordinator.stop()

    history = await jobs.list(project_id=project_id)
    assert [job.state for job in history] == [
        AnalysisJobState.DISCARDED,
        AnalysisJobState.PUBLISHED,
    ]
    assert len(runner.calls) == 2 and runner.max_active == 1


async def test_retry_is_bounded_and_provider_detail_is_not_persisted(tmp_path: Path) -> None:
    fixture = await _fixture(
        tmp_path,
        policy=ProjectIntelligenceConfig(enabled=True, max_attempts=2),
        outcomes=["error", "ok"],
    )
    project_id, knowledge, jobs, _reports, _attention, _orch, runner, router, coordinator = fixture
    await _source(knowledge, project_id, "repo/app.py", b"app")
    queued = await coordinator.enqueue_project(project_id)
    started = time.monotonic()
    await coordinator.start()
    await asyncio.wait_for(router.event.wait(), timeout=3)
    await coordinator.stop()

    job = await jobs.get(queued.job_id)  # type: ignore[arg-type]
    assert job is not None and job.state is AnalysisJobState.PUBLISHED
    assert job.attempts == 2 and job.last_error is None
    assert len(runner.calls) == 2
    assert time.monotonic() - started >= coordinator.retry_delay_seconds


@pytest.mark.parametrize("status", ["budget_stopped", "rejected", "revise"])
async def test_non_retryable_run_outcomes_fail_without_second_model_call(
    tmp_path: Path,
    status: str,
) -> None:
    fixture = await _fixture(tmp_path, outcomes=[status])
    project_id, knowledge, jobs, _reports, attention, _orch, runner, _router, coordinator = (
        fixture
    )
    await _source(knowledge, project_id, "repo/app.py", b"app")
    queued = await coordinator.enqueue_project(project_id)
    await coordinator.start()

    deadline = asyncio.get_running_loop().time() + 3.0
    while True:
        job = await jobs.get(queued.job_id)  # type: ignore[arg-type]
        if job is not None and job.state is AnalysisJobState.FAILED:
            break
        if asyncio.get_running_loop().time() >= deadline:
            raise TimeoutError("analysis job did not reach failed state")
        await asyncio.sleep(0.01)
    await coordinator.stop()
    job = await jobs.get(queued.job_id)  # type: ignore[arg-type]
    assert job is not None and job.attempts == 1
    assert len(runner.calls) == 1
    assert await attention.list(project_id=project_id) == []


async def test_startup_reconcile_publishes_completed_run_without_new_model_call(
    tmp_path: Path,
) -> None:
    fixture = await _fixture(tmp_path)
    (
        project_id,
        knowledge,
        jobs,
        _reports,
        _attention,
        orchestration,
        runner,
        router,
        coordinator,
    ) = fixture
    await _source(knowledge, project_id, "repo/app.py", b"app")
    snapshot = await seal_snapshot(knowledge, coordinator.graph, project_id)
    queued, _ = await jobs.enqueue(
        project_id=project_id,
        snapshot_hash=snapshot.snapshot_hash,
        profile_version=PROFILE_VERSION,
        graph_watermark=snapshot.graph_watermark,
        coverage=snapshot.coverage,
    )
    claimed = await jobs.claim(queued.id)
    assert claimed is not None

    async def attach(run_id: int) -> None:
        assert await jobs.attach_run_in_transaction(claimed, run_id)

    run_id = await orchestration.begin_run(
        project_id=project_id,
        workflow="project_assessment",
        title="assessment",
        config={"team": "project_intelligence"},
        context_manifest=[],
        estimated_cost_usd=1.0,
        budget_usd=2.0,
        on_created_in_transaction=attach,
    )
    await orchestration.complete_run(
        run_id,
        status="ok",
        verdict="accept",
        synthesis_summary="A grounded baseline.",
        synthesis_findings=[
            {
                "finding_id": "finding-1111111111111111",
                "member": "architecture_backend",
                "finding_title": "Visible entry point",
                "finding": "The source entry point is visible.",
                "category": "strength",
                "severity": "info",
                "confidence": "high",
                "evidence_ref": "repo/app.py",
            }
        ],
    )
    await coordinator.start()
    await asyncio.wait_for(router.event.wait(), timeout=3)
    await coordinator.stop()
    assert runner.calls == []
    assert (await jobs.get(queued.id)).state is AnalysisJobState.PUBLISHED  # type: ignore[union-attr]


async def test_publication_failure_keeps_accepted_run_for_reconciliation(
    tmp_path: Path,
) -> None:
    fixture = await _fixture(tmp_path)
    project_id, knowledge, jobs, _reports, attention, _orch, runner, router, coordinator = fixture
    await _source(knowledge, project_id, "repo/app.py", b"app")
    queued = await coordinator.enqueue_project(project_id)
    original = attention.create_if_new_in_transaction

    async def fail_once(**_kwargs: object):
        raise RuntimeError("transient attention failure")

    attention.create_if_new_in_transaction = fail_once  # type: ignore[method-assign]
    await coordinator.start()

    def accepted_is_retained() -> bool:
        return bool(runner.calls) and runner.active == 0

    await _eventually(accepted_is_retained)
    await coordinator.stop()
    retained = await jobs.get(queued.job_id)  # type: ignore[arg-type]
    assert retained is not None
    assert retained.state is AnalysisJobState.RUNNING
    assert retained.orchestration_run_id is not None

    attention.create_if_new_in_transaction = original  # type: ignore[method-assign]
    await coordinator.reconcile()
    await asyncio.wait_for(router.event.wait(), timeout=3)
    assert len(runner.calls) == 1
    assert (await jobs.get(retained.id)).state is AnalysisJobState.PUBLISHED  # type: ignore[union-attr]


async def test_host_sweeps_orphan_before_start_reconciles_and_retries_once(
    tmp_path: Path,
) -> None:
    fixture = await _fixture(tmp_path)
    (
        project_id,
        knowledge,
        jobs,
        _reports,
        _attention,
        orchestration,
        runner,
        router,
        coordinator,
    ) = fixture
    await _source(knowledge, project_id, "repo/app.py", b"app")
    snapshot = await seal_snapshot(knowledge, coordinator.graph, project_id)
    queued, _ = await jobs.enqueue(
        project_id=project_id,
        snapshot_hash=snapshot.snapshot_hash,
        profile_version=PROFILE_VERSION,
    )
    claimed = await jobs.claim(queued.id)
    assert claimed is not None

    async def attach(run_id: int) -> None:
        assert await jobs.attach_run_in_transaction(claimed, run_id)

    orphan_id = await orchestration.begin_run(
        project_id=project_id,
        workflow="project_assessment",
        title="interrupted assessment",
        config={"team": "project_intelligence"},
        context_manifest=[],
        estimated_cost_usd=1.0,
        budget_usd=2.0,
        on_created_in_transaction=attach,
    )
    await orchestration.sweep_orphans()
    await coordinator.start()
    await asyncio.wait_for(router.event.wait(), timeout=3)
    await coordinator.stop()
    orphan = await orchestration.get(orphan_id)
    final = await jobs.get(queued.id)
    assert orphan is not None and orphan.status == "aborted"
    assert final is not None and final.state is AnalysisJobState.PUBLISHED
    assert final.attempts == 2 and len(runner.calls) == 1


async def test_stop_drains_current_read_only_assessment(tmp_path: Path) -> None:
    fixture = await _fixture(tmp_path)
    project_id, knowledge, jobs, _reports, _attention, _orch, runner, _router, coordinator = fixture
    await _source(knowledge, project_id, "repo/app.py", b"app")
    runner.release = asyncio.Event()
    await coordinator.enqueue_project(project_id)
    await coordinator.start()
    await asyncio.wait_for(runner.entered.wait(), timeout=3)
    await _source(knowledge, project_id, "repo/queued.py", b"queued")
    queued = await coordinator.enqueue_project(project_id)
    stopping = asyncio.create_task(coordinator.stop())
    await asyncio.sleep(0)
    assert not stopping.done()
    runner.release.set()
    await asyncio.wait_for(stopping, timeout=3)
    assert not coordinator.running and len(runner.calls) == 1
    pending = await jobs.get(queued.job_id)  # type: ignore[arg-type]
    assert pending is not None and pending.state is AnalysisJobState.QUEUED
