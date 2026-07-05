"""TaskStore: SQLite persistence for tasks + their run history (schema v3).

Plain SQL like the other stores, and the same rules apply: it runs on the *same*
aiosqlite connection and shared write lock as SessionStore/MemoryStore (a second
connection to one file would deadlock; a second lock would let a task write land
inside another store's open transaction). Nothing is ever DELETEd — cancel is a
status, run history is audit.

The store is mechanism, not policy: it applies decisions the TaskService already
made (what the next fire time is, whether a failure trips the cap) atomically.
:class:`TaskAdvance` carries one such decision so ``finish_run`` can update the
run row and the task row in a single transaction — a crash between the two must
never leave a run recorded but the task un-advanced (double-run) or vice versa
(silently skipped occurrence).
"""

from __future__ import annotations

import asyncio
import datetime as _dt
from dataclasses import dataclass

import aiosqlite

from jarvis.persistence.db import transaction

_TASK_COLUMNS = (
    "id, kind, title, payload, schedule_kind, schedule_spec, timezone, next_run_at, "
    "status, created_by, source_session_id, consecutive_failures, last_run_at, "
    "last_error, created_at, updated_at"
)

_RUN_COLUMNS = (
    "id, task_id, scheduled_for, started_at, finished_at, status, session_id, "
    "result_text, denied_count, error, cost_usd, created_at"
)


def _now() -> str:
    return _dt.datetime.now(_dt.UTC).isoformat()


@dataclass(frozen=True)
class Task:
    """One row of ``tasks``. ``status`` is the task's *lifecycle*
    (active/done/cancelled/failed/missed); per-execution outcomes live on runs."""

    id: int
    kind: str  # 'reminder' | 'job'
    title: str
    payload: str
    schedule_kind: str  # 'once' | 'cron' | 'interval'
    schedule_spec: str
    timezone: str
    next_run_at: str | None  # UTC ISO; None iff not active
    status: str
    created_by: str  # 'user' | 'agent'
    source_session_id: int | None
    consecutive_failures: int
    last_run_at: str | None
    last_error: str | None
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class TaskRun:
    """One execution (or non-execution: missed/aborted) of a task."""

    id: int
    task_id: int
    scheduled_for: str  # the fire time this run serviced (UTC ISO)
    started_at: str | None
    finished_at: str | None
    status: str  # 'running' | 'ok' | 'error' | 'missed' | 'aborted'
    session_id: int | None
    result_text: str | None
    denied_count: int
    error: str | None
    cost_usd: float | None
    created_at: str


@dataclass(frozen=True)
class TaskAdvance:
    """A service-computed task update, applied atomically with a run outcome."""

    task_id: int
    next_run_at: str | None  # None when the task goes terminal (CHECK-enforced)
    status: str = "active"
    consecutive_failures: int = 0
    last_error: str | None = None


class TaskStore:
    def __init__(self, db: aiosqlite.Connection, lock: asyncio.Lock | None = None) -> None:
        self.db = db
        self.lock = lock or asyncio.Lock()

    # --- tasks ---------------------------------------------------------------

    async def add(
        self,
        *,
        kind: str,
        title: str,
        payload: str,
        schedule_kind: str,
        schedule_spec: str,
        timezone: str,
        next_run_at: str,
        created_by: str,
        source_session_id: int | None = None,
    ) -> int:
        """Insert an active task with its first fire time. Returns its id."""
        now = _now()
        async with self.lock:
            cursor = await self.db.execute(
                "INSERT INTO tasks (kind, title, payload, schedule_kind, schedule_spec, "
                "timezone, next_run_at, status, created_by, source_session_id, "
                "created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, ?)",
                (
                    kind,
                    title,
                    payload,
                    schedule_kind,
                    schedule_spec,
                    timezone,
                    next_run_at,
                    created_by,
                    source_session_id,
                    now,
                    now,
                ),
            )
            await self.db.commit()
        assert cursor.lastrowid is not None
        return cursor.lastrowid

    async def get(self, task_id: int) -> Task | None:
        cursor = await self.db.execute(
            f"SELECT {_TASK_COLUMNS} FROM tasks WHERE id = ?", (task_id,)
        )
        row = await cursor.fetchone()
        return _row_to_task(row) if row else None

    async def list(self, *, include_finished: bool = False) -> list[Task]:
        where = "" if include_finished else "WHERE status = 'active' "
        cursor = await self.db.execute(f"SELECT {_TASK_COLUMNS} FROM tasks {where}ORDER BY id")
        return [_row_to_task(r) for r in await cursor.fetchall()]

    async def earliest_next_run(self) -> str | None:
        """The soonest fire time among active tasks (UTC ISO), or None if none —
        lets the wake loop sleep exactly until the next task instead of polling."""
        cursor = await self.db.execute(
            "SELECT MIN(next_run_at) FROM tasks WHERE status = 'active' AND next_run_at IS NOT NULL"
        )
        row = await cursor.fetchone()
        return row[0] if row and row[0] else None

    async def due(self, now_iso: str) -> list[Task]:
        """Active tasks whose fire time has arrived and that aren't already running.

        The NOT EXISTS clause is the coalescing rule: a task with an unfinished run
        never fires again on top of itself (``max_instances=1`` by construction)."""
        cursor = await self.db.execute(
            f"SELECT {_TASK_COLUMNS} FROM tasks t "
            "WHERE t.status = 'active' AND t.next_run_at IS NOT NULL AND t.next_run_at <= ? "
            "AND NOT EXISTS (SELECT 1 FROM task_runs r "
            "                WHERE r.task_id = t.id AND r.status = 'running') "
            "ORDER BY t.next_run_at, t.id",
            (now_iso,),
        )
        return [_row_to_task(r) for r in await cursor.fetchall()]

    async def cancel(self, task_id: int) -> bool:
        """Flip an *active* task to ``cancelled`` (row kept — never DELETEd).
        Returns False if the task wasn't active (already terminal or unknown)."""
        async with self.lock:
            cursor = await self.db.execute(
                "UPDATE tasks SET status = 'cancelled', next_run_at = NULL, updated_at = ? "
                "WHERE id = ? AND status = 'active'",
                (_now(), task_id),
            )
            await self.db.commit()
        return cursor.rowcount > 0

    async def set_next_run(self, task_id: int, when: str | None) -> None:
        async with self.lock:
            await self.db.execute(
                "UPDATE tasks SET next_run_at = ?, updated_at = ? WHERE id = ?",
                (when, _now(), task_id),
            )
            await self.db.commit()

    # --- runs ----------------------------------------------------------------

    async def start_run(self, task_id: int, scheduled_for: str) -> int:
        """Open a ``running`` run row (and stamp the task's ``last_run_at``)."""
        now = _now()
        async with transaction(self.db, self.lock):
            cursor = await self.db.execute(
                "INSERT INTO task_runs (task_id, scheduled_for, started_at, status, "
                "created_at) VALUES (?, ?, ?, 'running', ?)",
                (task_id, scheduled_for, now, now),
            )
            await self.db.execute(
                "UPDATE tasks SET last_run_at = ?, updated_at = ? WHERE id = ?",
                (now, now, task_id),
            )
        assert cursor.lastrowid is not None
        return cursor.lastrowid

    async def finish_run(
        self,
        run_id: int,
        status: str,
        *,
        session_id: int | None = None,
        result_text: str | None = None,
        denied_count: int = 0,
        error: str | None = None,
        cost_usd: float | None = None,
        advance: TaskAdvance | None = None,
    ) -> None:
        """Close a run and (optionally) advance its task — one atomic transaction.

        A crash between "run recorded" and "task advanced" would either re-run a
        completed job (duplicate side effects) or silently skip an occurrence, so
        the two updates must not be separable."""
        now = _now()
        async with transaction(self.db, self.lock):
            await self.db.execute(
                "UPDATE task_runs SET finished_at = ?, status = ?, session_id = ?, "
                "result_text = ?, denied_count = ?, error = ?, cost_usd = ? WHERE id = ?",
                (now, status, session_id, result_text, denied_count, error, cost_usd, run_id),
            )
            if advance is not None:
                await self._apply_advance(advance, now)

    async def record_missed(self, task_id: int, scheduled_for: str, advance: TaskAdvance) -> int:
        """Record a run that never happened (fire time missed beyond grace) and
        advance the task, atomically. The row has no ``started_at`` — nothing ran."""
        now = _now()
        async with transaction(self.db, self.lock):
            cursor = await self.db.execute(
                "INSERT INTO task_runs (task_id, scheduled_for, finished_at, status, "
                "created_at) VALUES (?, ?, ?, 'missed', ?)",
                (task_id, scheduled_for, now, now),
            )
            await self._apply_advance(advance, now)
        assert cursor.lastrowid is not None
        return cursor.lastrowid

    async def runs_for(self, task_id: int, *, limit: int = 20) -> list[TaskRun]:
        cursor = await self.db.execute(
            f"SELECT {_RUN_COLUMNS} FROM task_runs WHERE task_id = ? ORDER BY id DESC LIMIT ?",
            (task_id, limit),
        )
        return [_row_to_run(r) for r in await cursor.fetchall()]

    async def stale_runs(self) -> list[TaskRun]:
        """Runs still ``running`` — after a crash these are orphans the service
        sweeps to ``aborted`` at startup (never silently re-run: their side effects
        may have completed before the process died)."""
        cursor = await self.db.execute(
            f"SELECT {_RUN_COLUMNS} FROM task_runs WHERE status = 'running' ORDER BY id"
        )
        return [_row_to_run(r) for r in await cursor.fetchall()]

    async def _apply_advance(self, advance: TaskAdvance, now: str) -> None:
        """Inside an open transaction only — no lock/commit here."""
        await self.db.execute(
            "UPDATE tasks SET next_run_at = ?, status = ?, consecutive_failures = ?, "
            "last_error = ?, updated_at = ? WHERE id = ?",
            (
                advance.next_run_at,
                advance.status,
                advance.consecutive_failures,
                advance.last_error,
                now,
                advance.task_id,
            ),
        )


def _row_to_task(row: tuple) -> Task:
    return Task(*row)


def _row_to_run(row: tuple) -> TaskRun:
    return TaskRun(*row)
