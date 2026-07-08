"""ProjectStore: SQLite persistence for project workspaces (schema v7).

Plain SQL like the other stores, on the *same* aiosqlite connection and shared write
lock (a second connection deadlocks; a second lock lets a write land inside another
store's open transaction). Nothing is ever DELETEd — ``archive`` is a status flip, and
scoped rows in other tables keep a nullable ``project_id`` (NULL == global scope).

The store is mechanism, not policy: it does not decide scope or inject context (that is
``ProjectService`` in Task 3). ``repos_json``/``settings_json`` are opaque JSON columns
decoded into ``Project.repos``/``Project.settings``; ``settings`` holds per-project
overrides (model routes, budgets, roster tweaks) and **never** secrets or API keys.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import json
import re
from dataclasses import dataclass

import aiosqlite

_COLUMNS = (
    "id, name, slug, description, status, color, icon, repos_json, settings_json, "
    "created_at, updated_at, archived_at, pinned"
)

_STATUSES = ("active", "paused", "archived")

# Columns update() will set, mapped to how the value is serialized. Whitelisted so the
# SET clause is built from constants, never caller-supplied column names.
_UPDATABLE = ("name", "description", "color", "icon")


def _now() -> str:
    return _dt.datetime.now(_dt.UTC).isoformat()


def slugify(name: str) -> str:
    """A stable, filesystem-safe handle from a project name. Lowercase, non-alnum runs
    collapse to single hyphens, trimmed. Empty input yields ``'project'`` so a slug base
    always exists (uniqueness is enforced separately)."""
    s = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
    return s or "project"


@dataclass(frozen=True)
class Project:
    """One row of ``projects``. ``status`` is the lifecycle (active/paused/archived);
    ``repos``/``settings`` are the decoded JSON columns."""

    id: int
    name: str
    slug: str
    description: str | None
    status: str
    color: str | None
    icon: str | None
    repos: tuple[str, ...]
    settings: dict
    created_at: str
    updated_at: str
    archived_at: str | None
    pinned: bool = False  # Phase 11: surfaced-first in the Projects grid (default last)


def _row_to_project(row: tuple) -> Project:
    return Project(
        id=row[0],
        name=row[1],
        slug=row[2],
        description=row[3],
        status=row[4],
        color=row[5],
        icon=row[6],
        repos=tuple(json.loads(row[7])),
        settings=json.loads(row[8]),
        created_at=row[9],
        updated_at=row[10],
        archived_at=row[11],
        pinned=bool(row[12]),
    )


class ProjectStore:
    def __init__(self, db: aiosqlite.Connection, lock: asyncio.Lock | None = None) -> None:
        self.db = db
        self.lock = lock or asyncio.Lock()

    async def _unique_slug(self, name: str) -> str:
        """A slug for ``name`` not already taken (``base``, then ``base-2``, ``base-3`` …).
        Called under the write lock, so the single writer sees a consistent taken-set."""
        base = slugify(name)
        cursor = await self.db.execute(
            "SELECT slug FROM projects WHERE slug = ? OR slug LIKE ?", (base, f"{base}-%")
        )
        taken = {r[0] for r in await cursor.fetchall()}
        if base not in taken:
            return base
        i = 2
        while f"{base}-{i}" in taken:
            i += 1
        return f"{base}-{i}"

    async def create(
        self,
        *,
        name: str,
        description: str | None = None,
        color: str | None = None,
        icon: str | None = None,
        repos: list[str] | None = None,
        settings: dict | None = None,
    ) -> int:
        """Insert an active project (auto-unique slug from the name). Returns its id."""
        now = _now()
        async with self.lock:
            slug = await self._unique_slug(name)
            cursor = await self.db.execute(
                "INSERT INTO projects (name, slug, description, status, color, icon, "
                "repos_json, settings_json, created_at, updated_at) "
                "VALUES (?, ?, ?, 'active', ?, ?, ?, ?, ?, ?)",
                (
                    name,
                    slug,
                    description,
                    color,
                    icon,
                    json.dumps(list(repos or [])),
                    json.dumps(settings or {}),
                    now,
                    now,
                ),
            )
            await self.db.commit()
        assert cursor.lastrowid is not None
        return cursor.lastrowid

    async def get(self, project_id: int) -> Project | None:
        cursor = await self.db.execute(
            f"SELECT {_COLUMNS} FROM projects WHERE id = ?", (project_id,)
        )
        row = await cursor.fetchone()
        return _row_to_project(row) if row else None

    async def get_by_slug(self, slug: str) -> Project | None:
        cursor = await self.db.execute(f"SELECT {_COLUMNS} FROM projects WHERE slug = ?", (slug,))
        row = await cursor.fetchone()
        return _row_to_project(row) if row else None

    async def list(self, *, status: str | None = None) -> list[Project]:
        """Projects ordered by most-recently-updated. ``status`` filters to one lifecycle
        state (e.g. only ``'active'``); None returns all, including archived."""
        order = "ORDER BY updated_at DESC, id DESC"
        if status is not None:
            cursor = await self.db.execute(
                f"SELECT {_COLUMNS} FROM projects WHERE status = ? {order}", (status,)
            )
        else:
            cursor = await self.db.execute(f"SELECT {_COLUMNS} FROM projects {order}")
        return [_row_to_project(r) for r in await cursor.fetchall()]

    async def update(
        self,
        project_id: int,
        *,
        repos: list[str] | None = None,
        settings: dict | None = None,
        **fields: str | None,
    ) -> bool:
        """Update whitelisted scalar fields (name/description/color/icon) and/or the
        ``repos``/``settings`` JSON columns. Unknown kwargs are rejected (defensive against
        a typo silently writing nothing). Returns False if the project doesn't exist."""
        unknown = set(fields) - set(_UPDATABLE)
        if unknown:
            raise ValueError(f"cannot update unknown project field(s): {sorted(unknown)}")
        sets: list[str] = []
        params: list[object] = []
        for col in _UPDATABLE:
            if col in fields:
                sets.append(f"{col} = ?")
                params.append(fields[col])
        if repos is not None:
            sets.append("repos_json = ?")
            params.append(json.dumps(list(repos)))
        if settings is not None:
            sets.append("settings_json = ?")
            params.append(json.dumps(settings))
        if not sets:
            return await self.get(project_id) is not None  # nothing to change
        sets.append("updated_at = ?")
        params.append(_now())
        params.append(project_id)
        async with self.lock:
            cursor = await self.db.execute(
                f"UPDATE projects SET {', '.join(sets)} WHERE id = ?", tuple(params)
            )
            await self.db.commit()
        return cursor.rowcount > 0

    async def set_status(self, project_id: int, status: str) -> bool:
        """Flip an existing project's lifecycle status. Returns False if unknown."""
        if status not in _STATUSES:
            raise ValueError(f"unknown project status: {status!r}")
        archived_at = _now() if status == "archived" else None
        async with self.lock:
            cursor = await self.db.execute(
                "UPDATE projects SET status = ?, archived_at = ?, updated_at = ? WHERE id = ?",
                (status, archived_at, _now(), project_id),
            )
            await self.db.commit()
        return cursor.rowcount > 0

    async def archive(self, project_id: int) -> bool:
        """Archive a project (status flip + archived_at) — the row is kept, never DELETEd.
        Scoped rows keep their ``project_id``; archiving hides, it does not erase."""
        return await self.set_status(project_id, "archived")

    async def set_pinned(self, project_id: int, pinned: bool) -> bool:
        """Pin/unpin a project for the Projects grid — a display preference, no new authority.
        Returns False if the project doesn't exist."""
        async with self.lock:
            cursor = await self.db.execute(
                "UPDATE projects SET pinned = ?, updated_at = ? WHERE id = ?",
                (1 if pinned else 0, _now(), project_id),
            )
            await self.db.commit()
        return cursor.rowcount > 0
