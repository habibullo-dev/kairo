"""TaskStore tests: CRUD, due() coalescing, run bookkeeping, atomicity, sweep."""

from __future__ import annotations

from pathlib import Path

import aiosqlite
import pytest

from jarvis.persistence.db import connect
from jarvis.scheduler.store import ParkedContinuation, TaskAdvance, TaskStore

T0 = "2026-07-06T09:00:00+00:00"
T1 = "2026-07-06T10:00:00+00:00"
T2 = "2026-07-06T11:00:00+00:00"


async def _store(tmp_path: Path) -> TaskStore:
    return TaskStore(await connect(tmp_path / "tasks.db"))


async def _add(store: TaskStore, **kw) -> int:
    return await store.add(
        kind=kw.get("kind", "job"),
        title=kw.get("title", "a task"),
        payload=kw.get("payload", "do the thing"),
        schedule_kind=kw.get("schedule_kind", "once"),
        schedule_spec=kw.get("schedule_spec", T1),
        timezone=kw.get("timezone", "UTC"),
        next_run_at=kw.get("next_run_at", T1),
        created_by=kw.get("created_by", "user"),
        source_session_id=kw.get("source_session_id"),
        project_id=kw.get("project_id"),
    )


async def test_add_get_roundtrip(tmp_path: Path) -> None:
    store = await _store(tmp_path)
    try:
        tid = await _add(store, kind="reminder", payload="stretch", created_by="agent")
        task = await store.get(tid)
        assert task is not None
        assert (task.kind, task.payload, task.created_by) == ("reminder", "stretch", "agent")
        assert task.status == "active"
        assert task.next_run_at == T1
        assert task.consecutive_failures == 0
        assert await store.get(9999) is None
    finally:
        await store.db.close()


async def test_list_filters_active_by_default(tmp_path: Path) -> None:
    store = await _store(tmp_path)
    try:
        active = await _add(store)
        finished = await _add(store)
        assert await store.cancel(finished) is True
        ids = [t.id for t in await store.list()]
        assert ids == [active]
        all_ids = [t.id for t in await store.list(include_finished=True)]
        assert all_ids == [active, finished]
    finally:
        await store.db.close()


async def test_cancel_is_a_status_flip_never_a_delete(tmp_path: Path) -> None:
    store = await _store(tmp_path)
    try:
        tid = await _add(store)
        assert await store.cancel(tid) is True
        task = await store.get(tid)  # row still fetchable — audit trail intact
        assert task is not None
        assert task.status == "cancelled"
        assert task.next_run_at is None  # terminal states never look due
        assert await store.cancel(tid) is False  # already terminal
        assert await store.cancel(4242) is False  # unknown id
    finally:
        await store.db.close()


async def test_due_orders_and_filters(tmp_path: Path) -> None:
    store = await _store(tmp_path)
    try:
        later = await _add(store, next_run_at=T1)
        earlier = await _add(store, next_run_at=T0)
        future = await _add(store, next_run_at="2027-01-01T00:00:00+00:00")
        cancelled = await _add(store, next_run_at=T0)
        await store.cancel(cancelled)

        due = await store.due(T2)
        assert [t.id for t in due] == [earlier, later]  # ordered by fire time
        assert future not in [t.id for t in due]
    finally:
        await store.db.close()


async def test_due_excludes_tasks_with_a_running_run(tmp_path: Path) -> None:
    # Coalescing: a task already executing never fires on top of itself.
    store = await _store(tmp_path)
    try:
        tid = await _add(store, next_run_at=T0)
        run_id = await store.start_run(tid, scheduled_for=T0)
        assert [t.id for t in await store.due(T2)] == []
        await store.finish_run(run_id, "ok")
        assert [t.id for t in await store.due(T2)] == [tid]  # next_run_at unchanged here
    finally:
        await store.db.close()


async def test_run_roundtrip_and_atomic_advance(tmp_path: Path) -> None:
    store = await _store(tmp_path)
    try:
        tid = await _add(store, schedule_kind="interval", schedule_spec="3600")
        run_id = await store.start_run(tid, scheduled_for=T0)
        started = (await store.runs_for(tid))[0]
        assert started.status == "running"
        assert started.started_at is not None
        assert (await store.get(tid)).last_run_at is not None  # stamped at start

        await store.finish_run(
            run_id,
            "ok",
            session_id=None,
            result_text="all good",
            denied_count=2,
            cost_usd=0.12,
            advance=TaskAdvance(task_id=tid, next_run_at=T2, consecutive_failures=0),
        )
        run = (await store.runs_for(tid))[0]
        assert (run.status, run.result_text, run.denied_count, run.cost_usd) == (
            "ok",
            "all good",
            2,
            0.12,
        )
        assert run.finished_at is not None
        task = await store.get(tid)
        assert task.next_run_at == T2  # advanced in the same transaction
        assert task.status == "active"
    finally:
        await store.db.close()


async def test_parked_run_is_coalesced_restart_safe_and_claimed_once(tmp_path: Path) -> None:
    store = await _store(tmp_path)
    try:
        tid = await _add(store, next_run_at=T0)
        run_id = await store.start_run(tid, scheduled_for=T0)
        await store.db.execute(
            "INSERT INTO sessions (id, created_at, updated_at) VALUES (77, ?, ?)", (T0, T0)
        )
        await store.db.commit()
        continuation = ParkedContinuation.from_call(
            tool_id="toolu-123",
            tool_name="write_file",
            tool_input={"content": "exact bytes", "path": "report.txt"},
            decision_reason="needs explicit approval",
        )

        assert await store.park_run(run_id, session_id=77, continuation=continuation)
        assert not await store.park_run(run_id, session_id=77, continuation=continuation)

        (run,) = await store.runs_for(tid)
        assert run.status == "running"  # preserves the existing one-run coalescing invariant
        assert run.approval_state == "pending"
        assert run.session_id == 77
        assert run.continuation == continuation
        assert await store.due(T2) == []  # task cannot fire again while it is parked
        assert await store.earliest_next_run() is None  # parking cannot spin the wake loop
        assert await store.stale_runs() == []  # restart must not abort/replay an intentional park

        claimed = await store.claim_parked_approval(run_id, resolution="approve")
        assert claimed is not None
        assert (claimed.resolution, claimed.continuation) == ("approve", continuation)
        assert await store.claim_parked_approval(run_id, resolution="approve") is None
        (claimed_run,) = await store.runs_for(tid)
        assert claimed_run.approval_state == "approved"
        assert await store.stale_runs() == []  # a claimed action is still never auto-replayed
    finally:
        await store.db.close()


async def test_pending_approval_count_uses_only_durable_pending_rows(tmp_path: Path) -> None:
    store = await _store(tmp_path)
    try:
        from jarvis.projects import ProjectStore

        projects = ProjectStore(store.db, store.lock)
        alpha = await projects.create(name="Alpha")
        beta = await projects.create(name="Beta")
        task_ids = [
            await _add(store, next_run_at=T0, project_id=alpha),
            await _add(store, next_run_at=T0, project_id=alpha),
            await _add(store, next_run_at=T0, project_id=beta),
        ]
        await store.db.execute(
            "INSERT INTO sessions (id, created_at, updated_at) VALUES (81, ?, ?)", (T0, T0)
        )
        await store.db.commit()
        continuation = ParkedContinuation.from_call(
            tool_id="toolu-count",
            tool_name="write_file",
            tool_input={"path": "safe.txt", "content": "safe"},
            decision_reason="needs explicit approval",
        )
        runs = [await store.start_run(task_id, scheduled_for=T0) for task_id in task_ids]
        for run_id in runs:
            assert await store.park_run(run_id, session_id=81, continuation=continuation)
        assert await store.pending_approval_count(project_id=alpha) == 2
        assert await store.pending_approval_count(project_id=beta) == 1
        assert await store.pending_approval_count(project_id=None) == 0
        assert await store.claim_parked_approval(runs[0], resolution="reject") is not None
        assert await store.pending_approval_count(project_id=alpha) == 1
    finally:
        await store.db.close()


async def test_rejected_parked_approval_never_returns_an_executable_continuation(
    tmp_path: Path,
) -> None:
    store = await _store(tmp_path)
    try:
        tid = await _add(store, next_run_at=T0)
        run_id = await store.start_run(tid, scheduled_for=T0)
        await store.db.execute(
            "INSERT INTO sessions (id, created_at, updated_at) VALUES (79, ?, ?)", (T0, T0)
        )
        await store.db.commit()
        continuation = ParkedContinuation.from_call(
            tool_id="toolu-789",
            tool_name="write_file",
            tool_input={"path": "nope.txt", "content": "must not execute"},
            decision_reason="needs explicit approval",
        )
        assert await store.park_run(run_id, session_id=79, continuation=continuation)

        claim = await store.claim_parked_approval(run_id, resolution="reject")
        assert claim is not None
        assert (claim.resolution, claim.continuation) == ("reject", None)
    finally:
        await store.db.close()


async def test_parked_continuation_refuses_tampered_input(tmp_path: Path) -> None:
    store = await _store(tmp_path)
    try:
        tid = await _add(store, next_run_at=T0)
        run_id = await store.start_run(tid, scheduled_for=T0)
        await store.db.execute(
            "INSERT INTO sessions (id, created_at, updated_at) VALUES (78, ?, ?)", (T0, T0)
        )
        await store.db.commit()
        continuation = ParkedContinuation.from_call(
            tool_id="toolu-456",
            tool_name="write_file",
            tool_input={"path": "safe.txt", "content": "safe"},
            decision_reason="needs explicit approval",
        )
        assert await store.park_run(run_id, session_id=78, continuation=continuation)

        # Simulate on-disk corruption or tampering.  The read/claim boundary must fail closed,
        # never reinterpret the altered input as the approved request.
        await store.db.execute(
            "UPDATE task_runs SET continuation_json = "
            "replace(continuation_json, 'safe.txt', 'evil.txt') "
            "WHERE id = ?",
            (run_id,),
        )
        await store.db.commit()
        with pytest.raises(ValueError, match="hash"):
            await store.claim_parked_approval(run_id, resolution="approve")
    finally:
        await store.db.close()


async def test_finish_run_records_failure_bookkeeping(tmp_path: Path) -> None:
    store = await _store(tmp_path)
    try:
        tid = await _add(store)
        run_id = await store.start_run(tid, scheduled_for=T0)
        await store.finish_run(
            run_id,
            "error",
            error="exploded",
            advance=TaskAdvance(
                task_id=tid,
                next_run_at=None,
                status="failed",  # service decided the cap was hit
                consecutive_failures=3,
                last_error="exploded",
            ),
        )
        task = await store.get(tid)
        assert (task.status, task.consecutive_failures, task.last_error) == (
            "failed",
            3,
            "exploded",
        )
        assert task.next_run_at is None
    finally:
        await store.db.close()


async def test_finish_run_atomicity_rolls_back_on_bad_advance(tmp_path: Path) -> None:
    # An advance that violates the schema CHECK (terminal + next_run_at set) must
    # roll back the run-row update too — never a closed run with an un-advanced task.
    store = await _store(tmp_path)
    try:
        tid = await _add(store)
        run_id = await store.start_run(tid, scheduled_for=T0)
        bad = TaskAdvance(task_id=tid, next_run_at=T2, status="done")  # violates CHECK
        with pytest.raises(aiosqlite.IntegrityError):
            await store.finish_run(run_id, "ok", result_text="won't persist", advance=bad)
        run = (await store.runs_for(tid))[0]
        assert run.status == "running"  # rolled back with the failed advance
        assert run.result_text is None
    finally:
        await store.db.close()


async def test_record_missed_inserts_closed_row_and_advances(tmp_path: Path) -> None:
    store = await _store(tmp_path)
    try:
        tid = await _add(store, schedule_kind="cron", schedule_spec="0 9 * * *")
        await store.record_missed(
            tid, scheduled_for=T0, advance=TaskAdvance(task_id=tid, next_run_at=T2)
        )
        run = (await store.runs_for(tid))[0]
        assert run.status == "missed"
        assert run.started_at is None  # nothing ever ran
        assert run.finished_at is not None  # but the row is closed, not stale
        assert (await store.get(tid)).next_run_at == T2
    finally:
        await store.db.close()


async def test_stale_runs_lists_only_orphaned_running_rows(tmp_path: Path) -> None:
    store = await _store(tmp_path)
    try:
        tid = await _add(store)
        orphan = await store.start_run(tid, scheduled_for=T0)
        done = await store.start_run(tid, scheduled_for=T1)
        await store.finish_run(done, "ok")
        await store.record_missed(
            tid, scheduled_for=T2, advance=TaskAdvance(task_id=tid, next_run_at=None, status="done")
        )

        stale = await store.stale_runs()
        assert [r.id for r in stale] == [orphan]

        # the sweep path: close the orphan as aborted; never re-run it silently
        await store.finish_run(
            orphan,
            "aborted",
            error="interrupted: process died mid-run",
            advance=TaskAdvance(task_id=tid, next_run_at=None, status="done"),
        )
        assert await store.stale_runs() == []
        assert (await store.get(tid)).status == "done"
    finally:
        await store.db.close()
