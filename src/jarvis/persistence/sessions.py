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
from dataclasses import dataclass
from pathlib import Path

import aiosqlite

from jarvis.persistence.db import connect, transaction

#: Sentinel for "don't filter by project" in list/search (distinct from ``None``, which
#: means the *global* scope — project_id IS NULL). A plain object so the default can't
#: collide with a real project id.
_ANY_PROJECT: object = object()


def _now() -> str:
    return _dt.datetime.now(_dt.UTC).isoformat()


@dataclass(frozen=True)
class SessionMeta:
    """A session summary for the chats list/search — no message bodies, just the row
    metadata plus a message count. ``pinned`` is surfaced so the UI can sort/badge."""

    id: int
    title: str | None
    kind: str
    project_id: int | None
    pinned: bool
    created_at: str
    updated_at: str
    reflected_at: str | None
    message_count: int
    archived: bool = False  # Phase 15.5: archived chats leave the default lists (never deleted)


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

    async def create_session(
        self, title: str | None = None, *, kind: str = "interactive", project_id: int | None = None
    ) -> int:
        """Create a session. ``project_id`` scopes it to a project (NULL == global). A
        session is bound to one project for its lifetime — reflection/promotion attribute
        to it, so switching projects starts a new session, never re-tags this one."""
        async with self.lock:
            session_id = await self.create_session_in_transaction(
                title,
                kind=kind,
                project_id=project_id,
            )
            await self.db.commit()
        return session_id

    async def create_session_in_transaction(
        self, title: str | None = None, *, kind: str = "interactive", project_id: int | None = None
    ) -> int:
        """Insert one session while the caller owns this store's database transaction.

        Cross-store lifecycle operations use this primitive so replacement sessions and the
        archive/reset that requires them either all commit or all roll back. The caller must hold
        ``self.lock`` through :func:`jarvis.persistence.db.transaction`.
        """
        now = _now()
        cursor = await self.db.execute(
            "INSERT INTO sessions (created_at, updated_at, title, kind, project_id) "
            "VALUES (?, ?, ?, ?, ?)",
            (now, now, title, kind, project_id),
        )
        assert cursor.lastrowid is not None
        return int(cursor.lastrowid)

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
        """Most recent non-empty interactive session suitable for ``--resume``.

        Task sessions and eagerly allocated browser-workspace rows with no messages are
        invisible, so opening a fresh UI tab cannot displace the user's last real chat.
        """
        cursor = await self.db.execute(
            "SELECT id FROM sessions WHERE kind = 'interactive' AND archived = 0 "
            "AND EXISTS (SELECT 1 FROM messages m WHERE m.session_id = sessions.id) "
            "ORDER BY updated_at DESC, id DESC LIMIT 1"
        )
        row = await cursor.fetchone()
        return row[0] if row else None

    _META_COLS = (
        "s.id, s.title, s.kind, s.project_id, s.pinned, s.created_at, s.updated_at, "
        "s.reflected_at, (SELECT count(*) FROM messages m WHERE m.session_id = s.id), s.archived"
    )

    @staticmethod
    def _row_to_meta(row: tuple) -> SessionMeta:
        return SessionMeta(
            id=row[0],
            title=row[1],
            kind=row[2],
            project_id=row[3],
            pinned=bool(row[4]),
            created_at=row[5],
            updated_at=row[6],
            reflected_at=row[7],
            message_count=row[8],
            archived=bool(row[9]),
        )

    def _scope_clause(self, project_id: object) -> tuple[str, list[object]]:
        """SQL fragment + params for a project filter. ``_ANY_PROJECT`` (default) adds
        nothing; ``None`` restricts to global (IS NULL); an int restricts to that project."""
        if project_id is _ANY_PROJECT:
            return "", []
        if project_id is None:
            return " AND s.project_id IS NULL", []
        return " AND s.project_id = ?", [project_id]

    async def list_sessions(
        self,
        *,
        kind: str | None = "interactive",
        project_id: object = _ANY_PROJECT,
        pinned: bool | None = None,
        include_archived: bool = False,
        limit: int = 50,
        offset: int = 0,
    ) -> list[SessionMeta]:
        """Sessions newest-first (pinned first), filtered by kind / project / pinned. Only
        sessions that have at least one message are returned — an empty lazily-created row
        never shows in the chats list. Archived chats are excluded unless ``include_archived``."""
        where = "WHERE (SELECT count(*) FROM messages m WHERE m.session_id = s.id) > 0"
        params: list[object] = []
        if kind is not None:
            where += " AND s.kind = ?"
            params.append(kind)
        scope_sql, scope_params = self._scope_clause(project_id)
        where += scope_sql
        params += scope_params
        if pinned is not None:
            where += " AND s.pinned = ?"
            params.append(1 if pinned else 0)
        if not include_archived:
            where += " AND s.archived = 0"
        cursor = await self.db.execute(
            f"SELECT {self._META_COLS} FROM sessions s {where} "
            "ORDER BY s.pinned DESC, s.updated_at DESC, s.id DESC LIMIT ? OFFSET ?",
            (*params, limit, offset),
        )
        return [self._row_to_meta(r) for r in await cursor.fetchall()]

    async def search_sessions(
        self,
        query: str,
        *,
        kind: str | None = "interactive",
        project_id: object = _ANY_PROJECT,
        limit: int = 50,
    ) -> list[SessionMeta]:
        """Case-insensitive substring search over titles AND message content, within the
        given kind/project scope. A plain LIKE — no embeddings — matching the chats UX."""
        like = f"%{query}%"
        where = (
            "WHERE (s.title LIKE ? OR EXISTS "
            "(SELECT 1 FROM messages m WHERE m.session_id = s.id AND m.content LIKE ?))"
        )
        params: list[object] = [like, like]
        if kind is not None:
            where += " AND s.kind = ?"
            params.append(kind)
        scope_sql, scope_params = self._scope_clause(project_id)
        where += scope_sql
        params += scope_params
        cursor = await self.db.execute(
            f"SELECT {self._META_COLS} FROM sessions s {where} "
            "ORDER BY s.pinned DESC, s.updated_at DESC, s.id DESC LIMIT ?",
            (*params, limit),
        )
        return [self._row_to_meta(r) for r in await cursor.fetchall()]

    async def count_since(
        self, since_iso: str, *, kind: str | None = "interactive", project_id: object = _ANY_PROJECT
    ) -> int:
        """Count sessions (with >=1 message) updated at/after ``since_iso`` within the kind/
        project scope — backs the Projects-grid 'sessions this week' health chip. ISO-8601 UTC
        strings compare lexicographically, so a string ``>=`` is a correct time filter."""
        where = (
            "WHERE (SELECT count(*) FROM messages m WHERE m.session_id = s.id) > 0 "
            "AND s.updated_at >= ?"
        )
        params: list[object] = [since_iso]
        if kind is not None:
            where += " AND s.kind = ?"
            params.append(kind)
        scope_sql, scope_params = self._scope_clause(project_id)
        where += scope_sql
        params += scope_params
        cursor = await self.db.execute(f"SELECT count(*) FROM sessions s {where}", tuple(params))
        row = await cursor.fetchone()
        return int(row[0]) if row else 0

    async def get_meta(self, session_id: int) -> SessionMeta | None:
        cursor = await self.db.execute(
            f"SELECT {self._META_COLS} FROM sessions s WHERE s.id = ?", (session_id,)
        )
        row = await cursor.fetchone()
        return self._row_to_meta(row) if row else None

    async def set_pinned(self, session_id: int, pinned: bool) -> bool:
        async with self.lock:
            cursor = await self.db.execute(
                "UPDATE sessions SET pinned = ? WHERE id = ?", (1 if pinned else 0, session_id)
            )
            await self.db.commit()
        return cursor.rowcount > 0

    async def set_title(self, session_id: int, title: str) -> bool:
        """Rename a chat (metadata only). ``updated_at`` is left untouched so a pure rename
        doesn't reorder the recents list."""
        async with self.lock:
            cursor = await self.db.execute(
                "UPDATE sessions SET title = ? WHERE id = ?", (title, session_id)
            )
            await self.db.commit()
        return cursor.rowcount > 0

    async def set_title_if_missing(self, session_id: int, title: str) -> bool:
        """Set a generated first-turn title without racing or replacing a human rename.

        The first message gives a new chat a useful label, but a user can rename the chat at
        any time.  Keeping the blank-title condition in SQL means a concurrent rename always
        wins; this helper can never overwrite it on the way out of a turn.
        """
        async with self.lock:
            cursor = await self.db.execute(
                "UPDATE sessions SET title = ? WHERE id = ? "
                "AND (title IS NULL OR trim(title) = '')",
                (title, session_id),
            )
            await self.db.commit()
        return cursor.rowcount > 0

    async def set_archived(self, session_id: int, archived: bool) -> bool:
        """Archive/unarchive a chat — a display-status flip (never a delete); archived chats
        drop out of the default lists but keep their full transcript for audit/resume."""
        async with self.lock:
            ok = await self.set_archived_in_transaction(session_id, archived)
            await self.db.commit()
        return ok

    async def set_archived_in_transaction(self, session_id: int, archived: bool) -> bool:
        """Set archive state while the caller owns this store's database transaction."""
        cursor = await self.db.execute(
            "UPDATE sessions SET archived = ? WHERE id = ?",
            (1 if archived else 0, session_id),
        )
        return cursor.rowcount > 0

    async def set_project(self, session_id: int, project_id: int | None) -> bool:
        """Re-scope a session. Used by promotion/admin paths — a live chat keeps its
        project for life (switching projects starts a new session instead)."""
        async with self.lock:
            cursor = await self.db.execute(
                "UPDATE sessions SET project_id = ? WHERE id = ?", (project_id, session_id)
            )
            await self.db.commit()
        return cursor.rowcount > 0

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
