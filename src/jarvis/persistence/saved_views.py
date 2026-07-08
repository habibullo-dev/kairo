"""SavedViewStore: user-defined smart collections / saved searches (schema v9).

A saved view is a named filter over a surface (``projects`` | ``artifacts`` | ``search``) —
"Needs review", "Generated this week", a saved search query. Unlike the content/history
stores (sessions, memories, projects, artifacts) which never DELETE, a saved view is a UI
preference the user owns, so :meth:`SavedViewStore.delete` is a real delete (removing a
bookmark, not erasing history). ``query_json`` is an opaque filter spec, replaced wholesale.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import json
from dataclasses import dataclass

import aiosqlite

from jarvis.persistence.fts import ANY_PROJECT

_COLUMNS = "id, name, scope, query_json, project_id, created_by, created_at, updated_at"
_SCOPES = ("projects", "artifacts", "search")
_CREATED_BY = ("user", "agent", "system")


def _now() -> str:
    return _dt.datetime.now(_dt.UTC).isoformat()


@dataclass(frozen=True)
class SavedView:
    id: int
    name: str
    scope: str
    query: dict
    project_id: int | None
    created_by: str
    created_at: str
    updated_at: str


def _row_to_view(row: tuple) -> SavedView:
    return SavedView(
        id=row[0],
        name=row[1],
        scope=row[2],
        query=json.loads(row[3]) if row[3] else {},
        project_id=row[4],
        created_by=row[5],
        created_at=row[6],
        updated_at=row[7],
    )


class SavedViewStore:
    def __init__(self, db: aiosqlite.Connection, lock: asyncio.Lock | None = None) -> None:
        self.db = db
        self.lock = lock or asyncio.Lock()

    async def save(
        self,
        *,
        name: str,
        scope: str,
        query: dict | None = None,
        project_id: int | None = None,
        created_by: str = "user",
        view_id: int | None = None,
    ) -> int:
        """Create a saved view, or update the one at ``view_id`` if it exists. Returns its id."""
        if scope not in _SCOPES:
            raise ValueError(f"unknown saved-view scope: {scope!r}")
        if created_by not in _CREATED_BY:
            raise ValueError(f"unknown created_by: {created_by!r}")
        query_json = json.dumps(query or {})
        now = _now()
        async with self.lock:
            if view_id is not None:
                cursor = await self.db.execute(
                    "UPDATE saved_views SET name = ?, scope = ?, query_json = ?, "
                    "project_id = ?, updated_at = ? WHERE id = ?",
                    (name, scope, query_json, project_id, now, view_id),
                )
                if cursor.rowcount > 0:
                    await self.db.commit()
                    return view_id
            cursor = await self.db.execute(
                "INSERT INTO saved_views (name, scope, query_json, project_id, created_by, "
                "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (name, scope, query_json, project_id, created_by, now, now),
            )
            await self.db.commit()
        assert cursor.lastrowid is not None
        return cursor.lastrowid

    async def get(self, view_id: int) -> SavedView | None:
        cursor = await self.db.execute(
            f"SELECT {_COLUMNS} FROM saved_views WHERE id = ?", (view_id,)
        )
        row = await cursor.fetchone()
        return _row_to_view(row) if row else None

    async def list(
        self,
        *,
        scope: str | None = None,
        project_id: object = ANY_PROJECT,
        include_global: bool = True,
    ) -> list[SavedView]:
        clauses: list[str] = []
        params: list[object] = []
        if scope is not None:
            clauses.append("scope = ?")
            params.append(scope)
        if project_id is not ANY_PROJECT:
            if project_id is None:
                clauses.append("project_id IS NULL")
            elif include_global:
                clauses.append("(project_id = ? OR project_id IS NULL)")
                params.append(project_id)
            else:
                clauses.append("project_id = ?")
                params.append(project_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        cursor = await self.db.execute(
            f"SELECT {_COLUMNS} FROM saved_views {where} ORDER BY name COLLATE NOCASE, id",
            tuple(params),
        )
        return [_row_to_view(r) for r in await cursor.fetchall()]

    async def delete(self, view_id: int) -> bool:
        """Remove a saved view (a UI bookmark). Returns False if it didn't exist."""
        async with self.lock:
            cursor = await self.db.execute("DELETE FROM saved_views WHERE id = ?", (view_id,))
            await self.db.commit()
        return cursor.rowcount > 0
