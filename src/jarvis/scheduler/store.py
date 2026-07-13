"""TaskStore: SQLite persistence for tasks + their run history (schema v3).

Plain SQL like the other stores, and the same rules apply: it runs on the *same*
aiosqlite connection and shared write lock as SessionStore/MemoryStore. A second
lock would let a task write land inside another store's open transaction. Nothing is ever
DELETEd — cancel is a status, run history is audit.

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
import hashlib
import json
from dataclasses import dataclass
from typing import Literal

import aiosqlite

from jarvis.persistence.db import transaction

_TASK_COLUMNS = (
    "id, kind, title, payload, schedule_kind, schedule_spec, timezone, next_run_at, "
    "status, created_by, source_session_id, consecutive_failures, last_run_at, "
    "last_error, created_at, updated_at, project_id"
)

#: Sentinel for "any project" (no scope filter) in list() — distinct from ``None`` (global
#: only, project_id IS NULL). Mirrors the memory/session/KB scope contract.
ANY_PROJECT: object = object()

_RUN_COLUMNS = (
    "id, task_id, scheduled_for, started_at, finished_at, status, session_id, "
    "result_text, denied_count, error, cost_usd, created_at, continuation_json, approval_state"
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
    project_id: int | None = None  # Phase 10: scope (None == global)


@dataclass(frozen=True)
class PendingToolCall:
    """One exact tool-use block held in an unfinished model batch."""

    tool_id: str
    tool_name: str
    tool_input: dict
    tool_input_hash: str

    @classmethod
    def from_call(cls, *, tool_id: str, tool_name: str, tool_input: dict) -> PendingToolCall:
        canonical = _canonical_input(tool_input)
        return cls(
            tool_id=tool_id,
            tool_name=tool_name,
            tool_input=json.loads(canonical),
            tool_input_hash=hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        )

    def verify(self) -> None:
        actual = hashlib.sha256(_canonical_input(self.tool_input).encode("utf-8")).hexdigest()
        if actual != self.tool_input_hash:
            raise ValueError("parked continuation input hash does not match its stored input")

    def to_public_dict(self) -> dict:
        self.verify()
        return {
            "tool_id": self.tool_id,
            "tool_name": self.tool_name,
            "tool_input": self.tool_input,
            "tool_input_hash": self.tool_input_hash,
        }

    @classmethod
    def from_public_dict(cls, value: object) -> PendingToolCall:
        if (
            not isinstance(value, dict)
            or not isinstance(value.get("tool_id"), str)
            or not isinstance(value.get("tool_name"), str)
            or not isinstance(value.get("tool_input"), dict)
            or not isinstance(value.get("tool_input_hash"), str)
        ):
            raise ValueError("invalid parked continuation")
        call = cls(
            tool_id=value["tool_id"],
            tool_name=value["tool_name"],
            tool_input=value["tool_input"],
            tool_input_hash=value["tool_input_hash"],
        )
        call.verify()
        return call


@dataclass(frozen=True)
class ParkedContinuation:
    """The full unfinished tool batch for a parked unattended task run.

    ``tool_input_hash`` is SHA-256 of canonical JSON (sorted keys, compact separators), not a
    security token.  It is an integrity tripwire: a resume host must reject a corrupt or altered
    input instead of executing something that was not the model call the human was asked about.
    ``pending_calls`` retains the complete provider batch, so a resume host can form valid tool
    results without inventing/removing tool-use blocks.  The human's resolution applies *only*
    to ``tool_id``; every other pending call must be re-gated in the resumed environment.
    """

    tool_id: str
    tool_name: str
    tool_input: dict
    tool_input_hash: str
    decision_reason: str
    pending_calls: tuple[PendingToolCall, ...]
    # Exact calls the owner has already approved while working through this one provider batch.
    # They are not policy grants: each remains bound to this parked run, its original id/name/input
    # and canonical hash.  A later ASK in the same batch must not make the owner re-approve an
    # earlier call that never reached execution because batch preflight stopped first.
    approved_calls: tuple[PendingToolCall, ...] = ()

    @classmethod
    def from_call(
        cls, *, tool_id: str, tool_name: str, tool_input: dict, decision_reason: str
    ) -> ParkedContinuation:
        return cls.from_batch(
            tool_id=tool_id,
            tool_name=tool_name,
            tool_input=tool_input,
            decision_reason=decision_reason,
            pending_calls=[{"id": tool_id, "name": tool_name, "input": tool_input}],
        )

    @classmethod
    def from_batch(
        cls,
        *,
        tool_id: str,
        tool_name: str,
        tool_input: dict,
        decision_reason: str,
        pending_calls: list[dict],
        approved_calls: list[dict] | None = None,
    ) -> ParkedContinuation:
        approved = PendingToolCall.from_call(
            tool_id=tool_id, tool_name=tool_name, tool_input=tool_input
        )
        calls: list[PendingToolCall] = []
        for call in pending_calls:
            if (
                not isinstance(call, dict)
                or not isinstance(call.get("id"), str)
                or not isinstance(call.get("name"), str)
                or not isinstance(call.get("input"), dict)
            ):
                raise ValueError("parked continuation must contain JSON tool calls")
            calls.append(
                PendingToolCall.from_call(
                    tool_id=call["id"], tool_name=call["name"], tool_input=call["input"]
                )
            )
        if len({call.tool_id for call in calls}) != len(calls):
            raise ValueError("parked continuation must contain unique JSON tool calls")
        if approved not in calls:
            raise ValueError("parked approval call is not present in its pending batch")
        approved_prior: list[PendingToolCall] = []
        for call in approved_calls or []:
            if not isinstance(call, dict):
                raise ValueError("parked continuation must contain JSON approved calls")
            # ``to_public_dict`` is deliberately the only accepted shape here.  It makes the
            # exact canonical hash part of an accumulated one-time approval, rather than a
            # mutable id-only marker.
            approved_prior.append(PendingToolCall.from_public_dict(call))
        if len({call.tool_id for call in approved_prior}) != len(approved_prior):
            raise ValueError("parked continuation must contain unique approved calls")
        if approved in approved_prior:
            raise ValueError("current parked approval must not already be approved")
        if any(call not in calls for call in approved_prior):
            raise ValueError("parked approved call is not present in its pending batch")
        return cls(
            tool_id=approved.tool_id,
            tool_name=approved.tool_name,
            tool_input=approved.tool_input,
            tool_input_hash=approved.tool_input_hash,
            decision_reason=decision_reason,
            pending_calls=tuple(calls),
            approved_calls=tuple(approved_prior),
        )

    def verify(self) -> None:
        actual = hashlib.sha256(_canonical_input(self.tool_input).encode("utf-8")).hexdigest()
        if actual != self.tool_input_hash:
            raise ValueError("parked continuation input hash does not match its stored input")
        if not self.pending_calls or len({call.tool_id for call in self.pending_calls}) != len(
            self.pending_calls
        ):
            raise ValueError("parked continuation must contain unique pending tool calls")
        for call in self.pending_calls:
            call.verify()
        if len({call.tool_id for call in self.approved_calls}) != len(self.approved_calls):
            raise ValueError("parked continuation must contain unique approved calls")
        for call in self.approved_calls:
            call.verify()
            if call not in self.pending_calls:
                raise ValueError("parked approved call is not present in its pending batch")
        if not any(
            call.tool_id == self.tool_id
            and call.tool_name == self.tool_name
            and call.tool_input == self.tool_input
            and call.tool_input_hash == self.tool_input_hash
            for call in self.pending_calls
        ):
            raise ValueError("parked approval call is not present in its pending batch")
        if any(call.tool_id == self.tool_id for call in self.approved_calls):
            raise ValueError("current parked approval must not already be approved")

    def to_json(self) -> str:
        self.verify()
        return json.dumps(
            {
                "version": 3,
                "tool_id": self.tool_id,
                "tool_name": self.tool_name,
                "tool_input": self.tool_input,
                "tool_input_hash": self.tool_input_hash,
                "decision_reason": self.decision_reason,
                "pending_calls": [call.to_public_dict() for call in self.pending_calls],
                "approved_calls": [call.to_public_dict() for call in self.approved_calls],
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    @classmethod
    def from_json(cls, raw: str) -> ParkedContinuation:
        try:
            value = json.loads(raw)
            if (
                not isinstance(value, dict)
                or value.get("version") not in {1, 2, 3}
                or not isinstance(value.get("tool_id"), str)
                or not isinstance(value.get("tool_name"), str)
                or not isinstance(value.get("tool_input"), dict)
                or not isinstance(value.get("tool_input_hash"), str)
                or not isinstance(value.get("decision_reason"), str)
            ):
                raise ValueError("invalid parked continuation")
            pending_calls = (
                [
                    {
                        "tool_id": value["tool_id"],
                        "tool_name": value["tool_name"],
                        "tool_input": value["tool_input"],
                        "tool_input_hash": value["tool_input_hash"],
                    }
                ]
                if value["version"] == 1
                else value.get("pending_calls")
            )
            if not isinstance(pending_calls, list):
                raise ValueError("invalid parked continuation")
            approved_calls = [] if value["version"] in {1, 2} else value.get("approved_calls")
            if not isinstance(approved_calls, list):
                raise ValueError("invalid parked continuation")
            continuation = cls(
                tool_id=value["tool_id"],
                tool_name=value["tool_name"],
                tool_input=value["tool_input"],
                tool_input_hash=value["tool_input_hash"],
                decision_reason=value["decision_reason"],
                pending_calls=tuple(
                    PendingToolCall.from_public_dict(call) for call in pending_calls
                ),
                approved_calls=tuple(
                    PendingToolCall.from_public_dict(call) for call in approved_calls
                ),
            )
            continuation.verify()
            return continuation
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError("invalid parked continuation") from exc


@dataclass(frozen=True)
class ParkedApprovalClaim:
    """The one-time result of claiming a parked approval.

    A rejected claim intentionally carries no continuation, so a caller cannot accidentally
    pass a rejected tool input to an execution path merely by ignoring the resolution field.
    """

    resolution: Literal["approve", "reject"]
    continuation: ParkedContinuation | None


def _canonical_input(value: dict) -> str:
    """Return the stable JSON representation used for parked-call integrity checks."""
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise ValueError("parked tool input must be JSON-serializable") from exc


@dataclass(frozen=True)
class TaskRun:
    """One execution (or non-execution: missed/aborted) of a task.

    ``approval_state == 'pending'`` means this otherwise-running row is deliberately parked.
    It must never be treated as a crash orphan or as permission to replay its tool call.
    """

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
    continuation: ParkedContinuation | None = None
    approval_state: str = "none"  # none | pending | approved | rejected


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
        project_id: int | None = None,
    ) -> int:
        """Insert an active task with its first fire time. Returns its id. ``project_id``
        scopes the task to a project (None == global; existing tasks stay global)."""
        now = _now()
        async with self.lock:
            cursor = await self.db.execute(
                "INSERT INTO tasks (kind, title, payload, schedule_kind, schedule_spec, "
                "timezone, next_run_at, status, created_by, source_session_id, "
                "created_at, updated_at, project_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, ?, ?)",
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
                    project_id,
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

    async def list(
        self,
        *,
        include_finished: bool = False,
        project_id: object = ANY_PROJECT,
        include_global: bool = True,
    ) -> list[Task]:
        """Tasks, optionally scoped to a project (Phase 10). ``project_id`` ANY_PROJECT
        (default) = every task; P = P's tasks (+ global when ``include_global``); None =
        global only. A project page passes P; the REPL/global views use the default."""
        clauses: list[str] = [] if include_finished else ["status = 'active'"]
        params: list[object] = []
        if project_id is not ANY_PROJECT:
            if project_id is None:
                clauses.append("project_id IS NULL")
            elif include_global:
                clauses.append("(project_id = ? OR project_id IS NULL)")
                params.append(project_id)
            else:
                clauses.append("project_id = ?")
                params.append(project_id)
        where = f"WHERE {' AND '.join(clauses)} " if clauses else ""
        cursor = await self.db.execute(
            f"SELECT {_TASK_COLUMNS} FROM tasks {where}ORDER BY id", tuple(params)
        )
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

    async def park_run(
        self,
        run_id: int,
        *,
        session_id: int,
        continuation: ParkedContinuation,
    ) -> bool:
        """Durably park one currently-running task run at one exact ``ASK`` call.

        The transition is conditional and atomic.  A second park attempt, a terminal run, or a
        run already claimed by a resolver returns ``False``.  Its ordinary ``running`` status
        continues to coalesce task firing; ``approval_state='pending'`` distinguishes it from a
        crash orphan, so startup cannot silently abort or replay it.
        """
        async with transaction(self.db, self.lock):
            return await self._park_run_in_transaction(
                run_id, session_id=session_id, continuation=continuation
            )

    async def park_task_run_with_session(
        self,
        task_id: int,
        *,
        session_id: int,
        messages: list[dict],
        continuation: ParkedContinuation,
    ) -> int | None:
        """Atomically save a task transcript and park its one active run.

        This deliberately crosses the task/session store seam because a crash between those
        two writes would leave either an unmatched tool-use transcript with no continuation or
        a continuation whose session cannot reconstruct its model context.  Both stores share
        this database connection and lock, so one transaction is the only safe composition.
        ``None`` means no active, unparked run was available; nothing is persisted in that case.
        """
        async with transaction(self.db, self.lock):
            cursor = await self.db.execute(
                "SELECT r.id FROM task_runs r JOIN tasks t ON t.id = r.task_id "
                "WHERE r.task_id = ? AND r.status = 'running' AND r.approval_state = 'none' "
                "AND t.status = 'active' ORDER BY r.id DESC LIMIT 1",
                (task_id,),
            )
            row = await cursor.fetchone()
            if row is None:
                return None
            run_id = int(row[0])
            if not await self._park_run_in_transaction(
                run_id, session_id=session_id, continuation=continuation
            ):
                return None
            now = _now()
            rows = [
                (session_id, seq, message["role"], json.dumps(message.get("content")), now)
                for seq, message in enumerate(messages)
            ]
            await self.db.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
            await self.db.executemany(
                "INSERT INTO messages (session_id, seq, role, content, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                rows,
            )
            await self.db.execute(
                "UPDATE sessions SET updated_at = ?, reflected_at = NULL WHERE id = ?",
                (now, session_id),
            )
        return run_id

    async def _park_run_in_transaction(
        self,
        run_id: int,
        *,
        session_id: int,
        continuation: ParkedContinuation,
    ) -> bool:
        """Apply the conditional parked transition inside an already-held transaction."""
        encoded = continuation.to_json()  # verify before mutating durable state
        cursor = await self.db.execute(
            "UPDATE task_runs SET session_id = ?, continuation_json = ?, "
            "approval_state = 'pending' "
            "WHERE id = ? AND status = 'running' AND approval_state = 'none' "
            "AND EXISTS (SELECT 1 FROM tasks t "
            "            WHERE t.id = task_runs.task_id AND t.status = 'active')",
            (session_id, encoded, run_id),
        )
        if cursor.rowcount != 1:
            return False
        # Leaving a due timestamp in the past would make the wake loop spin even though due()
        # correctly coalesces the running row.  The run's scheduled_for retains the occurrence a
        # later explicit resume worker must complete; the task itself is inert until then.
        await self.db.execute(
            "UPDATE tasks SET next_run_at = NULL, updated_at = ? "
            "WHERE id = (SELECT task_id FROM task_runs WHERE id = ?)",
            (_now(), run_id),
        )
        return True

    async def repark_claimed_run_with_session(
        self,
        run_id: int,
        *,
        session_id: int,
        messages: list[dict],
        continuation: ParkedContinuation,
    ) -> bool:
        """Replace a claimed continuation with a later ``ASK`` from the same batch/turn.

        This is the only transition from ``approved`` back to ``pending``.  The caller has
        already consumed one owner resolution and discovered another ASK *before executing that
        new batch*.  Saving the transcript and new continuation together keeps the next resolver
        bound to the exact provider blocks it will eventually answer.  It cannot be used to
        revive a rejected/terminal/orphaned run.
        """
        encoded = continuation.to_json()  # integrity verification before opening the transaction
        async with transaction(self.db, self.lock):
            cursor = await self.db.execute(
                "UPDATE task_runs SET session_id = ?, continuation_json = ?, "
                "approval_state = 'pending' "
                "WHERE id = ? AND status = 'running' AND approval_state = 'approved' "
                "AND EXISTS (SELECT 1 FROM tasks t "
                "            WHERE t.id = task_runs.task_id AND t.status = 'active')",
                (session_id, encoded, run_id),
            )
            if cursor.rowcount != 1:
                return False
            now = _now()
            rows = [
                (session_id, seq, message["role"], json.dumps(message.get("content")), now)
                for seq, message in enumerate(messages)
            ]
            await self.db.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
            await self.db.executemany(
                "INSERT INTO messages (session_id, seq, role, content, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                rows,
            )
            await self.db.execute(
                "UPDATE sessions SET updated_at = ?, reflected_at = NULL WHERE id = ?",
                (now, session_id),
            )
        return True

    async def claim_parked_approval(
        self,
        run_id: int,
        *,
        resolution: Literal["approve", "reject"],
    ) -> ParkedApprovalClaim | None:
        """Consume one parked approval exactly once and return its explicit claim record.

        This is the public programmatic approve/reject seam.  It deliberately executes no tool
        and changes no schedule: a caller that receives an ``approve`` continuation must still
        bind it to an explicit resume worker, re-check the current hard-deny policy, and execute
        *only* the verified id/name/input it received.  If that worker crashes after this claim,
        no later startup may replay the action automatically.
        """
        if resolution not in {"approve", "reject"}:
            raise ValueError("parked approval resolution must be 'approve' or 'reject'")
        state = "approved" if resolution == "approve" else "rejected"
        async with transaction(self.db, self.lock):
            cursor = await self.db.execute(
                "SELECT r.continuation_json FROM task_runs r JOIN tasks t ON t.id = r.task_id "
                "WHERE r.id = ? AND r.status = 'running' AND r.approval_state = 'pending' "
                "AND t.status = 'active'",
                (run_id,),
            )
            row = await cursor.fetchone()
            if row is None or not isinstance(row[0], str):
                return None
            continuation = ParkedContinuation.from_json(row[0])
            updated = await self.db.execute(
                "UPDATE task_runs SET approval_state = ? "
                "WHERE id = ? AND status = 'running' AND approval_state = 'pending'",
                (state, run_id),
            )
            if updated.rowcount != 1:
                return None
        return ParkedApprovalClaim(
            resolution=resolution,
            continuation=continuation if resolution == "approve" else None,
        )

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
            await self.finish_run_in_transaction(
                run_id,
                status,
                session_id=session_id,
                result_text=result_text,
                denied_count=denied_count,
                error=error,
                cost_usd=cost_usd,
                advance=advance,
                now=now,
            )

    async def finish_run_in_transaction(
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
        now: str | None = None,
    ) -> None:
        """Finish a run inside an already-held shared transaction.

        This is the narrow composition seam for a terminal task transition and its durable
        attention alert. Callers must own this store's shared lock and transaction; ordinary
        callers should use :meth:`finish_run`.
        """
        now = now or _now()
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

    async def get_run(self, run_id: int) -> TaskRun | None:
        """Return one run, including a verified parked continuation when present."""
        cursor = await self.db.execute(
            f"SELECT {_RUN_COLUMNS} FROM task_runs WHERE id = ?", (run_id,)
        )
        row = await cursor.fetchone()
        return _row_to_run(row) if row else None

    async def pending_approval_count(self, *, project_id: int | None) -> int:
        """Count durable parked approvals in exactly one project scope.

        ``None`` means only global/unscoped tasks, not every project.  This is the count used by
        a minimized approval nudge, so a project cannot learn that another project has pending
        work.  Only an active task with a running, ``pending`` approval row is actionable.
        """
        cursor = await self.db.execute(
            "SELECT COUNT(*) FROM task_runs r JOIN tasks t ON t.id = r.task_id "
            "WHERE r.status = 'running' AND r.approval_state = 'pending' "
            "AND t.status = 'active' AND t.project_id IS ?",
            (project_id,),
        )
        row = await cursor.fetchone()
        return int(row[0]) if row is not None else 0

    async def stale_runs(self) -> list[TaskRun]:
        """Crash-orphaned running rows, excluding intentional parked continuations.

        Pending/claimed parked rows remain durable and inert across restart.  They are never
        auto-replayed; a future host must use :meth:`claim_parked_approval` and an explicit
        resume worker.  Ordinary rows still sweep to ``aborted`` as before.
        """
        cursor = await self.db.execute(
            f"SELECT {_RUN_COLUMNS} FROM task_runs "
            "WHERE status = 'running' AND approval_state = 'none' ORDER BY id"
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
    *base, continuation_json, approval_state = row
    continuation = (
        ParkedContinuation.from_json(continuation_json)
        if isinstance(continuation_json, str)
        else None
    )
    return TaskRun(*base, continuation=continuation, approval_state=approval_state)
