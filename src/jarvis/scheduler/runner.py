"""BackgroundRunner: the asyncio wake loop that fires due tasks.

This is the concept the phase teaches — an agent that acts without being prompted —
and it is deliberately small. It owns *when* things fire; it does not know *how* a
job runs. Job execution needs the AgentLoop (core), so it's injected as an opaque
``run_job`` callback built one layer up (in the CLI, where core + services + the
permission gate are composed). The runner itself depends only on the TaskService,
which keeps this file free of core imports and the wake loop trivially testable.

Two firing rules carry weight:

* **The turn lock serializes everything that talks to the terminal.** A fire (even
  a millisecond reminder) is taken under the same lock the interactive turn holds,
  so a background notification can never interleave with a half-streamed response.
* **Reminders deliver at-least-once**: notify *before* recording the run, so a crash
  in between re-delivers on next startup (a harmless duplicate) rather than dropping
  it. Jobs are the opposite — the ``running`` row is opened *before* the work, so a
  crash leaves a detectable orphan the startup sweep aborts (a job's side effects
  may have completed, so it must never be silently re-run).
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from jarvis.observability import get_logger
from jarvis.scheduler.service import Due, TaskService
from jarvis.scheduler.store import ParkedContinuation, Task

#: Never sleep longer than this between due-checks even if the next task is far off:
#: asyncio timers don't advance through system suspend, so a laptop that slept past
#: a fire time still catches up within this bound of waking.
_MIN_SLEEP = 0.05


@dataclass
class JobOutcome:
    """What running one job produced — returned by the injected ``run_job``.

    ``parked`` is a non-terminal, no-execution outcome.  The runner must leave its pre-opened
    task-run row alone because the job runner has atomically converted it into a durable approval
    continuation.  ``retry_safe`` is positive evidence that a normal failed run never started a
    tool and never encountered a denial/park, so only then may scheduler policy schedule a retry.
    """

    session_id: int | None
    text: str
    denied_count: int = 0
    error: str | None = None
    cost_usd: float | None = None
    retry_safe: bool = False
    parked: bool = False


# Runs one job task to completion and returns its outcome (built in the CLI layer).
RunJob = Callable[[Task], Awaitable[JobOutcome]]
# Resume work is intentionally separate from a fresh task run: it receives the one claimed
# continuation and must never recreate an envelope/model request for the original occurrence.
ResumeJob = Callable[[Task, int, int, ParkedContinuation], Awaitable[JobOutcome]]
# Emits one user-facing notification line.
Notify = Callable[[str], None]
TaskNotify = Callable[[str, Task], None]


class BackgroundRunner:
    def __init__(
        self,
        service: TaskService,
        *,
        notify: Notify,
        task_notify: TaskNotify | None = None,
        run_job: RunJob,
        turn_lock: asyncio.Lock,
        resume_job: ResumeJob | None = None,
        run_digest: RunJob | None = None,
        log=None,
    ) -> None:
        self.service = service
        self.notify = notify
        self.task_notify = task_notify
        self.run_job = run_job
        self.resume_job = resume_job
        self.run_digest = run_digest
        self.turn_lock = turn_lock
        self.log = log or get_logger("jarvis.scheduler")
        self._wake = asyncio.Event()
        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None
        self.in_flight: str | None = None  # title of a job currently running, for shutdown UX

    # --- the testable core ---------------------------------------------------

    async def check_due(self) -> int:
        """Fire everything currently due; returns how many tasks were actioned.

        Each fire is taken under the turn lock (released between tasks so an
        interactive turn can interleave). No sleeping here — this is pure logic,
        driven by the service's clock."""
        handled = 0
        for due in await self.service.due():
            # A digest does network + a model call, so it must NOT hold the turn lock for the
            # duration (a Google 429 backoff would freeze the UI). It manages its own brief
            # lock windows around persist + notify (Phase 9, D4).
            if due.action != "missed" and due.task.kind == "digest":
                await self._fire_digest(due)
                handled += 1
                continue
            async with self.turn_lock:
                if due.action == "missed":
                    await self._fire_missed(due)
                elif due.task.kind == "reminder":
                    await self._fire_reminder(due)
                else:
                    await self._fire_job(due)
            handled += 1
        return handled

    async def _fire_reminder(self, due: Due) -> None:
        task = due.task
        late = due.action == "fire_late"
        suffix = f" (missed — was due {self._local(due)})" if late else ""
        # At-least-once: deliver first, then record. A crash before the record
        # re-delivers next startup (duplicate, not lost).
        self._notify(f"⏰ reminder #{task.id}: {task.payload}{suffix}", task)
        run_id = await self.service.begin_run(due)
        await self.service.complete_run(due, run_id, ok=True, result_text=f"delivered{suffix}")

    async def _fire_job(self, due: Due) -> None:
        task = due.task
        # Open the running row *before* the work: a crash leaves an orphan the
        # startup sweep aborts, never re-running possibly-completed side effects.
        run_id = await self.service.begin_run(due)
        self.in_flight = task.title
        try:
            outcome = await self.run_job(task)
        except Exception as exc:  # a job blowing up must not kill the wake loop
            self.log.exception("job_crashed", task_id=task.id)
            detail = f"{type(exc).__name__}: {exc}"
            await self.service.complete_run(due, run_id, ok=False, error=detail)
            self._notify(f'✗ job #{task.id} "{task.title}" failed: {detail}', task)
            return
        finally:
            self.in_flight = None

        if outcome.parked:
            # JobRunner already atomically persisted the task transcript + exact pending call
            # against ``run_id``.  Completing it here would erase the parked state and could
            # accidentally advance the task.  No action is resumed automatically after restart.
            self._notify(
                f'⏸ job #{task.id} "{task.title}" is waiting for your approval', task
            )
            return

        ok = outcome.error is None
        await self.service.complete_run(
            due,
            run_id,
            ok=ok,
            session_id=outcome.session_id,
            result_text=outcome.text or outcome.error,
            denied_count=outcome.denied_count,
            error=outcome.error,
            cost_usd=outcome.cost_usd,
            retry_safe=outcome.retry_safe,
        )
        self._notify_job_done(task, outcome, ok)

    async def resume_parked(self, run_id: int, resolution: str) -> bool:
        """Apply one explicit owner resolution to a durable unattended pause.

        Returns ``True`` only after the resolution was consumed and either terminally recorded or
        atomically re-parked.  A missing resume worker, stale nonce/row, or malformed request
        leaves the durable pending row untouched so a host must not claim success.  Once claimed,
        any ordinary preflight/runtime failure is terminally recorded with retries disabled;
        process death remains inert in ``approved`` state and is never auto-replayed.
        """
        if resolution not in {"approve", "reject"} or self.resume_job is None:
            return False
        async with self.turn_lock:
            try:
                run = await self.service.store.get_run(run_id)
            except ValueError:
                # A corrupt continuation cannot be treated as the owner's original request.
                # Do not consume its nonce or run a tool; the caller keeps the pending approval.
                return False
            if (
                run is None
                or run.status != "running"
                or run.approval_state != "pending"
                or run.continuation is None
                or run.session_id is None
            ):
                return False
            task = await self.service.store.get(run.task_id)
            if task is None or task.status != "active":
                return False
            claim = await self.service.store.claim_parked_approval(
                run_id, resolution=resolution  # type: ignore[arg-type]
            )
            if claim is None:
                return False
            due = Due(task=task, action="fire", scheduled_for=run.scheduled_for)
            if claim.resolution == "reject":
                # A reject is a completed owner decision, not a retryable model failure.  The
                # original tool batch never starts and the normal schedule advances once.
                await self.service.complete_run(
                    due,
                    run_id,
                    ok=True,
                    session_id=run.session_id,
                    result_text="Owner rejected the parked unattended action; no tool executed.",
                    denied_count=1,
                )
                self._notify(
                    f'⊘ job #{task.id} "{task.title}" approval rejected; no tool executed', task
                )
                return True

            continuation = claim.continuation
            if continuation is None:  # defensive: approved claims are the only executable form
                await self.service.complete_run(
                    due,
                    run_id,
                    ok=False,
                    error="parked approval claim had no executable continuation",
                    retry_safe=False,
                )
                return True
            self.in_flight = task.title
            try:
                outcome = await self.resume_job(task, run_id, run.session_id, continuation)
            except Exception as exc:
                # The claim is already one-time.  Closing this occurrence prevents an approved
                # but unresumable record from trapping the task forever or being retried later.
                self.log.exception("parked_job_resume_failed", task_id=task.id, run_id=run_id)
                detail = f"parked resume failed: {type(exc).__name__}: {exc}"
                await self.service.complete_run(
                    due, run_id, ok=False, error=detail, retry_safe=False
                )
                self._notify(f'✗ job #{task.id} "{task.title}" could not resume: {detail}', task)
                return True
            finally:
                self.in_flight = None

            if outcome.parked:
                # The resume worker persisted a fresh exact ASK in the same transaction as its
                # transcript.  Do not complete or advance the original scheduled occurrence.
                self._notify(
                    f'⏸ job #{task.id} "{task.title}" is waiting for your approval', task
                )
                return True
            ok = outcome.error is None
            await self.service.complete_run(
                due,
                run_id,
                ok=ok,
                session_id=outcome.session_id,
                result_text=outcome.text or outcome.error,
                denied_count=outcome.denied_count,
                error=outcome.error,
                cost_usd=outcome.cost_usd,
                retry_safe=outcome.retry_safe,
            )
            self._notify_job_done(task, outcome, ok)
            return True

    async def _fire_digest(self, due: Due) -> None:
        task = due.task
        # Job semantics: open the running row before the work (crash ⇒ visible orphan, swept
        # to aborted, never a silent re-run of egress). The lock is held only for the DB write.
        async with self.turn_lock:
            run_id = await self.service.begin_run(due)
        if self.run_digest is None:  # not composed (e.g. no utility client) — record, don't crash
            async with self.turn_lock:
                await self.service.complete_run(
                    due, run_id, ok=False, error="digest runner not configured"
                )
            return
        self.in_flight = task.title
        try:
            outcome = await self.run_digest(task)  # network + model + UI/notifier, NO turn lock
        except Exception as exc:
            self.log.exception("digest_crashed", task_id=task.id)
            detail = f"{type(exc).__name__}: {exc}"
            async with self.turn_lock:
                await self.service.complete_run(due, run_id, ok=False, error=detail)
                self._notify(f"✗ digest #{task.id} failed: {detail}", task)
            return
        finally:
            self.in_flight = None
        ok = outcome.error is None
        async with self.turn_lock:
            await self.service.complete_run(
                due,
                run_id,
                ok=ok,
                session_id=outcome.session_id,
                result_text=outcome.text or outcome.error,
                denied_count=outcome.denied_count,
                error=outcome.error,
                cost_usd=outcome.cost_usd,
            )
            self._notify(f"✓ daily digest ready{'' if ok else ' (with errors)'}", task)

    async def _fire_missed(self, due: Due) -> None:
        task = due.task
        await self.service.record_missed(due)
        self._notify(
            f'⚠ job #{task.id} "{task.title}" was due {self._local(due)} but the '
            "assistant wasn't running — recorded as missed, not run (see `tasks`)",
            task,
        )

    def _notify_job_done(self, task: Task, outcome: JobOutcome, ok: bool) -> None:
        cost = f" · ${outcome.cost_usd:.4f}" if outcome.cost_usd is not None else ""
        denied = f" · {outcome.denied_count} denied" if outcome.denied_count else ""
        if not ok:
            self._notify(f'✗ job #{task.id} "{task.title}": {outcome.error}{cost}{denied}', task)
            return
        head = _first_lines(outcome.text, 8)
        more = "\n  (see `tasks` for the full result)" if _has_more(outcome.text, 8) else ""
        self._notify(f'✓ job #{task.id} "{task.title}"{cost}{denied}:\n{head}{more}', task)

    def _notify(self, line: str, task: Task) -> None:
        self.notify(line)
        if self.task_notify is not None:
            self.task_notify(line, task)

    def _local(self, due: Due) -> str:
        return due.scheduled_for  # UTC ISO; describe()/`tasks` render local time

    # --- the thin wake loop (one smoke test; logic lives in check_due) --------

    def start(self) -> None:
        self._stop.clear()
        self._task = asyncio.create_task(self._loop())

    def kick(self) -> None:
        """Wake the loop now (e.g. a task was just scheduled)."""
        self._wake.set()

    async def stop(self) -> None:
        self._stop.set()
        self.kick()
        if self._task is not None:
            await self._task
            self._task = None

    @property
    def is_running(self) -> bool:
        """True while the wake loop is active (started, not stopped). Read-only status for
        the UI's runner pane / emergency-stop toggle (Phase 8) — no behavior change."""
        return self._task is not None and not self._task.done()

    async def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                await self.check_due()
            except Exception:  # a bad tick must never kill the loop
                self.log.exception("wake_tick_failed")
            await self._sleep_until_next()

    async def _sleep_until_next(self) -> None:
        secs = await self.service.seconds_until_next()
        cap = self.service.config.wake_cap_seconds
        delay = cap if secs is None else min(secs, cap)
        self._wake.clear()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._wake.wait(), timeout=max(delay, _MIN_SLEEP))


def _first_lines(text: str, n: int) -> str:
    lines = (text or "").splitlines()
    return "\n".join("  " + line for line in lines[:n])


def _has_more(text: str, n: int) -> bool:
    return len((text or "").splitlines()) > n
