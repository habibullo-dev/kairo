"""DigestStore: persistence for generated digests (schema v6).

Same rules as the other stores: the shared aiosqlite connection + write lock. Rows are
MINIMIZED (amendment A4) — the store only ever receives and
persists structured sections (snippets/counts/headers/status), a summary, suggested actions,
and delivery/provenance. Raw email bodies and provider error bodies never reach it.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import json
from dataclasses import dataclass

import aiosqlite

_COLUMNS = (
    "id, task_id, date_local, generated_at, sections_json, summary, "
    "suggested_actions_json, delivered_to, cost_usd, created_at"
)


def _now() -> str:
    return _dt.datetime.now(_dt.UTC).isoformat()


@dataclass(frozen=True)
class DigestRecord:
    id: int
    task_id: int | None
    date_local: str
    generated_at: str
    sections: list[dict]
    summary: str
    suggested_actions: list[str]
    delivered_to: list[str]
    cost_usd: float | None
    created_at: str


class DigestStore:
    def __init__(self, db: aiosqlite.Connection, lock: asyncio.Lock | None = None) -> None:
        self.db = db
        self.lock = lock or asyncio.Lock()

    async def add(
        self,
        *,
        task_id: int | None,
        date_local: str,
        generated_at: str,
        sections: list[dict],
        summary: str,
        suggested_actions: list[str],
        delivered_to: list[str],
        cost_usd: float | None,
    ) -> int:
        now = _now()
        async with self.lock:
            cursor = await self.db.execute(
                "INSERT INTO digests (task_id, date_local, generated_at, sections_json, summary, "
                "suggested_actions_json, delivered_to, cost_usd, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    task_id,
                    date_local,
                    generated_at,
                    json.dumps(sections),
                    summary,
                    json.dumps(suggested_actions),
                    json.dumps(delivered_to),
                    cost_usd,
                    now,
                ),
            )
            await self.db.commit()
        assert cursor.lastrowid is not None
        return cursor.lastrowid

    async def set_delivered(self, digest_id: int, delivered_to: list[str]) -> None:
        """Update the delivery record after best-effort notifier sends (UI/DB already done)."""
        async with self.lock:
            await self.db.execute(
                "UPDATE digests SET delivered_to = ? WHERE id = ?",
                (json.dumps(delivered_to), digest_id),
            )
            await self.db.commit()

    async def latest(self) -> DigestRecord | None:
        cursor = await self.db.execute(f"SELECT {_COLUMNS} FROM digests ORDER BY id DESC LIMIT 1")
        row = await cursor.fetchone()
        return _row(row) if row else None

    async def list(self, *, limit: int = 10) -> list[DigestRecord]:
        cursor = await self.db.execute(
            f"SELECT {_COLUMNS} FROM digests ORDER BY id DESC LIMIT ?", (limit,)
        )
        return [_row(r) for r in await cursor.fetchall()]


def _row(row: tuple) -> DigestRecord:
    return DigestRecord(
        id=row[0],
        task_id=row[1],
        date_local=row[2],
        generated_at=row[3],
        sections=json.loads(row[4]),
        summary=row[5],
        suggested_actions=json.loads(row[6]),
        delivered_to=json.loads(row[7]),
        cost_usd=row[8],
        created_at=row[9],
    )
