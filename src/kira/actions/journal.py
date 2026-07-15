"""ConnectorWriteJournal: the metadata-only outbox of outward writes (schema v10).

One row per executed connector write, in the spirit of ``model_calls`` / ``service_calls``: it
records THAT a write happened and HOW to reverse it, never WHAT was written. Deliberately absent:
titles, bodies, attendee addresses, secrets, provider response bodies. Those live on
``write_intents`` (needed to execute faithfully) — the journal is the audit/undo surface, so it
carries only handles: the remote id, the rollback kind + handle, the scope exercised, timestamps.

That split is a pinned safety invariant: a journal read model can be surfaced anywhere (Activity
feed, Workspace, a future Phase-16 briefing) without leaking a private write's content.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
from dataclasses import dataclass

import aiosqlite


@dataclass(frozen=True)
class ConnectorWrite:
    """One row of ``connector_writes`` — metadata only, no content."""

    id: int
    ts: str
    intent_id: int | None
    provider: str
    verb: str
    scope: str | None
    project_id: int | None
    remote_id: str | None
    rollback_kind: str | None
    rollback_ref: str | None
    status: str
    egress_ref: str | None
    trace_id: str | None
    created_at: str


_COLUMNS = (
    "id, ts, intent_id, provider, verb, scope, project_id, remote_id, rollback_kind, "
    "rollback_ref, status, egress_ref, trace_id, created_at"
)


def _now() -> str:
    return _dt.datetime.now(_dt.UTC).isoformat()


def _row_to_write(row: tuple) -> ConnectorWrite:
    return ConnectorWrite(
        id=row[0],
        ts=row[1],
        intent_id=row[2],
        provider=row[3],
        verb=row[4],
        scope=row[5],
        project_id=row[6],
        remote_id=row[7],
        rollback_kind=row[8],
        rollback_ref=row[9],
        status=row[10],
        egress_ref=row[11],
        trace_id=row[12],
        created_at=row[13],
    )


class ConnectorWriteJournal:
    """SQLite persistence for the outward-write journal (schema v10)."""

    def __init__(self, db: aiosqlite.Connection, lock: asyncio.Lock | None = None) -> None:
        self.db = db
        self.lock = lock or asyncio.Lock()

    async def record(
        self,
        *,
        provider: str,
        verb: str,
        status: str,
        intent_id: int | None = None,
        scope: str | None = None,
        project_id: int | None = None,
        remote_id: str | None = None,
        rollback_kind: str | None = None,
        rollback_ref: str | None = None,
        egress_ref: str | None = None,
        trace_id: str | None = None,
        ts: str | None = None,
    ) -> int:
        """Append a journal row and return its id. Callers pass only metadata handles — there is
        no parameter for content, by construction. ``status`` ∈ executed|failed|undone (enforced
        by the table CHECK)."""
        now = _now()
        async with self.lock:
            cursor = await self.db.execute(
                "INSERT INTO connector_writes (ts, intent_id, provider, verb, scope, project_id, "
                "remote_id, rollback_kind, rollback_ref, status, egress_ref, trace_id, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    ts or now,
                    intent_id,
                    provider,
                    verb,
                    scope,
                    project_id,
                    remote_id,
                    rollback_kind,
                    rollback_ref,
                    status,
                    egress_ref,
                    trace_id,
                    now,
                ),
            )
            await self.db.commit()
        assert cursor.lastrowid is not None
        return cursor.lastrowid

    async def get(self, write_id: int) -> ConnectorWrite | None:
        cursor = await self.db.execute(
            f"SELECT {_COLUMNS} FROM connector_writes WHERE id = ?", (write_id,)
        )
        row = await cursor.fetchone()
        return _row_to_write(row) if row else None

    async def list(
        self,
        *,
        intent_id: int | None = None,
        project_id: int | None = None,
        limit: int = 100,
    ) -> list[ConnectorWrite]:
        """Journal rows, newest first, optionally scoped to one intent or project."""
        clauses: list[str] = []
        params: list[object] = []
        if intent_id is not None:
            clauses.append("intent_id = ?")
            params.append(intent_id)
        if project_id is not None:
            clauses.append("project_id = ?")
            params.append(project_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(max(1, limit))
        cursor = await self.db.execute(
            f"SELECT {_COLUMNS} FROM connector_writes {where} ORDER BY id DESC LIMIT ?",
            tuple(params),
        )
        return [_row_to_write(r) for r in await cursor.fetchall()]

    async def mark_status(self, write_id: int, status: str) -> bool:
        """Flip a journal row's ``status`` (e.g. executed → undone once a write is reversed).
        Returns False if the row doesn't exist."""
        async with self.lock:
            cursor = await self.db.execute(
                "UPDATE connector_writes SET status = ? WHERE id = ?", (status, write_id)
            )
            await self.db.commit()
        return cursor.rowcount > 0
