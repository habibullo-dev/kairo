"""TaskService: scheduling semantics over the TaskStore.

The store is mechanism; this is policy — and all of it is driven by an injected
``now`` clock so every lifecycle rule unit-tests without sleeping:

* **Scheduling** validates specs, rejects past ``once`` times (with the current
  time in the error so the model can self-correct a timezone slip — it has no
  reliable clock otherwise), and computes the first fire.
* **Due classification** (D5 in docs/PLAN-3-tasks.md): within the misfire grace
  window everything fires; beyond it a reminder still fires late (late beats
  silent) while a job is recorded ``missed`` — one row per gap, never one per
  skipped cron slot (inherent: a task has a single ``next_run_at``).
* **Advancement** computes the next fire from the *scheduled* time it just
  serviced (no interval drift), retires ``once`` tasks, and applies the
  consecutive-failure cap so a broken recurring job can't burn a model call per
  interval forever.
* **The startup sweep** closes crash-orphaned ``running`` rows as ``aborted`` and
  advances their task *past* the interrupted occurrence — a half-run is never
  silently retried; its side effects may have completed before the crash.
"""

from __future__ import annotations

import datetime as _dt
from collections.abc import Callable
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from tzlocal import get_localzone_name

from jarvis.config import SchedulerConfig
from jarvis.scheduler.store import Task, TaskAdvance, TaskStore
from jarvis.scheduler.triggers import compute_next, validate

#: Stored per-run result text is bounded (the full transcript lives in the run's
#: session anyway); generous because the result is the product of a job.
MAX_RESULT_CHARS = 10_000

#: A once-time this far in the past still runs immediately instead of being
#: rejected — scheduling "in one minute" must not lose a race with the clock.
PAST_TOLERANCE = _dt.timedelta(minutes=2)


def utc_now() -> _dt.datetime:
    return _dt.datetime.now(_dt.UTC)


def _iso(moment: _dt.datetime) -> str:
    return moment.astimezone(_dt.UTC).isoformat()


def _parse(iso: str) -> _dt.datetime:
    return _dt.datetime.fromisoformat(iso)


class ScheduleError(ValueError):
    """A schedule the service refuses; the message is written for the model."""


@dataclass(frozen=True)
class Due:
    """One due task plus what to do about it."""

    task: Task
    action: str  # 'fire' | 'fire_late' | 'missed'
    scheduled_for: str  # the fire time being serviced (UTC ISO)


class TaskService:
    def __init__(
        self,
        store: TaskStore,
        config: SchedulerConfig,
        *,
        now: Callable[[], _dt.datetime] = utc_now,
    ) -> None:
        self.store = store
        self.config = config
        self.now = now
        # Set by the REPL (and by the runner during a job) so tool-created tasks
        # carry provenance — "why is Jarvis doing THAT at 3am?" must have an answer.
        self.bound_session_id: int | None = None

    # --- scheduling ----------------------------------------------------------

    async def schedule(
        self,
        *,
        kind: str,
        title: str,
        payload: str,
        schedule_kind: str,
        schedule_spec: str,
        created_by: str,
        timezone: str | None = None,
    ) -> Task:
        """Validate and insert a task; returns it. Raises :class:`ScheduleError`
        with a model-readable message when the spec is unusable."""
        tz = timezone or get_localzone_name()
        problem = validate(schedule_kind, schedule_spec, tz)
        if problem is not None:
            raise ScheduleError(problem)

        current = self.now()
        first = compute_next(schedule_kind, schedule_spec, tz, after=current)
        if first is None:  # only possible for a past 'once'
            instant = _parse_once_in_zone(schedule_spec, tz)
            if current - instant <= PAST_TOLERANCE:
                first = current  # "in a minute" must not lose a race with the clock
            else:
                local = current.astimezone(ZoneInfo(tz))
                raise ScheduleError(
                    f"{schedule_spec!r} is in the past — it is currently "
                    f"{local:%Y-%m-%d %H:%M} in {tz}. Give a future time."
                )

        task_id = await self.store.add(
            kind=kind,
            title=title,
            payload=payload,
            schedule_kind=schedule_kind,
            schedule_spec=schedule_spec,
            timezone=tz,
            next_run_at=_iso(first),
            created_by=created_by,
            source_session_id=self.bound_session_id,
        )
        task = await self.store.get(task_id)
        assert task is not None
        return task

    async def cancel(self, task_id: int) -> Task | None:
        """Cancel an active task; returns the (now-cancelled) task, or None if it
        wasn't active (already terminal, or unknown)."""
        if not await self.store.cancel(task_id):
            return None
        return await self.store.get(task_id)

    async def seconds_until_next(self) -> float | None:
        """Seconds until the soonest active task fires (0 if already due), or None
        if nothing is scheduled — the wake loop's sleep bound."""
        iso = await self.store.earliest_next_run()
        if iso is None:
            return None
        return max(0.0, (_parse(iso) - self.now()).total_seconds())

    # --- firing --------------------------------------------------------------

    async def due(self) -> list[Due]:
        """Classify everything currently due (see module docstring / plan D5)."""
        current = self.now()
        grace = _dt.timedelta(seconds=self.config.misfire_grace_seconds)
        out: list[Due] = []
        for task in await self.store.due(_iso(current)):
            assert task.next_run_at is not None
            late = current - _parse(task.next_run_at) > grace
            if not late:
                action = "fire"
            elif task.kind == "reminder":
                action = "fire_late"  # late beats silent
            else:
                action = "missed"  # a stale job must not surprise-run hours later
            out.append(Due(task=task, action=action, scheduled_for=task.next_run_at))
        return out

    async def begin_run(self, due: Due) -> int:
        """Open the run row for a firing task."""
        return await self.store.start_run(due.task.id, scheduled_for=due.scheduled_for)

    async def complete_run(
        self,
        due: Due,
        run_id: int,
        *,
        ok: bool,
        session_id: int | None = None,
        result_text: str | None = None,
        denied_count: int = 0,
        error: str | None = None,
        cost_usd: float | None = None,
    ) -> Task:
        """Close a run and advance its task atomically. Returns the updated task."""
        task = due.task
        # Advance from the *scheduled* time, not completion — no interval drift.
        next_fire = compute_next(
            task.schedule_kind,
            task.schedule_spec,
            task.timezone,
            after=_parse(due.scheduled_for),
        )
        if ok:
            advance = _advance_ok(task, next_fire)
        else:
            advance = _advance_error(task, next_fire, error, self.config)
        if result_text is not None and len(result_text) > MAX_RESULT_CHARS:
            result_text = result_text[:MAX_RESULT_CHARS] + " …[truncated]"
        await self.store.finish_run(
            run_id,
            "ok" if ok else "error",
            session_id=session_id,
            result_text=result_text,
            denied_count=denied_count,
            error=error,
            cost_usd=cost_usd,
            advance=advance,
        )
        updated = await self.store.get(task.id)
        assert updated is not None
        return updated

    async def record_missed(self, due: Due) -> Task:
        """One ``missed`` run row for the whole gap; recurring tasks resume from
        now (never looping over skipped occurrences), once-tasks go terminal."""
        task = due.task
        if task.schedule_kind == "once":
            advance = TaskAdvance(
                task_id=task.id,
                next_run_at=None,
                status="missed",
                consecutive_failures=task.consecutive_failures,
                last_error=task.last_error,
            )
        else:
            next_fire = compute_next(
                task.schedule_kind, task.schedule_spec, task.timezone, after=self.now()
            )
            advance = TaskAdvance(
                task_id=task.id,
                next_run_at=_iso(next_fire) if next_fire else None,
                status="active",
                consecutive_failures=task.consecutive_failures,
                last_error=task.last_error,
            )
        await self.store.record_missed(task.id, due.scheduled_for, advance)
        updated = await self.store.get(task.id)
        assert updated is not None
        return updated

    async def sweep_stale_runs(self) -> list[str]:
        """Close crash-orphaned runs as ``aborted``; never re-run them. Returns
        human-readable notes for the startup notification."""
        notes: list[str] = []
        for run in await self.store.stale_runs():
            task = await self.store.get(run.task_id)
            advance: TaskAdvance | None = None
            if task is not None and task.status == "active":
                if task.schedule_kind == "once":
                    advance = TaskAdvance(
                        task_id=task.id,
                        next_run_at=None,
                        status="failed",
                        consecutive_failures=task.consecutive_failures + 1,
                        last_error="interrupted: process died mid-run",
                    )
                else:
                    next_fire = compute_next(
                        task.schedule_kind, task.schedule_spec, task.timezone, after=self.now()
                    )
                    advance = TaskAdvance(
                        task_id=task.id,
                        next_run_at=_iso(next_fire) if next_fire else None,
                        status="active",
                        consecutive_failures=task.consecutive_failures + 1,
                        last_error="interrupted: process died mid-run",
                    )
            await self.store.finish_run(
                run.id,
                "aborted",
                error="interrupted: process died mid-run",
                advance=advance,
            )
            title = task.title if task else f"task {run.task_id}"
            notes.append(
                f'task #{run.task_id} "{title}": a run from {run.scheduled_for} was '
                "interrupted mid-execution and was NOT retried (its effects may have "
                "completed) — see `tasks` for details"
            )
        return notes

    # --- rendering -----------------------------------------------------------

    def describe(self, task: Task) -> str:
        """One human line: schedule in words + next fire in the task's own zone.
        Reused by tool results (so the model can confirm its time math) and the
        ``tasks`` REPL command."""
        schedule = _schedule_words(task)
        if task.next_run_at is None:
            when = f"status {task.status}"
        else:
            zone = ZoneInfo(task.timezone)
            local = _parse(task.next_run_at).astimezone(zone)
            delta = _human_delta(_parse(task.next_run_at) - self.now())
            when = f"next run {local:%Y-%m-%d %H:%M} {local:%Z} ({delta})"
        return f'{task.kind} #{task.id} "{task.title}" — {schedule}, {when}'


def _advance_ok(task: Task, next_fire: _dt.datetime | None) -> TaskAdvance:
    if next_fire is None:  # a 'once' that ran
        return TaskAdvance(task_id=task.id, next_run_at=None, status="done")
    return TaskAdvance(task_id=task.id, next_run_at=_iso(next_fire), status="active")


def _advance_error(
    task: Task, next_fire: _dt.datetime | None, error: str | None, config: SchedulerConfig
) -> TaskAdvance:
    failures = task.consecutive_failures + 1
    message = error or "unknown error"
    if next_fire is None or failures >= config.max_consecutive_failures:
        # once-tasks don't retry; recurring tasks stop at the cap — a broken job
        # must not silently burn a model call per interval forever.
        return TaskAdvance(
            task_id=task.id,
            next_run_at=None,
            status="failed",
            consecutive_failures=failures,
            last_error=message,
        )
    return TaskAdvance(
        task_id=task.id,
        next_run_at=_iso(next_fire),
        status="active",
        consecutive_failures=failures,
        last_error=message,
    )


def _parse_once_in_zone(spec: str, tz: str) -> _dt.datetime:
    parsed = _dt.datetime.fromisoformat(spec)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ZoneInfo(tz))
    return parsed


def _schedule_words(task: Task) -> str:
    if task.schedule_kind == "once":
        return f"once at {task.schedule_spec} ({task.timezone})"
    if task.schedule_kind == "cron":
        return f"cron '{task.schedule_spec}' ({task.timezone})"
    return f"every {_human_interval(int(task.schedule_spec))}"


def _human_interval(seconds: int) -> str:
    if seconds % 3600 == 0:
        hours = seconds // 3600
        return f"{hours} hour" + ("s" if hours != 1 else "")
    if seconds % 60 == 0:
        minutes = seconds // 60
        return f"{minutes} minute" + ("s" if minutes != 1 else "")
    return f"{seconds} seconds"


def _human_delta(delta: _dt.timedelta) -> str:
    seconds = int(delta.total_seconds())
    prefix, seconds = ("in ", seconds) if seconds >= 0 else ("overdue by ", -seconds)
    days, rest = divmod(seconds, 86_400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    if days:
        return f"{prefix}{days}d {hours}h"
    if hours:
        return f"{prefix}{hours}h {minutes}m"
    return f"{prefix}{max(minutes, 1)}m" if seconds >= 60 else f"{prefix}under a minute"
