"""AgentRunStore: the agent_runs audit trail for delegated sub-agent runs (Phase 6).

Mirrors the task_runs discipline — a 'running' row opened before the child executes,
completed with its outcome, and swept to 'aborted' if a crash left it open. Keyless.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from jarvis.agents import AgentRun, AgentRunStore
from jarvis.persistence import SessionStore
from jarvis.persistence.db import connect


async def _store(tmp_path: Path) -> tuple[AgentRunStore, SessionStore]:
    # Share one connection + lock, like the REPL wires the real stores.
    db = await connect(tmp_path / "a.db")
    lock = asyncio.Lock()
    return AgentRunStore(db, lock), SessionStore(db, lock)


async def test_begin_run_opens_a_running_row(tmp_path: Path) -> None:
    runs, _ = await _store(tmp_path)
    try:
        run_id = await runs.begin_run(
            parent_session_id=None,
            parent_trace_id="tr-parent",
            title="research",
            prompt="find X",
            tools_scope=["web_search", "web_fetch"],
        )
        run = await runs.get(run_id)
        assert isinstance(run, AgentRun)
        assert run.status == "running"
        assert run.title == "research"
        assert run.prompt == "find X"
        assert run.tools_scope == ["web_search", "web_fetch"]  # JSON round-trips as a list
        assert run.parent_trace_id == "tr-parent"
        assert run.finished_at is None
    finally:
        await runs.db.close()


async def test_complete_run_records_outcome_and_totals(tmp_path: Path) -> None:
    runs, _ = await _store(tmp_path)
    try:
        run_id = await runs.begin_run(
            parent_session_id=None,
            parent_trace_id=None,
            title="t",
            prompt="p",
            tools_scope=["read_file"],
        )
        await runs.complete_run(
            run_id,
            status="ok",
            child_session_id=None,
            child_trace_id="tr-child",
            iterations=4,
            denied_count=1,
            input_tokens=100,
            output_tokens=20,
            cost_usd=0.0031,
            result_text="found it",
        )
        run = await runs.get(run_id)
        assert run is not None
        assert run.status == "ok"
        assert run.child_trace_id == "tr-child"
        assert run.iterations == 4
        assert run.denied_count == 1
        assert run.input_tokens == 100
        assert run.output_tokens == 20
        assert run.cost_usd == 0.0031
        assert run.result_text == "found it"
        assert run.finished_at is not None
    finally:
        await runs.db.close()


async def test_list_is_recent_first_and_scopes_by_parent(tmp_path: Path) -> None:
    runs, sessions = await _store(tmp_path)
    try:
        parent_a = await sessions.create_session(title="A")
        parent_b = await sessions.create_session(title="B")
        r1 = await runs.begin_run(
            parent_session_id=parent_a,
            parent_trace_id=None,
            title="a1",
            prompt="p",
            tools_scope=[],
        )
        r2 = await runs.begin_run(
            parent_session_id=parent_b,
            parent_trace_id=None,
            title="b1",
            prompt="p",
            tools_scope=[],
        )
        r3 = await runs.begin_run(
            parent_session_id=parent_a,
            parent_trace_id=None,
            title="a2",
            prompt="p",
            tools_scope=[],
        )
        all_ids = [r.id for r in await runs.list()]
        assert all_ids == [r3, r2, r1]  # most recent first
        a_ids = [r.id for r in await runs.list(parent_session_id=parent_a)]
        assert a_ids == [r3, r1]  # only parent A's runs, recent first
    finally:
        await runs.db.close()


async def test_sweep_orphans_marks_running_as_aborted(tmp_path: Path) -> None:
    runs, _ = await _store(tmp_path)
    try:
        orphan = await runs.begin_run(
            parent_session_id=None,
            parent_trace_id=None,
            title="stuck",
            prompt="p",
            tools_scope=[],
        )
        done = await runs.begin_run(
            parent_session_id=None,
            parent_trace_id=None,
            title="clean",
            prompt="p",
            tools_scope=[],
        )
        await runs.complete_run(done, status="ok")

        notes = await runs.sweep_orphans()
        assert len(notes) == 1
        assert "stuck" in notes[0]
        assert (await runs.get(orphan)).status == "aborted"
        assert (await runs.get(orphan)).error is not None
        assert (await runs.get(done)).status == "ok"  # a completed run is untouched

        assert await runs.sweep_orphans() == []  # idempotent: nothing left running
    finally:
        await runs.db.close()


async def test_get_missing_run_is_none(tmp_path: Path) -> None:
    runs, _ = await _store(tmp_path)
    try:
        assert await runs.get(999) is None
    finally:
        await runs.db.close()


async def test_malformed_skills_manifest_does_not_break_historical_run_reads(
    tmp_path: Path,
) -> None:
    runs, _ = await _store(tmp_path)
    try:
        run_id = await runs.begin_run(
            parent_session_id=None,
            parent_trace_id=None,
            title="historical",
            prompt="p",
            tools_scope=[],
        )
        await runs.db.execute(
            "UPDATE agent_runs SET skills_manifest_json = ? WHERE id = ?", ("{not json", run_id)
        )
        await runs.db.commit()
        run = await runs.get(run_id)
        assert run is not None and run.skills_manifest == []
    finally:
        await runs.db.close()
