"""TaskService tests: lifecycle semantics driven by an injected, stepped clock.

No test here sleeps — time moves only when the test says so.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from jarvis.config import SchedulerConfig
from jarvis.persistence.db import connect
from jarvis.scheduler.service import ScheduleError, TaskService
from jarvis.scheduler.store import TaskStore

UTC = dt.UTC
START = dt.datetime(2026, 7, 6, 8, 0, tzinfo=UTC)


class Clock:
    """A controllable now() — the whole service marches to this."""

    def __init__(self, at: dt.datetime = START) -> None:
        self.at = at

    def __call__(self) -> dt.datetime:
        return self.at

    def advance(self, **kwargs: float) -> None:
        self.at += dt.timedelta(**kwargs)


_OPEN_DBS: list = []


@pytest.fixture(autouse=True)
async def _close_dbs():
    """Close every aiosqlite connection opened during a test. Without this the
    connection threads outlive the test and pytest hangs at exit waiting on them."""
    yield
    while _OPEN_DBS:
        await _OPEN_DBS.pop().close()


async def _service(tmp_path: Path, **config_kw) -> tuple[TaskService, Clock]:
    clock = Clock()
    store = TaskStore(await connect(tmp_path / "tasks.db"))
    _OPEN_DBS.append(store.db)
    service = TaskService(store, SchedulerConfig(**config_kw), now=clock)
    return service, clock


async def _schedule(service: TaskService, **kw):
    return await service.schedule(
        kind=kw.get("kind", "job"),
        title=kw.get("title", "t"),
        payload=kw.get("payload", "do it"),
        schedule_kind=kw.get("schedule_kind", "interval"),
        schedule_spec=kw.get("schedule_spec", "3600"),
        created_by=kw.get("created_by", "user"),
        timezone=kw.get("timezone", "UTC"),
    )


# --- scheduling --------------------------------------------------------------


async def test_schedule_computes_first_fire(tmp_path: Path) -> None:
    service, _ = await _service(tmp_path)
    task = await _schedule(service, schedule_kind="cron", schedule_spec="0 9 * * *")
    assert task.next_run_at == "2026-07-06T09:00:00+00:00"  # today 09:00, we're at 08:00
    assert task.status == "active"


async def test_schedule_rejects_past_once_with_current_time_in_error(tmp_path: Path) -> None:
    # Models routinely compute wrong-timezone datetimes; the error must carry the
    # actual current time so the model can self-correct instead of guessing.
    service, _ = await _service(tmp_path)
    with pytest.raises(ScheduleError) as exc:
        await _schedule(service, schedule_kind="once", schedule_spec="2026-07-06T07:00:00")
    assert "in the past" in str(exc.value)
    assert "2026-07-06 08:00" in str(exc.value)  # the clock, shown in the task tz (UTC)


async def test_schedule_once_just_past_runs_now(tmp_path: Path) -> None:
    # "in one minute" must not lose a race with the clock: <=2 min past -> now.
    service, clock = await _service(tmp_path)
    task = await _schedule(service, schedule_kind="once", schedule_spec="2026-07-06T07:59:00")
    assert task.next_run_at == clock().isoformat()


async def test_schedule_surfaces_validation_errors(tmp_path: Path) -> None:
    service, _ = await _service(tmp_path)
    with pytest.raises(ScheduleError, match="at least 60"):
        await _schedule(service, schedule_spec="5")
    with pytest.raises(ScheduleError, match="invalid cron"):
        await _schedule(service, schedule_kind="cron", schedule_spec="nope")


async def test_schedule_records_provenance(tmp_path: Path) -> None:
    service, _ = await _service(tmp_path)
    # source_session_id has a real FK to sessions(id) — insert a session to point at.
    await service.store.db.execute(
        "INSERT INTO sessions (id, created_at, updated_at) VALUES (42, ?, ?)",
        (START.isoformat(), START.isoformat()),
    )
    await service.store.db.commit()
    service.bound_session_id = 42
    task = await _schedule(service, created_by="agent")
    assert task.created_by == "agent"
    assert task.source_session_id == 42


# --- due classification (D5) --------------------------------------------------


async def test_due_classifies_fire_fire_late_and_missed(tmp_path: Path) -> None:
    service, clock = await _service(tmp_path, misfire_grace_seconds=3600)
    reminder = await _schedule(service, kind="reminder", schedule_spec="3600")  # fires 09:00
    job = await _schedule(service, kind="job", schedule_spec="3600")  # fires 09:00

    assert await service.due() == []  # nothing due at 08:00

    clock.advance(hours=1, minutes=30)  # 09:30 — 30 min late, within grace
    actions = {d.task.id: d.action for d in await service.due()}
    assert actions == {reminder.id: "fire", job.id: "fire"}

    clock.advance(hours=2)  # 11:30 — 2.5h late, beyond the 1h grace
    actions = {d.task.id: d.action for d in await service.due()}
    assert actions == {reminder.id: "fire_late", job.id: "missed"}


# --- advancement -------------------------------------------------------------


async def test_complete_run_advances_from_scheduled_time_not_completion(tmp_path: Path) -> None:
    service, clock = await _service(tmp_path)
    await _schedule(service, schedule_spec="3600")  # hourly, first fire 09:00
    clock.advance(hours=1, minutes=20)  # fires late at 09:20
    (due,) = await service.due()
    run_id = await service.begin_run(due)
    clock.advance(minutes=10)  # the run itself takes 10 minutes
    task = await service.complete_run(due, run_id, ok=True, result_text="done")
    # anchored to the scheduled 09:00 fire -> next is 10:00, not 10:30
    assert task.next_run_at == "2026-07-06T10:00:00+00:00"
    assert task.consecutive_failures == 0


async def test_once_task_completes_to_done(tmp_path: Path) -> None:
    service, clock = await _service(tmp_path)
    await _schedule(service, schedule_kind="once", schedule_spec="2026-07-06T09:00:00")
    clock.advance(hours=1, minutes=1)
    (due,) = await service.due()
    run_id = await service.begin_run(due)
    task = await service.complete_run(due, run_id, ok=True)
    assert (task.status, task.next_run_at) == ("done", None)


async def test_failure_cap_flips_task_to_failed(tmp_path: Path) -> None:
    service, clock = await _service(tmp_path, max_consecutive_failures=3)
    await _schedule(service, schedule_spec="3600")
    for expected_failures in (1, 2, 3):
        clock.advance(hours=1, minutes=1)
        (due,) = await service.due()
        run_id = await service.begin_run(due)
        task = await service.complete_run(due, run_id, ok=False, error="boom")
        assert task.consecutive_failures == expected_failures
    assert (task.status, task.next_run_at) == ("failed", None)
    assert task.last_error == "boom"
    assert await service.due() == []  # failed tasks never look due again


async def test_success_resets_the_failure_counter(tmp_path: Path) -> None:
    service, clock = await _service(tmp_path, max_consecutive_failures=3)
    await _schedule(service, schedule_spec="3600")
    for _ in range(2):
        clock.advance(hours=1, minutes=1)
        (due,) = await service.due()
        run_id = await service.begin_run(due)
        task = await service.complete_run(due, run_id, ok=False, error="flaky")
    assert task.consecutive_failures == 2
    clock.advance(hours=1, minutes=1)
    (due,) = await service.due()
    run_id = await service.begin_run(due)
    task = await service.complete_run(due, run_id, ok=True)
    assert (task.consecutive_failures, task.status) == (0, "active")


async def test_once_job_error_fails_immediately(tmp_path: Path) -> None:
    # once-tasks don't retry: there is no future occurrence to retry at.
    service, clock = await _service(tmp_path)
    await _schedule(service, schedule_kind="once", schedule_spec="2026-07-06T09:00:00")
    clock.advance(hours=1, minutes=1)
    (due,) = await service.due()
    run_id = await service.begin_run(due)
    task = await service.complete_run(due, run_id, ok=False, error="boom")
    assert (task.status, task.next_run_at) == ("failed", None)


async def test_result_text_is_bounded(tmp_path: Path) -> None:
    service, clock = await _service(tmp_path)
    await _schedule(service, schedule_spec="3600")
    clock.advance(hours=1, minutes=1)
    (due,) = await service.due()
    run_id = await service.begin_run(due)
    await service.complete_run(due, run_id, ok=True, result_text="x" * 50_000)
    (run,) = await service.store.runs_for(due.task.id, limit=1)
    assert len(run.result_text) < 11_000
    assert run.result_text.endswith("…[truncated]")


# --- missed + sweep ----------------------------------------------------------


async def test_missed_recurring_collapses_to_one_row_and_resumes_from_now(
    tmp_path: Path,
) -> None:
    # REPL closed for 3 days over a daily cron: ONE missed row, next fire in the
    # future — never a loop over the skipped occurrences.
    service, clock = await _service(tmp_path, misfire_grace_seconds=3600)
    await _schedule(service, schedule_kind="cron", schedule_spec="0 9 * * *")
    clock.advance(days=3)  # now 2026-07-09 08:00; missed 3 daily fires
    (due,) = await service.due()
    assert due.action == "missed"
    task = await service.record_missed(due)
    runs = await service.store.runs_for(task.id)
    assert [r.status for r in runs] == ["missed"]  # exactly one row for the gap
    assert task.next_run_at == "2026-07-09T09:00:00+00:00"  # from now, in the future
    assert task.status == "active"


async def test_missed_once_goes_terminal(tmp_path: Path) -> None:
    service, clock = await _service(tmp_path, misfire_grace_seconds=3600)
    await _schedule(service, schedule_kind="once", schedule_spec="2026-07-06T09:00:00")
    clock.advance(days=1)
    (due,) = await service.due()
    assert due.action == "missed"
    task = await service.record_missed(due)
    assert (task.status, task.next_run_at) == ("missed", None)


async def test_sweep_aborts_orphans_and_never_reruns_them(tmp_path: Path) -> None:
    service, clock = await _service(tmp_path)
    await _schedule(service, schedule_spec="3600")
    clock.advance(hours=1, minutes=1)
    (due,) = await service.due()
    await service.begin_run(due)  # ...and the process "dies" here

    notes = await service.sweep_stale_runs()  # next startup
    assert len(notes) == 1 and "NOT retried" in notes[0]
    task = await service.store.get(due.task.id)
    assert task.consecutive_failures == 1
    assert task.last_error.startswith("interrupted")
    (run,) = await service.store.runs_for(task.id, limit=1)
    assert run.status == "aborted"
    # advanced past the interrupted occurrence: nothing is due right now
    assert await service.due() == []
    assert task.next_run_at > clock().isoformat()


async def test_sweep_once_task_fails_terminal(tmp_path: Path) -> None:
    service, clock = await _service(tmp_path)
    await _schedule(service, schedule_kind="once", schedule_spec="2026-07-06T09:00:00")
    clock.advance(hours=1, minutes=1)
    (due,) = await service.due()
    await service.begin_run(due)
    await service.sweep_stale_runs()
    task = await service.store.get(due.task.id)
    assert (task.status, task.next_run_at) == ("failed", None)


# --- cancel + describe ---------------------------------------------------------


async def test_cancel_roundtrip(tmp_path: Path) -> None:
    service, _ = await _service(tmp_path)
    task = await _schedule(service)
    cancelled = await service.cancel(task.id)
    assert cancelled is not None and cancelled.status == "cancelled"
    assert await service.cancel(task.id) is None  # already terminal
    assert await service.cancel(999) is None


async def test_describe_shows_schedule_and_local_next_fire(tmp_path: Path) -> None:
    service, _ = await _service(tmp_path)
    task = await _schedule(
        service, kind="job", title="digest", schedule_kind="cron", schedule_spec="0 9 * * *"
    )
    text = service.describe(task)
    assert 'job #1 "digest"' in text
    assert "cron '0 9 * * *' (UTC)" in text
    assert "2026-07-06 09:00" in text
    assert "in 1h 0m" in text  # created at 08:00
