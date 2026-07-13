"""Remote Operator proposals and approval capabilities are durable and replay-proof."""

from __future__ import annotations

import asyncio
import datetime as dt
from pathlib import Path

from jarvis.config import TelegramRemoteOperatorConfig
from jarvis.persistence.db import connect
from jarvis.persistence.sessions import SessionStore
from jarvis.projects.store import ProjectStore
from jarvis.remote.operator import (
    RemoteOperatorStore,
    RemoteProposalParams,
    RemoteProposalTool,
    render_tool_approval,
)
from jarvis.scheduler.store import ParkedContinuation, TaskStore


async def _stores(tmp_path: Path):
    db = await connect(tmp_path / "operator.db")
    lock = asyncio.Lock()
    return db, RemoteOperatorStore(db, lock), ProjectStore(db, lock), TaskStore(db, lock)


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
