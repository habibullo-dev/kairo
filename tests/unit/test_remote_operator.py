"""Remote Operator proposals and approval capabilities are durable and replay-proof."""

from __future__ import annotations

import asyncio
import datetime as dt
from pathlib import Path

from pydantic import BaseModel

from jarvis.config import SchedulerConfig, TelegramRemoteOperatorConfig
from jarvis.persistence.db import connect
from jarvis.persistence.sessions import SessionStore
from jarvis.projects.store import ProjectStore
from jarvis.remote.operator import (
    RemoteLiveSearchParams,
    RemoteLiveSearchTool,
    RemoteOperatorService,
    RemoteOperatorStore,
    RemoteProposalParams,
    RemoteProposalTool,
    render_tool_approval,
)
from jarvis.scheduler.service import TaskService
from jarvis.scheduler.store import ParkedContinuation, TaskStore
from jarvis.tools.base import Tool


async def _stores(tmp_path: Path):
    db = await connect(tmp_path / "operator.db")
    lock = asyncio.Lock()
    return db, RemoteOperatorStore(db, lock), ProjectStore(db, lock), TaskStore(db, lock)


class _Runner:
    def __init__(self) -> None:
        self.kicks = 0
        self.resumed: list[tuple[int, str]] = []
        self.resumed_event = asyncio.Event()

    def kick(self) -> None:
        self.kicks += 1

    async def resume_parked(self, run_id: int, action: str) -> bool:
        self.resumed.append((run_id, action))
        self.resumed_event.set()
        return True


class _SearchParams(BaseModel):
    query: str
    max_results: int


class _SearchSource(Tool):
    name = "web_search"
    description = "test search source"
    Params = _SearchParams

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[_SearchParams] = []

    async def run(self, params: _SearchParams) -> str:
        self.calls.append(params)
        return "Current public weather: sunny, 28 C."


def _service(
    store: RemoteOperatorStore,
    projects: ProjectStore | None,
    tasks: TaskStore,
    runner: _Runner,
    *,
    sender=None,
) -> RemoteOperatorService:
    return RemoteOperatorService(
        store=store,
        config=TelegramRemoteOperatorConfig(enabled=True),
        tasks=TaskService(tasks, SchedulerConfig()),
        projects=projects,
        runner=runner,  # type: ignore[arg-type]
        sender=sender,
    )


async def test_operator_model_and_project_copy_uses_canonical_kira_branding(
    tmp_path: Path,
) -> None:
    schema = RemoteProposalParams.model_json_schema()
    assert "existing Kira project" in schema["properties"]["project"]["description"]
    assert "owner-requested Kira job" in RemoteProposalTool.description
    assert "Kairo" not in RemoteProposalTool.description

    db, store, projects, tasks = await _stores(tmp_path)
    runner = _Runner()
    unavailable = _service(store, None, tasks, runner)
    empty = _service(store, projects, tasks, runner)
    try:
        assert await unavailable.projects_text() == (
            "Projects are unavailable on this Kira instance."
        )
        assert await empty.projects_text() == (
            "No active Kira projects. Create and link a project on the local workstation."
        )

        tool = RemoteProposalTool(
            store=store,
            projects=None,
            config=TelegramRemoteOperatorConfig(enabled=True),
        )
        tool.begin_turn()
        result = await tool.run(
            RemoteProposalParams(
                kind="job",
                title="Inspect project",
                instruction="Inspect the configured project.",
                project="missing",
            )
        )
        assert getattr(result, "is_error", False)
        assert "Use Kira locally" in str(result)
    finally:
        await unavailable.stop()
        await empty.stop()
        await db.close()


async def test_proposal_code_is_hashed_single_use_and_bound_to_immutable_proposal(
    tmp_path: Path,
) -> None:
    db, store, projects, _tasks = await _stores(tmp_path)
    try:
        project_id = await projects.create(name="Frontend", repos=[str(tmp_path / "frontend")])
        tool = RemoteProposalTool(
            store=store,
            projects=projects,
            config=TelegramRemoteOperatorConfig(enabled=True),
        )
        tool.begin_turn()
        result = await tool.run(
            RemoteProposalParams(
                kind="job",
                title="Repair the dashboard",
                instruction="Inspect the dashboard and repair its broken API wiring.",
                project="frontend",
                status_interval_minutes=5,
            )
        )
        assert "awaiting owner approval" in str(result)
        authorization = tool.drain_created()[0]
        assert authorization.proposal.project_id == project_id

        raw = " ".join(
            str(value)
            for row in await (await db.execute("SELECT * FROM remote_operator_tokens")).fetchall()
            for value in row
        )
        assert authorization.approval_code not in raw

        grant = await store.consume_token(authorization.approval_code, resolution="approve")
        assert grant is not None and grant.subject_type == "proposal"
        assert await store.consume_token(authorization.approval_code, resolution="approve") is None
        assert (await store.get(authorization.proposal.id)).state == "approved"  # type: ignore[union-attr]
    finally:
        await db.close()


async def test_only_one_proposal_can_be_created_per_model_turn(tmp_path: Path) -> None:
    db, store, projects, _tasks = await _stores(tmp_path)
    try:
        tool = RemoteProposalTool(
            store=store,
            projects=projects,
            config=TelegramRemoteOperatorConfig(enabled=True),
        )
        tool.begin_turn()
        params = RemoteProposalParams(
            kind="reminder",
            title="Check deployment",
            instruction="Check the deployment.",
        )
        first, second = await asyncio.gather(tool.run(params), tool.run(params))
        results = [str(first), str(second)]
        assert sum("awaiting owner approval" in result for result in results) == 1
        assert sum("Only one remote proposal" in result for result in results) == 1
        assert len(tool.drain_created()) == 1
    finally:
        await db.close()


async def test_live_search_is_bounded_to_one_fixed_size_public_query_per_turn() -> None:
    source = _SearchSource()
    tool = RemoteLiveSearchTool(source=source, max_results=3)
    tool.begin_turn()

    first = await tool.run(RemoteLiveSearchParams(query="  weather   Seoul today  "))
    second = await tool.run(RemoteLiveSearchParams(query="weather Busan today"))

    assert first == "Current public weather: sunny, 28 C."
    assert len(source.calls) == 1
    assert source.calls[0].query == "weather Seoul today"
    assert source.calls[0].max_results == 3
    assert "Only one live public search" in str(second)


async def test_reissuing_proposal_code_invalidates_the_previous_code(tmp_path: Path) -> None:
    db, store, _projects, _tasks = await _stores(tmp_path)
    try:
        original = await store.create_proposal(
            kind="job",
            title="Audit backend",
            instruction="Audit the backend.",
            project_id=None,
            schedule_kind="immediate",
            schedule_spec="",
            status_interval_minutes=15,
            proposal_ttl_minutes=30,
            approval_ttl_minutes=15,
        )
        replacement = await store.issue_proposal_token(original.proposal.id, ttl_minutes=15)
        assert replacement is not None and replacement.approval_code != original.approval_code
        assert await store.consume_token(original.approval_code, resolution="approve") is None
        assert await store.consume_token(replacement.approval_code, resolution="deny") is not None
        assert (await store.get(original.proposal.id)).state == "denied"  # type: ignore[union-attr]
    finally:
        await db.close()


async def test_expired_proposal_never_authorizes_work(tmp_path: Path) -> None:
    db, store, _projects, _tasks = await _stores(tmp_path)
    try:
        start = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)
        authorization = await store.create_proposal(
            kind="job",
            title="Old work request",
            instruction="Do not execute after expiry.",
            project_id=None,
            schedule_kind="immediate",
            schedule_spec="",
            status_interval_minutes=15,
            proposal_ttl_minutes=5,
            approval_ttl_minutes=5,
            now=start,
        )
        grant = await store.consume_token(
            authorization.approval_code,
            resolution="approve",
            now=start + dt.timedelta(minutes=6),
        )
        assert grant is None
        await store.expire_pending(now=start + dt.timedelta(minutes=6))
        assert (await store.get(authorization.proposal.id)).state == "expired"  # type: ignore[union-attr]
    finally:
        await db.close()


async def test_parked_tool_code_is_bound_to_exact_call_and_preview_redacts_file_content(
    tmp_path: Path,
) -> None:
    db, store, _projects, tasks = await _stores(tmp_path)
    try:
        proposal = await store.create_proposal(
            kind="job",
            title="Edit app",
            instruction="Edit the app.",
            project_id=None,
            schedule_kind="immediate",
            schedule_spec="",
            status_interval_minutes=15,
            proposal_ttl_minutes=30,
            approval_ttl_minutes=15,
        )
        assert await store.consume_token(proposal.approval_code, resolution="approve") is not None
        task_id = await tasks.add(
            kind="job",
            title="Edit app",
            payload="Edit the app.",
            schedule_kind="once",
            schedule_spec="2026-01-01T00:00:00",
            timezone="UTC",
            next_run_at="2026-01-01T00:00:00+00:00",
            created_by="user",
            origin="remote_operator",
        )
        assert await store.mark_queued(proposal.proposal.id, task_id)
        run_id = await tasks.start_run(task_id, "2026-01-01T00:00:00+00:00")
        session_id = await SessionStore(db, tasks.lock).create_session(kind="task")
        canary = "PRIVATE-CONTENT-CANARY"
        continuation = ParkedContinuation.from_call(
            tool_id="tool-1",
            tool_name="write_file",
            tool_input={"path": "app.py", "content": canary},
            decision_reason="write requires approval",
        )
        assert await tasks.park_run(run_id, session_id=session_id, continuation=continuation)
        issued = await store.issue_parked_token(run_id, ttl_minutes=15)
        assert issued is not None
        pending, code = issued
        preview = render_tool_approval(pending, code)
        assert "write_file" in preview and "app.py" in preview
        assert canary not in preview

        grant = await store.consume_token(code, resolution="approve")
        assert grant is not None and grant.binding_hash == continuation.tool_input_hash
        assert await store.consume_token(code, resolution="approve") is None
    finally:
        await db.close()


async def test_service_approval_queues_remote_origin_without_local_session_provenance(
    tmp_path: Path,
) -> None:
    db, store, projects, tasks = await _stores(tmp_path)
    runner = _Runner()
    service = _service(store, projects, tasks, runner)
    try:
        project_id = await projects.create(name="Kira", repos=[str(tmp_path)])
        authorization = await store.create_proposal(
            kind="job",
            title="Repair frontend wiring",
            instruction="Inspect the API wiring and repair the broken frontend flow.",
            project_id=project_id,
            schedule_kind="immediate",
            schedule_spec="",
            status_interval_minutes=15,
            proposal_ttl_minutes=30,
            approval_ttl_minutes=15,
        )
        service.tasks.bound_session_id = 999

        reply = await service.resolve(authorization.approval_code, resolution="approve")

        queued = await store.get(authorization.proposal.id)
        assert queued is not None and queued.state == "queued" and queued.task_id is not None
        task = await tasks.get(queued.task_id)
        assert task is not None
        assert task.origin == "remote_operator"
        assert task.project_id == project_id
        assert task.source_session_id is None
        assert task.status == "active"
        assert runner.kicks == 1
        assert "Approved and queued" in reply
        assert "Kira will send milestones" in reply and "Kairo" not in reply

        cancelled = await service.cancel(str(queued.id))
        assert "Cancelled remote job" in cancelled
        assert (await store.get(queued.id)).state == "cancelled"  # type: ignore[union-attr]
        assert (await tasks.get(task.id)).status == "cancelled"  # type: ignore[union-attr]
    finally:
        await service.stop()
        await db.close()


async def test_service_bind_failure_uses_canonical_kira_branding(
    tmp_path: Path,
    monkeypatch,
) -> None:
    db, store, projects, tasks = await _stores(tmp_path)
    runner = _Runner()
    service = _service(store, projects, tasks, runner)

    async def reject_binding(_proposal_id: int, _task_id: int) -> bool:
        return False

    monkeypatch.setattr(store, "mark_queued", reject_binding)
    try:
        authorization = await store.create_proposal(
            kind="job",
            title="Reject unsafe binding",
            instruction="Do not run without a durable proposal binding.",
            project_id=None,
            schedule_kind="immediate",
            schedule_spec="",
            status_interval_minutes=0,
            proposal_ttl_minutes=30,
            approval_ttl_minutes=15,
        )
        reply = await service.resolve(authorization.approval_code, resolution="approve")
        assert reply == "Kira could not bind the approved proposal safely; the task was cancelled."
        failed = await store.get(authorization.proposal.id)
        assert failed is not None and failed.state == "failed" and failed.task_id is None
        assert runner.kicks == 0
    finally:
        await service.stop()
        await db.close()


async def test_service_denial_never_creates_a_scheduler_task(tmp_path: Path) -> None:
    db, store, projects, tasks = await _stores(tmp_path)
    runner = _Runner()
    service = _service(store, projects, tasks, runner)
    try:
        authorization = await store.create_proposal(
            kind="job",
            title="Do not run",
            instruction="This proposal must remain inert.",
            project_id=None,
            schedule_kind="immediate",
            schedule_spec="",
            status_interval_minutes=0,
            proposal_ttl_minutes=30,
            approval_ttl_minutes=15,
        )

        reply = await service.resolve(authorization.approval_code, resolution="deny")

        assert "Nothing was scheduled or run" in reply
        assert (await store.get(authorization.proposal.id)).state == "denied"  # type: ignore[union-attr]
        assert await tasks.list() == []
        assert runner.kicks == 0
    finally:
        await service.stop()
        await db.close()


async def test_service_start_closes_interrupted_approval_and_cancels_unbound_task(
    tmp_path: Path,
) -> None:
    db, store, projects, tasks = await _stores(tmp_path)
    runner = _Runner()
    sent: list[str] = []

    async def sender(text: str) -> None:
        sent.append(text)

    service = _service(store, projects, tasks, runner, sender=sender)
    try:
        authorization = await store.create_proposal(
            kind="job",
            title="Interrupted approval",
            instruction="This work must not survive a partial queue transaction.",
            project_id=None,
            schedule_kind="immediate",
            schedule_spec="",
            status_interval_minutes=0,
            proposal_ttl_minutes=30,
            approval_ttl_minutes=15,
        )
        assert await store.consume_token(
            authorization.approval_code, resolution="approve"
        ) is not None
        orphan_task_id = await tasks.add(
            kind="job",
            title="Interrupted approval",
            payload="This work must not survive a partial queue transaction.",
            schedule_kind="once",
            schedule_spec="2099-01-01T00:00:00",
            timezone="UTC",
            next_run_at="2099-01-01T00:00:00+00:00",
            created_by="user",
            origin="remote_operator",
        )

        await service.start()

        proposal = await store.get(authorization.proposal.id)
        assert proposal is not None and proposal.state == "failed" and proposal.task_id is None
        assert proposal.error == (
            "Kira restarted before the approved proposal was durably bound to a task"
        )
        assert (await tasks.get(orphan_task_id)).status == "cancelled"  # type: ignore[union-attr]
        assert await store.active_count() == 0
        assert runner.kicks == 1
        assert sent == [
            f"Remote proposal #{proposal.id} was interrupted during queueing and was closed "
            "without running work. Please send the request again."
        ]
    finally:
        await service.stop()
        await db.close()


async def test_service_parked_approval_resumes_only_the_saved_run(tmp_path: Path) -> None:
    db, store, projects, tasks = await _stores(tmp_path)
    runner = _Runner()
    sent: list[str] = []

    async def sender(text: str) -> None:
        sent.append(text)

    service = _service(store, projects, tasks, runner, sender=sender)
    try:
        authorization = await store.create_proposal(
            kind="job",
            title="Write approved file",
            instruction="Write only after exact approval.",
            project_id=None,
            schedule_kind="immediate",
            schedule_spec="",
            status_interval_minutes=0,
            proposal_ttl_minutes=30,
            approval_ttl_minutes=15,
        )
        assert await store.consume_token(
            authorization.approval_code, resolution="approve"
        ) is not None
        task_id = await tasks.add(
            kind="job",
            title="Write approved file",
            payload="Write only after exact approval.",
            schedule_kind="once",
            schedule_spec="2026-01-01T00:00:00",
            timezone="UTC",
            next_run_at="2026-01-01T00:00:00+00:00",
            created_by="user",
            origin="remote_operator",
        )
        assert await store.mark_queued(authorization.proposal.id, task_id)
        run_id = await tasks.start_run(task_id, "2026-01-01T00:00:00+00:00")
        session_id = await SessionStore(db, tasks.lock).create_session(kind="task")
        continuation = ParkedContinuation.from_call(
            tool_id="tool-exact",
            tool_name="run_shell",
            tool_input={"command": "uv run pytest -q", "cwd": str(tmp_path)},
            decision_reason="shell requires approval",
        )
        assert await tasks.park_run(
            run_id, session_id=session_id, continuation=continuation
        )
        issued = await store.issue_parked_token(run_id, ttl_minutes=15)
        assert issued is not None
        _pending, code = issued

        reply = await service.resolve(code, resolution="approve")
        await asyncio.wait_for(runner.resumed_event.wait(), timeout=1)

        assert "approved" in reply
        assert "Kira is processing the saved continuation." in reply
        assert runner.resumed == [(run_id, "approve")]
        assert sent == []
    finally:
        await service.stop()
        await db.close()
