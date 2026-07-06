"""Session + message persistence.

The model is stateless — the whole conversation lives here and is reconstructed on
every model call. Each turn, the full message list is saved (delete + re-insert):
simple and correct at conversation scale, and it preserves message content exactly
as JSON (including thinking blocks and their signatures) so a resumed session
replays to the API unchanged.

Sessions have a ``kind``: ``'interactive'`` (a human at the REPL), ``'task'`` (an
unattended background job's transcript), or ``'subagent'`` (a delegated child
run's transcript — Phase 6). Non-interactive sessions are deliberately
second-class: they never win :meth:`latest_session_id` (``--resume`` must not
land the user inside a job or sub-agent transcript). ``'task'`` sessions feed the
reflection queries only when explicitly opted in
(``scheduler.reflect_job_sessions``); ``'subagent'`` sessions feed them *never* —
:data:`REFLECTABLE_KINDS` is the hard ceiling, so a child transcript (which may
quote poisoned fetched content) can't launder into long-term memory.

All writes hold the shared write lock (see :mod:`jarvis.persistence.db`).
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import json
from pathlib import Path

import aiosqlite

from jarvis.persistence.db import connect, transaction


def _now() -> str:
    return _dt.datetime.now(_dt.UTC).isoformat()


#: Session kinds that may EVER feed reflection into long-term memory. 'subagent' is
#: deliberately absent: a delegated child's transcript can quote poisoned fetched
#: content, so it must never launder into permanent memory (Phase 6 / ADR-0006). The
#: reflection queries intersect their requested ``kinds`` with this ceiling, so *no*
#: caller — however buggy — can reflect a subagent session. This is the structural
#: firewall the plan's D4 requires, not a convention callers must remember.
REFLECTABLE_KINDS: frozenset[str] = frozenset({"interactive", "task"})

#: The default set reflection considers: interactive sessions only. Background job
#: ('task') transcripts opt in via ``scheduler.reflect_job_sessions`` (see
#: :func:`reflectable_kinds`).
INTERACTIVE_ONLY: frozenset[str] = frozenset({"interactive"})


def reflectable_kinds(*, reflect_job_sessions: bool) -> frozenset[str]:
    """Map the ``scheduler.reflect_job_sessions`` config knob to a kinds set for the
    reflection queries. Always a subset of :data:`REFLECTABLE_KINDS`, so the result
    can never contain 'subagent' — the single place callers derive a kinds set."""
    return REFLECTABLE_KINDS if reflect_job_sessions else INTERACTIVE_ONLY


class SessionStore:
    def __init__(self, db: aiosqlite.Connection, lock: asyncio.Lock | None = None) -> None:
        self.db = db
        self.lock = lock or asyncio.Lock()

    @classmethod
    async def open(cls, path: Path) -> SessionStore:
        return cls(await connect(path))

    async def close(self) -> None:
        await self.db.close()

    async def create_session(self, title: str | None = None, *, kind: str = "interactive") -> int:
        now = _now()
        async with self.lock:
            cursor = await self.db.execute(
                "INSERT INTO sessions (created_at, updated_at, title, kind) VALUES (?, ?, ?, ?)",
                (now, now, title, kind),
            )
            await self.db.commit()
        assert cursor.lastrowid is not None
        return cursor.lastrowid

    async def save_messages(self, session_id: int, messages: list[dict]) -> None:
        """Persist the full conversation for a session (replaces prior rows).

        Also clears ``reflected_at``: new content means the session's reflection is
        stale, so it must be reflected again (on clean exit, or by startup catch-up
        if the process dies first). Without this, resuming a reflected session and
        adding turns would leave ``reflected_at`` set and the new turns unreflected.

        DELETE + INSERT + UPDATE are one :func:`transaction` — an interleaved or
        half-committed save must never be able to lose a session's history."""
        now = _now()
        rows = [
            (session_id, seq, m["role"], json.dumps(m.get("content")), now)
            for seq, m in enumerate(messages)
        ]
        async with transaction(self.db, self.lock):
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

    async def load_messages(self, session_id: int) -> list[dict]:
        cursor = await self.db.execute(
            "SELECT role, content FROM messages WHERE session_id = ? ORDER BY seq",
            (session_id,),
        )
        rows = await cursor.fetchall()
        return [{"role": role, "content": json.loads(content)} for role, content in rows]

    async def latest_session_id(self) -> int | None:
        """Most recently updated *interactive* session — task sessions are invisible
        here so ``--resume`` never lands inside a background job's transcript."""
        cursor = await self.db.execute(
            "SELECT id FROM sessions WHERE kind = 'interactive' "
            "ORDER BY updated_at DESC, id DESC LIMIT 1"
        )
        row = await cursor.fetchone()
        return row[0] if row else None

    async def save_compaction(self, session_id: int, summary: str | None, cut: int) -> None:
        """Persist the ContextManager's frozen summary + cut so ``--resume`` restores
        the exact working state instead of re-summarizing (and getting a different
        summary than the session was running with)."""
        async with self.lock:
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

    async def unreflected_session_ids(
        self, *, exclude: int | None = None, kinds: frozenset[str] = INTERACTIVE_ONLY
    ) -> list[int]:
        """Past sessions that have messages but were never reflected on (e.g. the
        process was killed before exit). Used for startup catch-up.

        ``kinds`` selects which session kinds to consider (default: interactive only;
        use :func:`reflectable_kinds` to derive it from config). It is intersected
        with :data:`REFLECTABLE_KINDS`, so a subagent session is never returned no
        matter what is passed."""
        allowed = kinds & REFLECTABLE_KINDS
        if not allowed:
            return []
        placeholders = ",".join("?" for _ in allowed)
        cursor = await self.db.execute(
            "SELECT s.id FROM sessions s "
            "WHERE s.reflected_at IS NULL "
            "AND EXISTS (SELECT 1 FROM messages m WHERE m.session_id = s.id) "
            f"AND s.kind IN ({placeholders}) "
            "AND s.id != ? "
            "ORDER BY s.id",
            (*sorted(allowed), exclude if exclude is not None else -1),
        )
        return [row[0] for row in await cursor.fetchall()]

    async def mark_reflected(self, session_id: int) -> None:
        async with self.lock:
            await self.db.execute(
                "UPDATE sessions SET reflected_at = ? WHERE id = ?", (_now(), session_id)
            )
            await self.db.commit()

    async def needs_reflection(
        self, session_id: int, *, kinds: frozenset[str] = INTERACTIVE_ONLY
    ) -> bool:
        """True if the session has content and hasn't been reflected since its last
        change (``reflected_at IS NULL``). Lets the on-exit path skip re-reflecting a
        session that was only resumed and read, not modified.

        ``kinds`` is intersected with :data:`REFLECTABLE_KINDS` (default: interactive
        only), so a subagent session always reports False."""
        allowed = kinds & REFLECTABLE_KINDS
        if not allowed:
            return False
        placeholders = ",".join("?" for _ in allowed)
        cursor = await self.db.execute(
            "SELECT 1 FROM sessions s "
            "WHERE s.id = ? AND s.reflected_at IS NULL "
            f"AND s.kind IN ({placeholders}) "
            "AND EXISTS (SELECT 1 FROM messages m WHERE m.session_id = s.id)",
            (session_id, *sorted(allowed)),
        )
        return await cursor.fetchone() is not None
