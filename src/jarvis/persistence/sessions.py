"""Session + message persistence.

The model is stateless — the whole conversation lives here and is reconstructed on
every model call. Each turn, the full message list is saved (delete + re-insert):
simple and correct at conversation scale, and it preserves message content exactly
as JSON (including thinking blocks and their signatures) so a resumed session
replays to the API unchanged.
"""

from __future__ import annotations

import datetime as _dt
import json
from pathlib import Path

import aiosqlite

from jarvis.persistence.db import connect


def _now() -> str:
    return _dt.datetime.now(_dt.UTC).isoformat()


class SessionStore:
    def __init__(self, db: aiosqlite.Connection) -> None:
        self.db = db

    @classmethod
    async def open(cls, path: Path) -> SessionStore:
        return cls(await connect(path))

    async def close(self) -> None:
        await self.db.close()

    async def create_session(self, title: str | None = None) -> int:
        now = _now()
        cursor = await self.db.execute(
            "INSERT INTO sessions (created_at, updated_at, title) VALUES (?, ?, ?)",
            (now, now, title),
        )
        await self.db.commit()
        assert cursor.lastrowid is not None
        return cursor.lastrowid

    async def save_messages(self, session_id: int, messages: list[dict]) -> None:
        """Persist the full conversation for a session (replaces prior rows)."""
        now = _now()
        rows = [
            (session_id, seq, m["role"], json.dumps(m.get("content")), now)
            for seq, m in enumerate(messages)
        ]
        await self.db.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
        await self.db.executemany(
            "INSERT INTO messages (session_id, seq, role, content, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            rows,
        )
        await self.db.execute("UPDATE sessions SET updated_at = ? WHERE id = ?", (now, session_id))
        await self.db.commit()

    async def load_messages(self, session_id: int) -> list[dict]:
        cursor = await self.db.execute(
            "SELECT role, content FROM messages WHERE session_id = ? ORDER BY seq",
            (session_id,),
        )
        rows = await cursor.fetchall()
        return [{"role": role, "content": json.loads(content)} for role, content in rows]

    async def latest_session_id(self) -> int | None:
        cursor = await self.db.execute(
            "SELECT id FROM sessions ORDER BY updated_at DESC, id DESC LIMIT 1"
        )
        row = await cursor.fetchone()
        return row[0] if row else None

    async def save_compaction(self, session_id: int, summary: str | None, cut: int) -> None:
        """Persist the ContextManager's frozen summary + cut so ``--resume`` restores
        the exact working state instead of re-summarizing (and getting a different
        summary than the session was running with)."""
        await self.db.execute(
            "UPDATE sessions SET compaction_summary = ?, compaction_cut = ? WHERE id = ?",
            (summary, cut, session_id),
        )
        await self.db.commit()

    async def load_compaction(self, session_id: int) -> tuple[str | None, int]:
        cursor = await self.db.execute(
            "SELECT compaction_summary, compaction_cut FROM sessions WHERE id = ?",
            (session_id,),
        )
        row = await cursor.fetchone()
        if not row:
            return None, 0
        return row[0], row[1] or 0

    async def unreflected_session_ids(self, *, exclude: int | None = None) -> list[int]:
        """Past sessions that have messages but were never reflected on (e.g. the
        process was killed before exit). Used for startup catch-up."""
        cursor = await self.db.execute(
            "SELECT s.id FROM sessions s "
            "WHERE s.reflected_at IS NULL "
            "AND EXISTS (SELECT 1 FROM messages m WHERE m.session_id = s.id) "
            "AND s.id != ? "
            "ORDER BY s.id",
            (exclude if exclude is not None else -1,),
        )
        return [row[0] for row in await cursor.fetchall()]

    async def mark_reflected(self, session_id: int) -> None:
        await self.db.execute(
            "UPDATE sessions SET reflected_at = ? WHERE id = ?", (_now(), session_id)
        )
        await self.db.commit()
