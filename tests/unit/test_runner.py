"""BackgroundRunner + JobRunner: firing semantics and unattended job execution.

FakeClient + stepped clock throughout — no test sleeps. Connections opened here
are closed by an autouse fixture (an unclosed aiosqlite connection hangs pytest
at exit)."""

from __future__ import annotations

import asyncio
import datetime as dt
from pathlib import Path
from types import SimpleNamespace

import pytest

from jarvis.attention import AttentionStore
from jarvis.cli.jobs import JobRunner
from jarvis.config import SchedulerConfig, load_config
from jarvis.core import FakeClient, ToolCall, text_message, tool_use_message
from jarvis.permissions import PermissionGate, Policy
from jarvis.persistence.db import connect
from jarvis.persistence.sessions import SessionStore
from jarvis.projects import ProjectService, ProjectStore
from jarvis.scheduler.runner import BackgroundRunner, JobOutcome
from jarvis.scheduler.service import TaskService
from jarvis.scheduler.store import TaskStore
from jarvis.tools import Permission, ToolContext
from jarvis.tools.executor import ToolExecutor
from jarvis.tools.registry import ToolRegistry

UTC = dt.UTC
START = dt.datetime(2026, 7, 6, 8, 0, tzinfo=UTC)

_OPEN_DBS: list = []


@pytest.fixture(autouse=True)
async def _close_dbs():
    yield
    while _OPEN_DBS:
        await _OPEN_DBS.pop().close()


class Clock:
    def __init__(self, at: dt.datetime = START) -> None:
        self.at = at

    def __call__(self) -> dt.datetime:
        return self.at

    def advance(self, **kw: float) -> None:
        self.at += dt.timedelta(**kw)


async def _service(tmp_path: Path, **config_kw) -> tuple[TaskService, Clock, TaskStore]:
    clock = Clock()
    store = TaskStore(await connect(tmp_path / "tasks.db"))
    _OPEN_DBS.append(store.db)
    service = TaskService(
        store,
        SchedulerConfig(**config_kw),
        now=clock,
        attention=AttentionStore(store.db, store.lock),
    )
    return service, clock, store


async def _schedule(service: TaskService, **kw):
    return await service.schedule(
        kind=kw.get("kind", "job"),
        title=kw.get("title", "t"),
        payload=kw.get("payload", "do it"),
        schedule_kind=kw.get("schedule_kind", "interval"),
        schedule_spec=kw.get("schedule_spec", "3600"),
        created_by=kw.get("created_by", "user"),
        timezone="UTC",
        project_id=kw.get("project_id"),
        origin=kw.get("origin", "local"),
    )


class Recorder:
    """Captures notify lines and can stand in as a run_job that returns/raises/counts."""

    def __init__(self) -> None:
        self.lines: list[str] = []
        self.job_calls = 0

    def notify(self, line: str) -> None:
        self.lines.append(line)


class CountOnlyRouter:
    """Captures the count-only seam without constructing a real external notifier."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def notify(self, **kwargs) -> None:
        self.calls.append(kwargs)


class BlockingCountOnlyRouter(CountOnlyRouter):
    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def notify(self, **kwargs) -> None:
        self.calls.append(kwargs)
        self.started.set()
        await self.release.wait()


# --- BackgroundRunner: lifecycle ---------------------------------------------


class _LifecycleService:
    def __init__(self) -> None:
        self.config = SchedulerConfig(wake_cap_seconds=30)
        self.sleep_started = asyncio.Event()

    async def seconds_until_next(self) -> None:
        self.sleep_started.set()
        return None


class _ControlledLifecycleRunner(BackgroundRunner):
    def __init__(
        self,
        service: _LifecycleService,
        cycles: list[tuple[asyncio.Event, asyncio.Event]],
    ) -> None:
        super().__init__(
            service,  # type: ignore[arg-type]
            notify=lambda _line: None,
            run_job=None,  # type: ignore[arg-type]
            turn_lock=asyncio.Lock(),
        )
        self.cycles = cycles
        self.check_count = 0

    async def check_due(self, *, stop_event: asyncio.Event | None = None) -> int:
        del stop_event
        index = self.check_count
        self.check_count += 1
        if index >= len(self.cycles):
            raise AssertionError("unexpected extra wake-loop generation")
        started, release = self.cycles[index]
        started.set()
        await release.wait()
        return 0


class _ControlledTurnLock:
    def __init__(self) -> None:
        self.waiting = asyncio.Event()
        self.release = asyncio.Event()

    async def __aenter__(self):
        self.waiting.set()
        await self.release.wait()
        return self

    async def __aexit__(self, *_exc) -> None:
        return None


class _OneDueReminderService(_LifecycleService):
    def __init__(self) -> None:
        super().__init__()
        self.due_calls = 0
        self.begin_calls = 0
        task = SimpleNamespace(id=1, kind="reminder", title="queued", payload="do not fire")
        self.reminder = SimpleNamespace(action="fire", task=task, scheduled_for=START.isoformat())

    async def due(self) -> list[SimpleNamespace]:
        self.due_calls += 1
        return [self.reminder] if self.due_calls == 1 else []

    async def begin_run(self, _due) -> int:
        self.begin_calls += 1
        return 1

    async def complete_run(self, *_args, **_kwargs) -> None:
        return None


class _OneDueDigestService(_OneDueReminderService):
    def __init__(self) -> None:
        super().__init__()
        self.reminder.task.kind = "digest"


async def _first_completed(*tasks: asyncio.Future) -> set[asyncio.Future]:
    done, _pending = await asyncio.wait_for(
        asyncio.wait(set(tasks), return_when=asyncio.FIRST_COMPLETED), timeout=3
    )
    return done


async def test_start_is_idempotent_and_stop_wake_cannot_be_lost_after_a_check() -> None:
    service = _LifecycleService()
    check_started = asyncio.Event()
    release_check = asyncio.Event()
    runner = _ControlledLifecycleRunner(service, [(check_started, release_check)])

    runner.start()
    owned = runner._task
    runner.start()
    assert runner._task is owned and runner.is_running
    await check_started.wait()

    stopping_generation = runner.request_stop()
    assert stopping_generation is owned
    assert runner._stop.is_set()
    assert not runner.is_running
    stopping = asyncio.shield(stopping_generation)
    sleeping = asyncio.create_task(service.sleep_started.wait())
    release_check.set()
    try:
        done = await _first_completed(stopping, sleeping)
        assert stopping in done
        assert not service.sleep_started.is_set()
        assert runner._task is None
    finally:
        if not stopping.done():
            runner.kick()
            await stopping
        sleeping.cancel()
        await asyncio.gather(sleeping, return_exceptions=True)


async def test_cancelling_a_stop_waiter_never_cancels_the_owned_loop() -> None:
    service = _LifecycleService()
    check_started = asyncio.Event()
    release_check = asyncio.Event()
    runner = _ControlledLifecycleRunner(service, [(check_started, release_check)])
    runner.start()
    await check_started.wait()
    owned = runner._task
    assert owned is not None

    stopping = asyncio.create_task(runner.stop())
    await runner._stop.wait()
    stopping.cancel()
    with pytest.raises(asyncio.CancelledError):
        await stopping
    assert runner._task is owned
    assert not owned.done() and not owned.cancelled()
    assert not runner.is_running

    sleeping = asyncio.create_task(service.sleep_started.wait())
    release_check.set()
    try:
        done = await _first_completed(owned, sleeping)
        assert owned in done
        assert runner._task is None
    finally:
        if not owned.done():
            runner.kick()
            await owned
        sleeping.cancel()
        await asyncio.gather(sleeping, return_exceptions=True)


async def test_stop_while_due_waits_for_turn_lock_never_starts_that_work() -> None:
    service = _OneDueReminderService()
    turn_lock = _ControlledTurnLock()
    notifications: list[str] = []
    runner = BackgroundRunner(
        service,  # type: ignore[arg-type]
        notify=notifications.append,
        run_job=None,  # type: ignore[arg-type]
        turn_lock=turn_lock,  # type: ignore[arg-type]
    )
    runner.start()
    await turn_lock.waiting.wait()

    stopping_generation = runner.request_stop()
    assert stopping_generation is runner._task
    stopping = asyncio.shield(stopping_generation)
    sleeping = asyncio.create_task(service.sleep_started.wait())
    turn_lock.release.set()
    try:
        done = await _first_completed(stopping, sleeping)
        assert stopping in done
        assert notifications == []
        assert service.begin_calls == 0
        assert runner._task is None and not runner.is_running
    finally:
        if not stopping.done():
            runner.kick()
            await stopping
        sleeping.cancel()
        await asyncio.gather(sleeping, return_exceptions=True)


async def test_stop_while_digest_waits_for_turn_lock_never_starts_that_work() -> None:
    service = _OneDueDigestService()
    turn_lock = _ControlledTurnLock()
    digest_calls = 0

    async def run_digest(_task) -> JobOutcome:
        nonlocal digest_calls
        digest_calls += 1
        return JobOutcome(session_id=None, text="must not run")

    runner = BackgroundRunner(
        service,  # type: ignore[arg-type]
        notify=lambda _line: None,
        run_job=None,  # type: ignore[arg-type]
        run_digest=run_digest,
        turn_lock=turn_lock,  # type: ignore[arg-type]
    )
    runner.start()
    await turn_lock.waiting.wait()

    stopping_generation = runner.request_stop()
    assert stopping_generation is runner._task
    stopping = asyncio.shield(stopping_generation)
    sleeping = asyncio.create_task(service.sleep_started.wait())
    turn_lock.release.set()
    try:
        done = await _first_completed(stopping, sleeping)
        assert stopping in done
        assert service.begin_calls == 0
        assert digest_calls == 0
        assert runner._task is None and not runner.is_running
    finally:
        if not stopping.done():
            runner.kick()
            await stopping
        sleeping.cancel()
        await asyncio.gather(sleeping, return_exceptions=True)


async def test_start_during_stop_restarts_once_and_the_last_stop_command_wins() -> None:
    service = _LifecycleService()
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    second_started = asyncio.Event()
    release_second = asyncio.Event()
    runner = _ControlledLifecycleRunner(
        service,
        [(first_started, release_first), (second_started, release_second)],
    )
    runner.start()
    runner.start()
    await first_started.wait()
    first = runner._task
    assert first is not None

    first_generation = runner.request_stop()
    assert first_generation is first and runner._stop.is_set()
    stopping_first = asyncio.shield(first_generation)
    runner.start()
    runner.start()
    assert runner.is_running and runner._task is first
    sleeping = asyncio.create_task(service.sleep_started.wait())
    release_first.set()
    try:
        done = await _first_completed(stopping_first, sleeping)
        assert stopping_first in done
        second = runner._task
        assert second is not None and second is not first
        # The stale waiter for the first generation must not clear its replacement.
        assert runner._task is second and runner.is_running
    finally:
        if not stopping_first.done():
            runner.kick()
            await stopping_first
        sleeping.cancel()
        await asyncio.gather(sleeping, return_exceptions=True)
    await second_started.wait()

    second_generation = runner.request_stop()
    assert second_generation is second and runner._stop.is_set()
    stopping_second = asyncio.shield(second_generation)
    runner.start()
    assert runner.is_running
    final_generation = runner.request_stop()
    assert final_generation is second
    stopping_last = asyncio.shield(final_generation)
    assert not runner.is_running
    sleeping = asyncio.create_task(service.sleep_started.wait())
    release_second.set()
    drained = asyncio.gather(stopping_second, stopping_last)
    try:
        done = await _first_completed(drained, sleeping)
        assert drained in done
        assert runner._task is None
        assert runner.check_count == 2
    finally:
        if not drained.done():
            runner.kick()
            await drained
        sleeping.cancel()
        await asyncio.gather(sleeping, return_exceptions=True)


async def test_unexpected_sleep_failure_is_retrieved_logged_and_retires_runner() -> None:
    logged = asyncio.Event()

    class FailingSleepService(_LifecycleService):
        async def due(self) -> list:
            return []

        async def seconds_until_next(self) -> None:
            raise RuntimeError("sleep lookup failed")

    class RecordingLog:
        def __init__(self) -> None:
            self.errors: list[tuple[str, dict]] = []

        def error(self, event: str, **kwargs) -> None:
            self.errors.append((event, kwargs))
            logged.set()

        def exception(self, _event: str, **_kwargs) -> None:
            return None

    log = RecordingLog()
    runner = BackgroundRunner(
        FailingSleepService(),  # type: ignore[arg-type]
        notify=lambda _line: None,
        run_job=None,  # type: ignore[arg-type]
        turn_lock=asyncio.Lock(),
        log=log,
    )
    runner.start()
    owned = runner._task
    assert owned is not None

    await logged.wait()
    assert owned.done()
    assert runner._task is None and not runner.is_running
    assert log.errors == [("wake_loop_crashed", {"error_type": "RuntimeError"})]


# --- BackgroundRunner: reminders ---------------------------------------------


async def test_reminder_fires_notify_once_and_records_ok(tmp_path: Path) -> None:
    service, clock, store = await _service(tmp_path)
    task = await _schedule(service, kind="reminder", payload="stretch")
    rec = Recorder()

    async def _run_job(_t):  # never called for reminders
        raise AssertionError("reminders make no model call")

    runner = BackgroundRunner(
        service, notify=rec.notify, run_job=_run_job, turn_lock=asyncio.Lock()
    )
    clock.advance(hours=1, minutes=1)  # past the 09:00 first fire
    assert await runner.check_due() == 1
    assert len(rec.lines) == 1 and "stretch" in rec.lines[0]
    runs = await store.runs_for(task.id)
    assert [r.status for r in runs] == ["ok"]
    assert (await store.get(task.id)).next_run_at == "2026-07-06T10:00:00+00:00"  # advanced


async def test_background_notification_keeps_the_task_project_scope(tmp_path: Path) -> None:
    service, clock, _store = await _service(tmp_path)
    project_id = await ProjectStore(service.store.db).create(name="Project A")
    await _schedule(service, kind="reminder", payload="only project A", project_id=project_id)
    rec = Recorder()
    scoped: list[tuple[str, int | None]] = []

    def task_notify(line: str, task) -> None:
        scoped.append((line, task.project_id))

    runner = BackgroundRunner(
        service,
        notify=rec.notify,
        task_notify=task_notify,
        run_job=None,
        turn_lock=asyncio.Lock(),
    )
    clock.advance(hours=1, minutes=1)
    await runner.check_due()
    assert len(scoped) == 1 and "only project A" in scoped[0][0]
    assert scoped[0][1] == project_id


async def test_reminder_notify_precedes_recording(tmp_path: Path) -> None:
    # At-least-once: notify must happen before the run is recorded, so a crash in
    # between re-delivers (never drops). Force recording to fail and assert notify
    # already fired.
    service, clock, _ = await _service(tmp_path)
    await _schedule(service, kind="reminder", payload="drink water")
    rec = Recorder()

    async def _boom_begin(_due):
        raise RuntimeError("crash right after notifying")

    service.begin_run = _boom_begin  # type: ignore[assignment]
    runner = BackgroundRunner(service, notify=rec.notify, run_job=None, turn_lock=asyncio.Lock())
    clock.advance(hours=1, minutes=1)
    with pytest.raises(RuntimeError):
        await runner.check_due()
    assert rec.lines and "drink water" in rec.lines[0]  # delivered before the crash


async def test_reminder_beyond_grace_still_fires_annotated(tmp_path: Path) -> None:
    service, clock, _ = await _service(tmp_path, misfire_grace_seconds=3600)
    await _schedule(service, kind="reminder", payload="call mom")
    rec = Recorder()
    runner = BackgroundRunner(service, notify=rec.notify, run_job=None, turn_lock=asyncio.Lock())
    clock.advance(hours=5)  # well beyond grace
    await runner.check_due()
    assert "call mom" in rec.lines[0]
    assert "missed" in rec.lines[0]  # late beats silent, but flagged


# --- BackgroundRunner: jobs --------------------------------------------------


async def test_job_fires_run_job_and_records_outcome(tmp_path: Path) -> None:
    service, clock, store = await _service(tmp_path)
    task = await _schedule(service, kind="job", title="digest")
    # task_runs.session_id has a real FK; insert a session for the fake outcome to reference.
    await store.db.execute(
        "INSERT INTO sessions (id, created_at, updated_at, kind) VALUES (7, ?, ?, 'task')",
        (START.isoformat(), START.isoformat()),
    )
    await store.db.commit()
    rec = Recorder()

    async def _run_job(t):
        rec.job_calls += 1
        return JobOutcome(session_id=7, text="here is the digest", denied_count=0, cost_usd=0.02)

    runner = BackgroundRunner(
        service, notify=rec.notify, run_job=_run_job, turn_lock=asyncio.Lock()
    )
    clock.advance(hours=1, minutes=1)
    await runner.check_due()
    assert rec.job_calls == 1
    (run,) = await store.runs_for(task.id, limit=1)
    assert run.status == "ok"
    assert run.session_id == 7
    assert run.result_text == "here is the digest"
    assert run.cost_usd == 0.02
    assert any("digest" in line and "✓" in line for line in rec.lines)


async def test_job_exception_records_error_and_counts_failure(tmp_path: Path) -> None:
    service, clock, store = await _service(tmp_path)
    task = await _schedule(service, kind="job")
    rec = Recorder()

    async def _run_job(_t):
        raise RuntimeError("model API exploded")

    runner = BackgroundRunner(
        service, notify=rec.notify, run_job=_run_job, turn_lock=asyncio.Lock()
    )
    clock.advance(hours=1, minutes=1)
    await runner.check_due()  # must not propagate — a bad job can't kill the loop
    (run,) = await store.runs_for(task.id, limit=1)
    assert run.status == "error"
    assert "exploded" in run.error
    assert (await store.get(task.id)).consecutive_failures == 1
    assert any("✗" in line for line in rec.lines)


async def test_runner_terminal_failure_files_scoped_dead_letter(tmp_path: Path) -> None:
    service, clock, store = await _service(tmp_path, max_consecutive_failures=1)
    project_id = await ProjectStore(store.db, store.lock).create(name="Scoped")
    task = await _schedule(service, kind="job", project_id=project_id)
    rec = Recorder()

    async def _run_job(_t):
        raise RuntimeError("model API exploded")

    runner = BackgroundRunner(
        service, notify=rec.notify, run_job=_run_job, turn_lock=asyncio.Lock()
    )
    clock.advance(hours=1, minutes=1)
    await runner.check_due()

    assert service.attention is not None
    (alert,) = await service.attention.list(state="open", project_id=project_id)
    assert alert.source == "scheduler" and alert.source_ref == str(task.id)
    assert alert.payload["task_id"] == task.id


async def test_missed_job_records_missed_and_makes_no_model_call(tmp_path: Path) -> None:
    service, clock, store = await _service(tmp_path, misfire_grace_seconds=3600)
    task = await _schedule(service, kind="job", title="stale")
    rec = Recorder()

    async def _run_job(_t):
        rec.job_calls += 1
        return JobOutcome(session_id=1, text="should not happen")

    runner = BackgroundRunner(
        service, notify=rec.notify, run_job=_run_job, turn_lock=asyncio.Lock()
    )
    clock.advance(hours=5)  # beyond grace -> missed, not run
    await runner.check_due()
    assert rec.job_calls == 0  # the whole point: no unattended run of a stale job
    (run,) = await store.runs_for(task.id, limit=1)
    assert run.status == "missed"
    assert any("missed" in line for line in rec.lines)


async def test_held_turn_lock_delays_firing(tmp_path: Path) -> None:
    service, clock, store = await _service(tmp_path)
    await _schedule(service, kind="reminder", payload="ping")
    rec = Recorder()
    lock = asyncio.Lock()
    runner = BackgroundRunner(service, notify=rec.notify, run_job=None, turn_lock=lock)
    clock.advance(hours=1, minutes=1)

    await lock.acquire()  # simulate an interactive turn in progress
    check = asyncio.create_task(runner.check_due())
    await asyncio.sleep(0.05)
    assert rec.lines == []  # blocked on the lock — nothing fired yet
    lock.release()
    await check
    assert rec.lines and "ping" in rec.lines[0]  # fired once the turn released


# --- JobRunner: unattended execution -----------------------------------------


async def _job_runner(tmp_path: Path, client, policy: Policy | None = None) -> JobRunner:
    cfg = load_config(root=tmp_path, env_file=None)
    store = SessionStore(await connect(tmp_path / "sessions.db"))
    _OPEN_DBS.append(store.db)
    registry = ToolRegistry()
    registry.discover("jarvis.tools.builtin", ToolContext(config=cfg))
    executor = ToolExecutor(
        timeout=cfg.limits.tool_timeout_seconds, max_result_chars=cfg.limits.max_tool_result_chars
    )
    gate = PermissionGate(policy or Policy(), cfg.root)
    return JobRunner(
        session_store=store,
        client=client,
        registry=registry,
        executor=executor,
        gate=gate,
        config=cfg,
    )


async def _parkable_job_runner(
    tmp_path: Path,
    client,
    task_store: TaskStore,
    policy: Policy | None = None,
    projects: ProjectService | None = None,
) -> JobRunner:
    """A JobRunner with the explicitly shared store required for durable parking."""
    cfg = load_config(root=tmp_path, env_file=None)
    session_store = SessionStore(task_store.db, task_store.lock)
    registry = ToolRegistry()
    registry.discover("jarvis.tools.builtin", ToolContext(config=cfg))
    executor = ToolExecutor(
        timeout=cfg.limits.tool_timeout_seconds, max_result_chars=cfg.limits.max_tool_result_chars
    )
    return JobRunner(
        session_store=session_store,
        client=client,
        registry=registry,
        executor=executor,
        gate=PermissionGate(policy or Policy(), cfg.root),
        config=cfg,
        task_store=task_store,
        projects=projects,
    )


async def _a_job_task(tmp_path: Path):
    # A standalone task row to hand to JobRunner (its own tiny store).
    store = TaskStore(await connect(tmp_path / "tasks2.db"))
    _OPEN_DBS.append(store.db)
    tid = await store.add(
        kind="job",
        title="summarize notes",
        payload="Read notes.txt and report the key number.",
        schedule_kind="once",
        schedule_spec="2026-07-06T09:00:00",
        timezone="UTC",
        next_run_at="2026-07-06T09:00:00+00:00",
        created_by="user",
    )
    return await store.get(tid)


async def test_job_runs_in_task_session_with_envelope(tmp_path: Path) -> None:
    client = FakeClient([text_message("The key number is 42.")])
    runner = await _job_runner(tmp_path, client)
    task = await _a_job_task(tmp_path)

    outcome = await runner.run(task)

    assert outcome.error is None
    assert outcome.text == "The key number is 42."
    assert outcome.cost_usd is not None and outcome.cost_usd > 0
    # ran in a fresh kind='task' session (invisible to --resume / reflection)
    assert outcome.session_id is not None
    assert await runner.session_store.latest_session_id() is None  # no interactive session exists
    saved = await runner.session_store.load_messages(outcome.session_id)
    first = saved[0]["content"]
    assert "Scheduled task #" in first and "STORED instruction" in first
    assert "Read notes.txt" in first  # the payload, verbatim, inside the envelope


async def test_unattended_denial_flows_back_and_is_counted(tmp_path: Path) -> None:
    # write_file is ALLOW in policy (as if the user had persisted it interactively);
    # the UnattendedGate must demote it, the denial must reach the model as an
    # is_error result, and denied_count must reflect it.
    policy = Policy(tools={"write_file": Permission.ALLOW})
    client = FakeClient(
        [
            tool_use_message(
                [ToolCall(id="c1", name="write_file", input={"path": "out.txt", "content": "hi"})]
            ),
            text_message("I couldn't write unattended, so here's what I found instead."),
        ]
    )
    runner = await _job_runner(tmp_path, client, policy=policy)
    task = await _a_job_task(tmp_path)

    outcome = await runner.run(task)

    assert outcome.denied_count == 1  # the demoted write
    assert outcome.error is None  # the model adapted and ended cleanly
    saved = await runner.session_store.load_messages(outcome.session_id)
    tool_results = [
        b
        for m in saved
        if isinstance(m["content"], list)
        for b in m["content"]
        if isinstance(b, dict) and b.get("type") == "tool_result"
    ]
    assert tool_results and tool_results[0]["is_error"] is True  # model saw the denial
    assert not (tmp_path / "out.txt").exists()  # nothing was written
    assert not outcome.parked and not outcome.retry_safe


async def test_remote_operator_job_exposes_local_subset_and_parks_allowed_write(
    tmp_path: Path,
) -> None:
    service, clock, store = await _service(tmp_path)
    task = await _schedule(
        service,
        kind="job",
        title="remote repair",
        origin="remote_operator",
    )
    policy = Policy(tools={"write_file": Permission.ALLOW})
    client = FakeClient(
        [
            tool_use_message(
                [
                    ToolCall(
                        id="remote-write",
                        name="write_file",
                        input={"path": "remote.txt", "content": "approved only"},
                    )
                ]
            )
        ]
    )
    job_runner = await _parkable_job_runner(tmp_path, client, store, policy=policy)
    runner = BackgroundRunner(
        service,
        notify=Recorder().notify,
        run_job=job_runner.run,
        turn_lock=asyncio.Lock(),
    )

    clock.advance(hours=1, minutes=1)
    assert await runner.check_due() == 1

    expected = set(
        job_runner.config.connectors.telegram.remote_control.operator.allowed_tools
    ) & set(job_runner.registry.names())
    assert {spec["name"] for spec in client.calls[0]["tools"]} == expected
    (run,) = await store.runs_for(task.id)
    assert run.status == "running" and run.approval_state == "pending"
    assert run.continuation is not None and run.continuation.tool_id == "remote-write"
    assert not (tmp_path / "remote.txt").exists()


async def test_remote_operator_job_pins_project_context_and_session_scope(
    tmp_path: Path,
) -> None:
    service, _clock, store = await _service(tmp_path)
    project_store = ProjectStore(store.db, store.lock)
    repo = tmp_path / "frontend"
    repo.mkdir()
    project_id = await project_store.create(
        name="Frontend Repair",
        description="The user-approved frontend workspace.",
        repos=[str(repo)],
    )
    task = await _schedule(
        service,
        kind="job",
        title="inspect frontend",
        project_id=project_id,
        origin="remote_operator",
    )
    client = FakeClient([text_message("Inspection complete.")])
    job_runner = await _parkable_job_runner(
        tmp_path,
        client,
        store,
        projects=ProjectService(project_store),
    )

    outcome = await job_runner.run(task)

    assert outcome.error is None and outcome.session_id is not None
    system = client.calls[0]["system"]
    assert "Active project: Frontend Repair" in system
    assert str(repo) in system
    row = await (
        await store.db.execute(
            "SELECT kind, project_id FROM sessions WHERE id = ?", (outcome.session_id,)
        )
    ).fetchone()
    assert row == ("task", project_id)


async def test_job_ask_parks_exact_call_and_runner_does_not_complete_it(tmp_path: Path) -> None:
    service, clock, store = await _service(tmp_path)
    task = await _schedule(service, kind="job", title="write report")
    push = CountOnlyRouter()
    service.notification_router = push
    client = FakeClient(
        [
            tool_use_message(
                [
                    ToolCall(
                        id="toolu-safe-before-ask",
                        name="read_file",
                        input={"path": "notes.txt"},
                    ),
                    ToolCall(
                        id="toolu-park-1",
                        name="write_file",
                        input={"path": "report.txt", "content": "approved bytes"},
                    ),
                ]
            )
        ]
    )
    job_runner = await _parkable_job_runner(tmp_path, client, store)
    rec = Recorder()
    runner = BackgroundRunner(
        service, notify=rec.notify, run_job=job_runner.run, turn_lock=asyncio.Lock()
    )

    clock.advance(hours=1, minutes=1)
    assert await runner.check_due() == 1

    (run,) = await store.runs_for(task.id)
    assert run.status == "running"  # BackgroundRunner did not overwrite the parked run
    assert run.approval_state == "pending"
    assert run.continuation is not None
    assert (
        run.continuation.tool_id,
        run.continuation.tool_name,
        run.continuation.tool_input,
    ) == ("toolu-park-1", "write_file", {"path": "report.txt", "content": "approved bytes"})
    assert [call.tool_id for call in run.continuation.pending_calls] == [
        "toolu-safe-before-ask",
        "toolu-park-1",
    ]
    assert run.session_id is not None
    saved = await job_runner.session_store.load_messages(run.session_id)
    assert saved[-1]["role"] == "assistant"
    assert [block["id"] for block in saved[-1]["content"]] == [
        "toolu-safe-before-ask",
        "toolu-park-1",
    ]  # exact provider tool-use block order
    assert await store.earliest_next_run() is None
    assert await store.stale_runs() == []  # restart cannot auto-abort/replay an intentional park
    assert not (tmp_path / "report.txt").exists()
    assert any("waiting for your approval" in line for line in rec.lines)
    assert len(push.calls) == 1
    assert push.calls[0]["priority"] == "urgent"
    assert push.calls[0]["open_counts"] == {"approval": 1}
    assert "title" not in push.calls[0] and "tool_input" not in push.calls[0]


async def test_parked_push_releases_turn_lock_and_uses_pending_aggregate(tmp_path: Path) -> None:
    service, clock, _store = await _service(tmp_path)
    await _schedule(service, kind="job", title="await approval")
    router = BlockingCountOnlyRouter()
    service.notification_router = router

    async def _count_pending(*, project_id):
        assert project_id is None
        return 2

    service.store.pending_approval_count = _count_pending  # type: ignore[method-assign]

    async def _park(_task):
        return JobOutcome(session_id=None, text="", parked=True)

    runner = BackgroundRunner(
        service, notify=Recorder().notify, run_job=_park, turn_lock=asyncio.Lock()
    )
    clock.advance(hours=1, minutes=1)
    tick = asyncio.create_task(runner.check_due())
    await asyncio.wait_for(router.started.wait(), timeout=0.5)
    # The external notifier is blocked, but an interactive turn can acquire the shared lock.
    await asyncio.wait_for(runner.turn_lock.acquire(), timeout=0.1)
    runner.turn_lock.release()
    assert router.calls[0]["open_counts"] == {"approval": 2}
    router.release.set()
    assert await tick == 1


async def test_rejected_parked_job_skips_the_original_occurrence_without_running_tools(
    tmp_path: Path,
) -> None:
    service, clock, store = await _service(tmp_path)
    task = await _schedule(service, kind="job", title="do not write")
    client = FakeClient(
        [
            tool_use_message(
                [
                    ToolCall(
                        id="toolu-reject",
                        name="write_file",
                        input={"path": "never.txt", "content": "must not exist"},
                    )
                ]
            )
        ]
    )
    job_runner = await _parkable_job_runner(tmp_path, client, store)
    rec = Recorder()
    runner = BackgroundRunner(
        service,
        notify=rec.notify,
        run_job=job_runner.run,
        resume_job=job_runner.resume_parked,
        turn_lock=asyncio.Lock(),
    )

    clock.advance(hours=1, minutes=1)
    await runner.check_due()
    (parked,) = await store.runs_for(task.id)
    assert await runner.resume_parked(parked.id, "reject")

    run = await store.get_run(parked.id)
    assert run is not None
    assert (run.status, run.approval_state, run.denied_count) == ("ok", "rejected", 1)
    assert run.result_text is not None and "no tool executed" in run.result_text
    assert not (tmp_path / "never.txt").exists()
    assert len(client.calls) == 1  # rejection does not make another model request
    assert (await store.get(task.id)).next_run_at is not None  # original occurrence advanced once


async def test_two_ask_batch_keeps_first_exact_grant_until_second_approval(tmp_path: Path) -> None:
    service, clock, store = await _service(tmp_path)
    task = await _schedule(service, kind="job", title="two approved writes")
    client = FakeClient(
        [
            tool_use_message(
                [
                    ToolCall(
                        id="toolu-first",
                        name="write_file",
                        input={"path": "first.txt", "content": "first exact bytes"},
                    ),
                    ToolCall(
                        id="toolu-second",
                        name="write_file",
                        input={"path": "second.txt", "content": "second exact bytes"},
                    ),
                ]
            ),
            text_message("both approved writes completed"),
        ]
    )
    job_runner = await _parkable_job_runner(tmp_path, client, store)
    rec = Recorder()
    runner = BackgroundRunner(
        service,
        notify=rec.notify,
        run_job=job_runner.run,
        resume_job=job_runner.resume_parked,
        turn_lock=asyncio.Lock(),
    )

    clock.advance(hours=1, minutes=1)
    await runner.check_due()
    (first_park,) = await store.runs_for(task.id)
    assert first_park.continuation is not None
    assert first_park.continuation.tool_id == "toolu-first"

    assert await runner.resume_parked(first_park.id, "approve")
    second_park = await store.get_run(first_park.id)
    assert second_park is not None and second_park.continuation is not None
    assert second_park.approval_state == "pending"
    assert second_park.continuation.tool_id == "toolu-second"
    assert [call.tool_id for call in second_park.continuation.approved_calls] == ["toolu-first"]
    assert not (tmp_path / "first.txt").exists()
    assert not (tmp_path / "second.txt").exists()
    assert len(client.calls) == 1  # no model continuation while a sibling still asks

    assert await runner.resume_parked(first_park.id, "approve")
    completed = await store.get_run(first_park.id)
    assert completed is not None and completed.status == "ok"
    assert (tmp_path / "first.txt").read_text() == "first exact bytes"
    assert (tmp_path / "second.txt").read_text() == "second exact bytes"
    # The resumed model sees a valid, complete tool-result batch in provider order.
    result_ids = [
        block["tool_use_id"]
        for block in client.calls[-1]["messages"][-1]["content"]
        if block.get("type") == "tool_result"
    ]
    assert result_ids == ["toolu-first", "toolu-second"]


async def test_non_end_turn_stop_is_reported_as_failure(tmp_path: Path) -> None:
    # A run that exhausts the iteration budget is a failure to report, not silence.
    cfg_client = FakeClient(
        [tool_use_message([ToolCall(id="c1", name="read_file", input={"path": "x"})])]
    )
    runner = await _job_runner(tmp_path, cfg_client)
    runner.config.limits.max_iterations = 1  # force the guard after one iteration
    task = await _a_job_task(tmp_path)

    outcome = await runner.run(task)
    assert outcome.error is not None
    assert "max_iterations" in outcome.error
