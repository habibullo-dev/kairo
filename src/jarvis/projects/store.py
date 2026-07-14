"""ProjectStore: SQLite persistence for project workspaces (schema v7).

Plain SQL like the other stores, on the *same* aiosqlite connection and shared write
lock (a second lock lets a write land inside another store's open transaction). Nothing is ever
DELETEd — ``archive`` is a status flip, and
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
from dataclasses import dataclass, replace
from typing import Literal

import aiosqlite

from jarvis.persistence.db import transaction

_COLUMNS = (
    "id, name, slug, description, status, color, icon, repos_json, settings_json, "
    "created_at, updated_at, archived_at, pinned"
)

_STATUSES = ("active", "paused", "archived")

# Columns update() will set, mapped to how the value is serialized. Whitelisted so the
# SET clause is built from constants, never caller-supplied column names.
_UPDATABLE = ("name", "description", "color", "icon")

ServiceSelectionCheck = Literal["ready", "unchanged", "conflict", "missing"]
ServiceSelectionUpdate = Literal["updated", "unchanged", "conflict", "missing"]


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


@dataclass(frozen=True)
class ProjectReset:
    """Durable lineage for one archive-and-successor project reset."""

    predecessor_id: int
    successor_id: int
    retained_repositories: bool
    created_at: str


class ProjectResetError(RuntimeError):
    """The requested project cannot be reset from its current lifecycle state."""


class ProjectResetBusyError(ProjectResetError):
    """Project reset is blocked because externally consequential work is still in flight."""


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

    async def set_label(self, project_id: int, label: str | None) -> bool:
        """Set (or clear, when ``label`` is falsy) the project's category label WITHIN
        settings_json, without disturbing other settings (model routes/budgets/roster). A
        read-modify-write under the lock so a label edit can never clobber sibling overrides.
        Returns False if the project doesn't exist."""
        async with self.lock:
            p = await self.get(project_id)
            if p is None:
                return False
            settings = dict(p.settings)
            if label:
                settings["label"] = label
            else:
                settings.pop("label", None)
            cursor = await self.db.execute(
                "UPDATE projects SET settings_json = ?, updated_at = ? WHERE id = ?",
                (json.dumps(settings), _now(), project_id),
            )
            await self.db.commit()
        return cursor.rowcount > 0

    async def set_services(self, project_id: int, services: list[str] | None) -> bool:
        """Set (or clear, when ``services`` is None) the project's service narrowing WITHIN
        settings_json, without disturbing sibling overrides (model routes/budgets/roster/label).
        The caller (the route) enforces the narrow-only subset invariant; this just persists the
        list. Read-modify-write under the lock. Returns False if the project doesn't exist."""
        result = await self.compare_and_set_services(project_id, services)
        return result in {"updated", "unchanged"}

    async def compare_and_set_services(
        self,
        project_id: int,
        services: list[str] | None,
        *,
        expected_services: list[str] | None = None,
        expected_provided: bool = False,
    ) -> ServiceSelectionUpdate:
        """Atomically apply one canonical service selection.

        ``None`` is inherited global access, while ``[]`` is an explicit deny-all selection.
        When an expected value is provided, a concurrent change returns ``"conflict"`` instead
        of silently overwriting it.  An already-applied desired value wins before that comparison,
        making a lost-response retry idempotently successful.
        """
        result, _project = await self.compare_and_set_services_with_project(
            project_id,
            services,
            expected_services=expected_services,
            expected_provided=expected_provided,
        )
        return result

    async def compare_and_set_services_with_project(
        self,
        project_id: int,
        services: list[str] | None,
        *,
        expected_services: list[str] | None = None,
        expected_provided: bool = False,
    ) -> tuple[ServiceSelectionUpdate, Project | None]:
        """Apply a service CAS and return the exact row snapshot that was committed.

        The UI publishes this snapshot synchronously into live execution contexts. Returning it
        from inside the store lock avoids a fallible second database read after commit, which
        would otherwise permit durable policy and in-memory tool authority to diverge.
        """
        desired = None if services is None else sorted(set(services))
        expected = None if expected_services is None else sorted(set(expected_services))
        async with self.lock:
            p = await self.get(project_id)
            if p is None:
                return "missing", None
            current_valid, current = self._service_selection(p.settings)
            if current_valid and current == desired and self._service_selection_is_canonical(
                p.settings, desired
            ):
                return "unchanged", p
            if expected_provided and (not current_valid or current != expected):
                return "conflict", None
            settings = dict(p.settings)
            if desired is None:
                settings.pop("services", None)
            else:
                settings["services"] = desired
            updated_at = _now()
            cursor = await self.db.execute(
                "UPDATE projects SET settings_json = ?, updated_at = ? WHERE id = ?",
                (json.dumps(settings), updated_at, project_id),
            )
            await self.db.commit()
        if cursor.rowcount <= 0:
            return "missing", None
        return "updated", replace(p, settings=settings, updated_at=updated_at)

    async def check_services(
        self,
        project_id: int,
        services: list[str] | None,
        *,
        expected_services: list[str] | None = None,
        expected_provided: bool = False,
    ) -> ServiceSelectionCheck:
        """Read the same canonical comparison used by :meth:`compare_and_set_services`.

        This non-mutating preflight lets a lost-response retry succeed without waiting for the
        execution barrier.  The later atomic compare-and-set repeats every check before a write.
        """
        desired = None if services is None else sorted(set(services))
        expected = None if expected_services is None else sorted(set(expected_services))
        async with self.lock:
            p = await self.get(project_id)
            if p is None:
                return "missing"
            current_valid, current = self._service_selection(p.settings)
            if current_valid and current == desired and self._service_selection_is_canonical(
                p.settings, desired
            ):
                return "unchanged"
            if expected_provided and (not current_valid or current != expected):
                return "conflict"
            return "ready"

    @staticmethod
    def _service_selection(settings: dict) -> tuple[bool, list[str] | None]:
        saved = settings.get("services")
        if saved is None:
            return True, None
        if isinstance(saved, list) and all(isinstance(name, str) for name in saved):
            return True, sorted(set(saved))
        return False, None

    @staticmethod
    def _service_selection_is_canonical(
        settings: dict, desired: list[str] | None
    ) -> bool:
        if desired is None:
            return "services" not in settings
        return settings.get("services") == desired

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
        async with self.lock:
            archived = await self.archive_in_transaction(project_id)
            await self.db.commit()
        return archived

    async def archive_in_transaction(self, project_id: int) -> bool:
        """Archive one project while the caller owns this store's database transaction."""
        now = _now()
        cursor = await self.db.execute(
            "UPDATE projects SET status = 'archived', archived_at = ?, updated_at = ? WHERE id = ?",
            (now, now, project_id),
        )
        return cursor.rowcount > 0

    async def reset(self, project_id: int, *, retain_repositories: bool) -> ProjectReset | None:
        """Archive one project and create its clean successor atomically.

        Historical chats, memory, knowledge, tasks, reports, ledgers, and artifacts remain scoped
        to the archived predecessor.  The successor receives only display metadata, the narrow
        ``label``/``services`` preferences, and optionally the repository links that Kairo may
        relearn.  No linked repository or shared content-addressed file is touched.
        """
        async with transaction(self.db, self.lock):
            return await self.reset_in_transaction(
                project_id,
                retain_repositories=retain_repositories,
            )

    async def reset_in_transaction(
        self, project_id: int, *, retain_repositories: bool
    ) -> ProjectReset | None:
        """Reset one project while the caller owns this store's database transaction.

        The UI lifecycle route uses this form to create every live workspace's replacement
        session against the still-uncommitted successor. A failed insert therefore rolls back the
        predecessor archive, successor, lineage event, and dormant-capability terminalization.
        """
        now = _now()
        row = await (
            await self.db.execute(f"SELECT {_COLUMNS} FROM projects WHERE id = ?", (project_id,))
        ).fetchone()
        if row is None:
            return None
        predecessor = _row_to_project(row)
        if predecessor.status == "archived":
            raise ProjectResetError("archived projects cannot be reset")

        # A reset may terminalize work that has not started, but it must never race a model,
        # connector, task, or assessment already executing outside SQLite.  Those workers
        # finish/stop through their ordinary lifecycle before the owner retries.
        blockers = await (
            await self.db.execute(
                "SELECT "
                "EXISTS(SELECT 1 FROM task_runs r JOIN tasks t ON t.id = r.task_id "
                "       WHERE t.project_id = ? AND r.status = 'running'), "
                "EXISTS(SELECT 1 FROM orchestration_runs "
                "       WHERE project_id = ? AND status = 'running'), "
                "EXISTS(SELECT 1 FROM analysis_jobs "
                "       WHERE project_id = ? AND state = 'running'), "
                "EXISTS(SELECT 1 FROM write_intents "
                "       WHERE project_id = ? AND state = 'approved')",
                (project_id, project_id, project_id, project_id),
            )
        ).fetchone()
        if blockers is None or any(bool(value) for value in blockers):
            raise ProjectResetBusyError("project has in-flight work")

        slug = await self._unique_slug(predecessor.name)
        preserved_settings = {
            key: predecessor.settings[key]
            for key in ("label", "services")
            if key in predecessor.settings
        }
        cursor = await self.db.execute(
            "INSERT INTO projects (name, slug, description, status, color, icon, "
            "repos_json, settings_json, created_at, updated_at, pinned) "
            "VALUES (?, ?, ?, 'active', ?, ?, ?, ?, ?, ?, ?)",
            (
                predecessor.name,
                slug,
                predecessor.description,
                predecessor.color,
                predecessor.icon,
                json.dumps(list(predecessor.repos) if retain_repositories else []),
                json.dumps(preserved_settings),
                now,
                now,
                1 if predecessor.pinned else 0,
            ),
        )
        assert cursor.lastrowid is not None
        successor_id = int(cursor.lastrowid)
        archived = await self.db.execute(
            "UPDATE projects SET status = 'archived', archived_at = ?, updated_at = ? "
            "WHERE id = ? AND status <> 'archived'",
            (now, now, project_id),
        )
        if archived.rowcount != 1:
            raise ProjectResetError("project reset lost its lifecycle race")

        # Terminalize every dormant capability that could otherwise wake against the
        # archived predecessor. Historical successes and already-executed journals remain
        # untouched. All of these transitions commit or roll back with the project lineage.
        await self.db.execute(
            "UPDATE tasks SET status = 'cancelled', next_run_at = NULL, updated_at = ? "
            "WHERE project_id = ? AND status = 'active'",
            (now, project_id),
        )
        await self.db.execute(
            "UPDATE write_intents SET state = 'rejected', decided_at = ?, updated_at = ? "
            "WHERE project_id = ? AND state IN ('draft', 'previewed')",
            (now, now, project_id),
        )
        await self.db.execute(
            "UPDATE graph_suggestions SET status = 'rejected', resolved_at = ?, "
            "resolved_by = 'project_reset' WHERE project_id = ? AND status = 'pending'",
            (now, project_id),
        )
        await self.db.execute(
            "UPDATE attention_items SET state = 'expired', updated_at = ?, resolved_at = ?, "
            "snooze_until = NULL WHERE project_id = ? AND state IN ('open', 'snoozed')",
            (now, now, project_id),
        )
        await self.db.execute(
            "UPDATE remote_operator_tokens SET consumed_at = ?, resolution = 'deny' "
            "WHERE consumed_at IS NULL AND subject_type = 'proposal' AND subject_id IN "
            "(SELECT id FROM remote_operator_proposals WHERE project_id = ?)",
            (now, project_id),
        )
        await self.db.execute(
            "UPDATE remote_operator_proposals SET state = 'cancelled', resolved_at = ?, "
            "updated_at = ? WHERE project_id = ? "
            "AND state IN ('pending', 'approved', 'queued')",
            (now, now, project_id),
        )
        await self.db.execute(
            "UPDATE analysis_jobs SET state = 'discarded', updated_at = ? "
            "WHERE project_id = ? AND state = 'queued'",
            (now, project_id),
        )
        await self.db.execute(
            "UPDATE orchestration_runs SET resume_state = 'none', resume_checkpoint_json = '{}' "
            "WHERE project_id = ? AND resume_state = 'ready'",
            (project_id,),
        )
        await self.db.execute(
            "INSERT INTO project_reset_events "
            "(predecessor_project_id, successor_project_id, retained_repositories, created_at) "
            "VALUES (?, ?, ?, ?)",
            (project_id, successor_id, 1 if retain_repositories else 0, now),
        )
        return ProjectReset(project_id, successor_id, retain_repositories, now)

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
