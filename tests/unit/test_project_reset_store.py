"""Project reset archives history and creates one clean successor atomically."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from jarvis.actions.intents import IntentKind, IntentState, IntentStore
from jarvis.attention.store import AttentionStore
from jarvis.graph.store import GraphStore
from jarvis.intelligence.store import AnalysisJobStore
from jarvis.persistence.db import connect
from jarvis.persistence.sessions import SessionStore
from jarvis.projects import ProjectResetBusyError, ProjectResetError, ProjectStore
from jarvis.scheduler.store import TaskStore


async def _stores(tmp_path: Path) -> tuple[ProjectStore, SessionStore]:
    db = await connect(tmp_path / "reset.db")
    lock = asyncio.Lock()
    projects = ProjectStore(db, lock)
    return projects, SessionStore(db, lock)


@pytest.mark.asyncio
@pytest.mark.parametrize("retain_repositories", [True, False])
async def test_reset_archives_history_and_creates_clean_successor(
    tmp_path: Path, retain_repositories: bool
) -> None:
    projects, sessions = await _stores(tmp_path)
    try:
        predecessor_id = await projects.create(
            name="Kira",
            description="Agent workstation",
            color="#123456",
            icon="K",
            repos=["C:/src/kira"],
            settings={"label": "Coding", "services": ["filesystem"], "private": "drop-me"},
        )
        await projects.set_pinned(predecessor_id, True)
        old_session_id = await sessions.create_session(project_id=predecessor_id)

        result = await projects.reset(
            predecessor_id, retain_repositories=retain_repositories
        )

        assert result is not None and result.predecessor_id == predecessor_id
        predecessor = await projects.get(predecessor_id)
        successor = await projects.get(result.successor_id)
        assert predecessor is not None and predecessor.status == "archived"
        assert predecessor.archived_at == result.created_at
        assert successor is not None and successor.status == "active"
        assert successor.slug == "kira-2"
        assert (successor.name, successor.description, successor.color, successor.icon) == (
            "Kira",
            "Agent workstation",
            "#123456",
            "K",
        )
        assert successor.pinned is True
        assert successor.settings == {"label": "Coding", "services": ["filesystem"]}
        assert successor.repos == (("C:/src/kira",) if retain_repositories else ())
        assert (await sessions.get_meta(old_session_id)).project_id == predecessor_id
        assert await sessions.list_sessions(project_id=result.successor_id) == []
        event = await (
            await projects.db.execute(
                "SELECT predecessor_project_id, successor_project_id, "
                "retained_repositories, created_at FROM project_reset_events"
            )
        ).fetchone()
        assert event == (
            predecessor_id,
            result.successor_id,
            1 if retain_repositories else 0,
            result.created_at,
        )
    finally:
        await projects.db.close()


@pytest.mark.asyncio
async def test_reset_refuses_missing_or_archived_project(tmp_path: Path) -> None:
    projects, _sessions = await _stores(tmp_path)
    try:
        assert await projects.reset(999, retain_repositories=False) is None
        project_id = await projects.create(name="Old")
        await projects.archive(project_id)
        with pytest.raises(ProjectResetError, match="archived"):
            await projects.reset(project_id, retain_repositories=False)
        assert len(await projects.list()) == 1
    finally:
        await projects.db.close()


@pytest.mark.asyncio
async def test_reset_rolls_back_successor_and_archive_when_lineage_write_fails(
    tmp_path: Path,
) -> None:
    projects, _sessions = await _stores(tmp_path)
    try:
        project_id = await projects.create(name="Protected")
        await projects.db.execute(
            "CREATE TRIGGER fail_project_reset BEFORE INSERT ON project_reset_events "
            "BEGIN SELECT RAISE(ABORT, 'injected reset failure'); END"
        )
        await projects.db.commit()

        with pytest.raises(Exception, match="injected reset failure"):
            await projects.reset(project_id, retain_repositories=True)

        project = await projects.get(project_id)
        assert project is not None and project.status == "active" and project.archived_at is None
        assert [item.id for item in await projects.list()] == [project_id]
        assert await (
            await projects.db.execute("SELECT COUNT(*) FROM project_reset_events")
        ).fetchone() == (0,)
    finally:
        await projects.db.close()


@pytest.mark.asyncio
async def test_reset_terminalizes_dormant_project_capabilities_only(tmp_path: Path) -> None:
    projects, _sessions = await _stores(tmp_path)
    try:
        project_id = await projects.create(name="Reset me")
        other_id = await projects.create(name="Keep me")
        tasks = TaskStore(projects.db, projects.lock)
        intents = IntentStore(projects.db, projects.lock)
        graph = GraphStore(projects.db, projects.lock)
        attention = AttentionStore(projects.db, projects.lock)
        analysis = AnalysisJobStore(projects.db, projects.lock)
        task_id = await tasks.add(
            kind="job",
            title="Dormant",
            payload="later",
            schedule_kind="once",
            schedule_spec="2099-01-01T00:00:00+00:00",
            timezone="UTC",
            next_run_at="2099-01-01T00:00:00+00:00",
            created_by="user",
            project_id=project_id,
        )
        other_task_id = await tasks.add(
            kind="job",
            title="Other",
            payload="later",
            schedule_kind="once",
            schedule_spec="2099-01-01T00:00:00+00:00",
            timezone="UTC",
            next_run_at="2099-01-01T00:00:00+00:00",
            created_by="user",
            project_id=other_id,
        )
        intent_id = await intents.create_draft(
            idempotency_key="reset-intent",
            provider="google",
            kind=IntentKind.DOC_CREATE,
            request={},
            summary="Dormant draft",
            source="user",
            project_id=project_id,
        )
        suggestion_id = await graph.add_suggestion(
            kind="memory", payload={"content": "maybe"}, trust_class="model_generated",
            project_id=project_id,
        )
        attention_id = await attention.create(
            kind="proposal", source="test", title="Review me", project_id=project_id
        )
        job, _created = await analysis.enqueue(
            project_id=project_id, snapshot_hash="abc", profile_version="v1"
        )

        await projects.reset(project_id, retain_repositories=False)

        assert (await tasks.get(task_id)).status == "cancelled"
        assert (await tasks.get(other_task_id)).status == "active"
        assert (await intents.get(intent_id)).state is IntentState.REJECTED
        assert (await graph.get_suggestion(suggestion_id)).status == "rejected"
        assert (await attention.get(attention_id)).state.value == "expired"
        assert (await analysis.get(job.id)).state.value == "discarded"
    finally:
        await projects.db.close()


@pytest.mark.asyncio
async def test_reset_refuses_any_running_project_task_without_partial_changes(
    tmp_path: Path,
) -> None:
    projects, _sessions = await _stores(tmp_path)
    try:
        project_id = await projects.create(name="Busy")
        tasks = TaskStore(projects.db, projects.lock)
        task_id = await tasks.add(
            kind="job",
            title="Running",
            payload="now",
            schedule_kind="once",
            schedule_spec="2026-07-14T00:00:00+00:00",
            timezone="UTC",
            next_run_at="2026-07-14T00:00:00+00:00",
            created_by="user",
            project_id=project_id,
        )
        await tasks.start_run(task_id, "2026-07-14T00:00:00+00:00")

        with pytest.raises(ProjectResetBusyError, match="in-flight"):
            await projects.reset(project_id, retain_repositories=False)

        assert (await projects.get(project_id)).status == "active"
        assert (await tasks.get(task_id)).status == "active"
        assert await (
            await projects.db.execute("SELECT COUNT(*) FROM project_reset_events")
        ).fetchone() == (0,)
    finally:
        await projects.db.close()
