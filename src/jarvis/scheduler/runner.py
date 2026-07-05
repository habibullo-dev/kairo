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
from jarvis.scheduler.store import Task

#: Never sleep longer than this between due-checks even if the next task is far off:
#: asyncio timers don't advance through system suspend, so a laptop that slept past
#: a fire time still catches up within this bound of waking.
_MIN_SLEEP = 0.05


@dataclass
class JobOutcome:
    """What running one job produced — returned by the injected ``run_job``."""

    session_id: int | None
    text: str
    denied_count: int = 0
    error: str | None = None
    cost_usd: float | None = None


# Runs one job task to completion and returns its outcome (built in the CLI layer).
RunJob = Callable[[Task], Awaitable[JobOutcome]]
# Emits one user-facing notification line.
Notify = Callable[[str], None]


class BackgroundRunner:
    def __init__(
        self,
        service: TaskService,
        *,
        notify: Notify,
        run_job: RunJob,
        turn_lock: asyncio.Lock,
        log=None,
    ) -> None:
        self.service = service
        self.notify = notify
        self.run_job = run_job
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
        self.notify(f"⏰ reminder #{task.id}: {task.payload}{suffix}")
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
            self.notify(f'✗ job #{task.id} "{task.title}" failed: {detail}')
            return
        finally:
            self.in_flight = None

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
        )
        self._notify_job_done(task, outcome, ok)

    async def _fire_missed(self, due: Due) -> None:
        task = due.task
        await self.service.record_missed(due)
        self.notify(
            f'⚠ job #{task.id} "{task.title}" was due {self._local(due)} but the '
            "assistant wasn't running — recorded as missed, not run (see `tasks`)"
        )

    def _notify_job_done(self, task: Task, outcome: JobOutcome, ok: bool) -> None:
        cost = f" · ${outcome.cost_usd:.4f}" if outcome.cost_usd is not None else ""
        denied = f" · {outcome.denied_count} denied" if outcome.denied_count else ""
        if not ok:
            self.notify(f'✗ job #{task.id} "{task.title}": {outcome.error}{cost}{denied}')
            return
        head = _first_lines(outcome.text, 8)
        more = "\n  (see `tasks` for the full result)" if _has_more(outcome.text, 8) else ""
        self.notify(f'✓ job #{task.id} "{task.title}"{cost}{denied}:\n{head}{more}')

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
