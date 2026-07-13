"""``agent_runs`` persistence: the audit trail for delegated sub-agent runs (Phase 6).

Mirrors the ``task_runs`` discipline. A row is opened ``'running'`` *before* the
child executes (:meth:`begin_run`), so a crash between start and finish leaves a
detectable orphan the startup sweep (:meth:`sweep_orphans`) marks ``'aborted'`` —
a child's side effects may have partly completed, so it must never be silently
forgotten or re-run. Nothing here is ever DELETEd: this is audit, and the
never-DELETE invariant of ADR-0005 extends to it (ADR-0006).

Each row records *both* trace ids — the parent turn's and the child turn's — so a
single log query reconstructs the full parent↔child causality chain.

All writes hold the shared write lock (see :mod:`jarvis.persistence.db`).
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import json
from dataclasses import dataclass

import aiosqlite

# Explicit column order (never SELECT *), matching the agent_runs schema (v7).
_COLS = (
    "id, project_id, parent_session_id, parent_trace_id, child_session_id, child_trace_id, "
    "title, prompt, tools_scope, status, iterations, denied_count, input_tokens, "
    "output_tokens, cost_usd, result_text, error, started_at, finished_at, created_at, "
    "skills_manifest_json"
)


def _now() -> str:
    return _dt.datetime.now(_dt.UTC).isoformat()


def _skills_manifest(value: str | None) -> list:
    """Decode historical skills metadata fail-closed.

    The read model applies the field allowlist before any browser response.  Keeping the store
    tolerant as well means one malformed old row cannot make an otherwise bodies-free Studio
    detail unavailable.
    """
    if not value:
        return []
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return []
    return parsed if isinstance(parsed, list) else []


@dataclass
class AgentRun:
    """One row of ``agent_runs`` — a single delegated sub-agent run."""

    id: int
    project_id: int | None
    parent_session_id: int | None
    parent_trace_id: str | None
    child_session_id: int | None
    child_trace_id: str | None
    title: str
    prompt: str
    tools_scope: list[str]
    status: str
    iterations: int
    denied_count: int
    input_tokens: int
    output_tokens: int
    cost_usd: float | None
    result_text: str | None
    error: str | None
    started_at: str
    finished_at: str | None
    created_at: str
    skills_manifest: list


def _row_to_run(row: tuple) -> AgentRun:
    return AgentRun(
        id=row[0],
        project_id=row[1],
        parent_session_id=row[2],
        parent_trace_id=row[3],
        child_session_id=row[4],
        child_trace_id=row[5],
        title=row[6],
        prompt=row[7],
        tools_scope=json.loads(row[8]) if row[8] else [],
        status=row[9],
        iterations=row[10],
        denied_count=row[11],
        input_tokens=row[12],
        output_tokens=row[13],
        cost_usd=row[14],
        result_text=row[15],
        error=row[16],
        started_at=row[17],
        finished_at=row[18],
        created_at=row[19],
        skills_manifest=_skills_manifest(row[20]),
    )


class AgentRunStore:
    """CRUD + orphan sweep over the ``agent_runs`` audit table."""

    def __init__(self, db: aiosqlite.Connection, lock: asyncio.Lock | None = None) -> None:
        self.db = db
        self.lock = lock or asyncio.Lock()

    async def begin_run(
        self,
        *,
        parent_session_id: int | None,
        parent_trace_id: str | None,
        title: str,
        prompt: str,
        tools_scope: list[str],
        project_id: int | None = None,
        orchestration_run_id: int | None = None,
        role: str | None = None,
        stage: str | None = None,
        skills_manifest: list[dict] | None = None,
    ) -> int:
        """Open a ``'running'`` row before the child executes; returns its id. The Phase 10
        columns (project_id / orchestration_run_id / role / stage) are NULL for a plain
        Phase-6 spawn and set when the host orchestration engine drives the run (10B)."""
        now = _now()
        async with self.lock:
            cursor = await self.db.execute(
                "INSERT INTO agent_runs "
                "(parent_session_id, parent_trace_id, title, prompt, tools_scope, "
                "status, started_at, created_at, project_id, orchestration_run_id, role, stage, "
                "skills_manifest_json) "
                "VALUES (?, ?, ?, ?, ?, 'running', ?, ?, ?, ?, ?, ?, ?)",
                (
                    parent_session_id,
                    parent_trace_id,
                    title,
                    prompt,
                    json.dumps(tools_scope),
                    now,
                    now,
                    project_id,
                    orchestration_run_id,
                    role,
                    stage,
                    json.dumps(skills_manifest or []),
                ),
            )
            await self.db.commit()
        assert cursor.lastrowid is not None
        return cursor.lastrowid

    async def complete_run(
        self,
        run_id: int,
        *,
        status: str,
        child_session_id: int | None = None,
        child_trace_id: str | None = None,
        iterations: int = 0,
        denied_count: int = 0,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cost_usd: float | None = None,
        result_text: str | None = None,
        error: str | None = None,
    ) -> None:
        """Record a run's terminal outcome (``ok`` / ``error`` / ``timeout`` /
        ``cancelled`` / ``aborted``) and its measured totals."""
        now = _now()
        async with self.lock:
            await self.db.execute(
                "UPDATE agent_runs SET status = ?, child_session_id = ?, child_trace_id = ?, "
                "iterations = ?, denied_count = ?, input_tokens = ?, output_tokens = ?, "
                "cost_usd = ?, result_text = ?, error = ?, finished_at = ? WHERE id = ?",
                (
                    status,
                    child_session_id,
                    child_trace_id,
                    iterations,
                    denied_count,
                    input_tokens,
                    output_tokens,
                    cost_usd,
                    result_text,
                    error,
                    now,
                    run_id,
                ),
            )
            await self.db.commit()

    async def get(self, run_id: int) -> AgentRun | None:
        cursor = await self.db.execute(f"SELECT {_COLS} FROM agent_runs WHERE id = ?", (run_id,))
        row = await cursor.fetchone()
        return _row_to_run(row) if row else None

    async def list(
        self,
        *,
        limit: int = 50,
        parent_session_id: int | None = None,
        project_id: int | None = None,
    ) -> list[AgentRun]:
        """Most-recent runs first, optionally scoped to a parent session and/or project.

        ``project_id=None`` remains the administrative aggregate; callers that hold a live
        project workspace pass its concrete id. This keeps project scoping server-owned rather
        than turning a browser query parameter into a history-enumeration capability.
        """
        clauses: list[str] = []
        params: list[object] = []
        if parent_session_id is not None:
            clauses.append("parent_session_id = ?")
            params.append(parent_session_id)
        if project_id is not None:
            clauses.append("project_id = ?")
            params.append(project_id)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        cursor = await self.db.execute(
            f"SELECT {_COLS} FROM agent_runs{where} ORDER BY id DESC LIMIT ?",
            (*params, limit),
        )
        return [_row_to_run(row) for row in await cursor.fetchall()]

    async def member_runs(self, orchestration_run_id: int) -> list[dict]:
        """Bodies-free per-member metadata for one orchestration run (Phase 10B Studio detail):
        role / stage / status / iterations / denied / cost plus recorded skills metadata — NEVER
        the prompt or result_text (those are a fresh injection channel and stay off every UI
        surface).  The UI read model further allowlists the metadata before it is exposed."""
        cursor = await self.db.execute(
            "SELECT id, role, stage, status, iterations, denied_count, cost_usd, title, "
            "skills_manifest_json "
            "FROM agent_runs WHERE orchestration_run_id = ? ORDER BY id",
            (orchestration_run_id,),
        )
        return [
            {
                "id": r[0],
                "role": r[1],
                "stage": r[2],
                "status": r[3],
                "iterations": r[4],
                "denied_count": r[5],
                "cost_usd": r[6],
                "title": r[7],
                "skills_manifest": _skills_manifest(r[8]),
            }
            for r in await cursor.fetchall()
        ]

    async def sweep_orphans(self) -> list[str]:
        """Mark any still-``'running'`` rows ``'aborted'`` (a crash left them open) and
        return one human-readable note per orphan. Called at startup, mirroring the
        scheduler's stale-run sweep."""
        cursor = await self.db.execute(
            "SELECT id, title FROM agent_runs WHERE status = 'running' ORDER BY id"
        )
        orphans = await cursor.fetchall()
        if not orphans:
            return []
        note = "interrupted before completion (process exited mid-run) — marked aborted"
        now = _now()
        async with self.lock:
            await self.db.execute(
                "UPDATE agent_runs SET status = 'aborted', error = ?, finished_at = ? "
                "WHERE status = 'running'",
                (note, now),
            )
            await self.db.commit()
        return [f'sub-agent run #{run_id} "{title}" {note}' for run_id, title in orphans]
