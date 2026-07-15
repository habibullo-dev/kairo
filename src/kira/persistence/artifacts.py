"""ArtifactStore: a searchable record of the things Kira produces (schema v9).

An *artifact* is a digest, an orchestration run summary, an eval report, a wiki page, a
meeting note, a generated design — anything worth finding again. Plain SQL like the other
stores, on the shared connection + write lock.

Two safety properties are enforced *here*, at registration, not by callers:

* **Path confinement.** A ``local_path`` artifact must resolve under one of the managed roots
  (``data/artifacts`` and the existing wiki/eval roots) and must not be a sensitive path
  (``is_sensitive_path`` — ``.env``, ``data/connectors/``, keys, …). The same
  :meth:`ArtifactStore.content_path` is reused by the content route so serving and
  registration can never disagree. External-URI artifacts (Google Stitch, DB-backed
  deep-links) have no servable file and skip this.
* **XOR of ``local_path`` / ``external_uri``.** Exactly one; a DB CHECK backs it up.

Identity + dedupe is ``(origin_type, origin_id)``; ``content_hash`` is a NON-UNIQUE version
fingerprint. Re-registering the same origin updates the row in place (a wiki page edited →
same artifact, new version). For an origin-less artifact, identical content within the same
producer + project dedupes on the hash.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import json
from dataclasses import dataclass
from pathlib import Path

import aiosqlite

from kira.paths import is_sensitive_path, resolve_path
from kira.persistence.db import transaction
from kira.persistence.fts import ANY_PROJECT

_COLUMNS = (
    "id, project_id, kind, title, local_path, external_uri, content_hash, origin_type, "
    "origin_id, created_by, team, role, model, sensitivity, provenance_class, labels_json, "
    "pinned, created_at, updated_at"
)

_CREATED_BY = ("user", "agent", "system")

# Whitelisted scalar columns update() may set (SET clause built from these constants only).
# pinned + labels are handled explicitly (like projects' repos/settings), not here.
_UPDATABLE = ("title", "kind", "sensitivity", "provenance_class")
# Of those, the columns the schema declares NOT NULL: update() refuses to set them to NULL, or
# the failed write would leave the shared connection mid-transaction (poisoning later writes).
_NOT_NULL_UPDATABLE = ("title", "kind")


class ArtifactPathError(ValueError):
    """A local artifact path escaped the managed roots or is a sensitive path."""


def _now() -> str:
    return _dt.datetime.now(_dt.UTC).isoformat()


def _blank_to_none(value: str | Path | None) -> str | Path | None:
    """Empty/whitespace-only strings count as absent, so the local_path/external_uri XOR can't
    be satisfied by a blank (a sync helper: pathlib/str methods on a union type trip ASYNC240
    inside an async function)."""
    if isinstance(value, str) and not value.strip():
        return None
    return value


@dataclass(frozen=True)
class Artifact:
    """One row of ``artifacts``. ``labels`` is the decoded ``labels_json``; ``local_path`` is
    stored relative to the data dir (resolve via :meth:`ArtifactStore.content_path`)."""

    id: int
    project_id: int | None
    kind: str
    title: str
    local_path: str | None
    external_uri: str | None
    content_hash: str | None
    origin_type: str
    origin_id: str | None
    created_by: str
    team: str | None
    role: str | None
    model: str | None
    sensitivity: str | None
    provenance_class: str | None
    labels: tuple[str, ...]
    pinned: bool
    created_at: str
    updated_at: str


def _row_to_artifact(row: tuple) -> Artifact:
    return Artifact(
        id=row[0],
        project_id=row[1],
        kind=row[2],
        title=row[3],
        local_path=row[4],
        external_uri=row[5],
        content_hash=row[6],
        origin_type=row[7],
        origin_id=row[8],
        created_by=row[9],
        team=row[10],
        role=row[11],
        model=row[12],
        sensitivity=row[13],
        provenance_class=row[14],
        labels=tuple(json.loads(row[15]) if row[15] else []),
        pinned=bool(row[16]),
        created_at=row[17],
        updated_at=row[18],
    )


class ArtifactStore:
    def __init__(
        self,
        db: aiosqlite.Connection,
        lock: asyncio.Lock | None = None,
        *,
        data_dir: Path,
        managed_roots: dict[str, Path],
    ) -> None:
        self.db = db
        self.lock = lock or asyncio.Lock()
        self.data_dir = data_dir.resolve()
        # Resolved managed roots a local artifact may live under (data/artifacts, wiki, evals).
        self._roots = tuple(r.resolve() for r in managed_roots.values())

    # --- path confinement (shared by register + the content route) --------------------
    def _confine(self, raw_path: str | Path) -> Path:
        """Resolve ``raw_path`` (relative paths against the data dir), then refuse it unless it
        lives under a managed root and is not sensitive. Raises :class:`ArtifactPathError`."""
        target = resolve_path(raw_path, self.data_dir)
        if not any(target == r or target.is_relative_to(r) for r in self._roots):
            raise ArtifactPathError(f"artifact path escapes the managed roots: {target}")
        if is_sensitive_path(target):
            raise ArtifactPathError(f"refusing a sensitive artifact path: {target}")
        return target

    def _stored_rel(self, raw_path: str | Path) -> str:
        """Confine then express the path relative to the data dir (how it is persisted)."""
        target = self._confine(raw_path)
        try:
            return target.relative_to(self.data_dir).as_posix()
        except ValueError:
            return target.as_posix()  # a managed root outside the data dir (unusual)

    def content_path(self, artifact: Artifact) -> Path | None:
        """The absolute, re-confined path to serve for a local artifact, or ``None`` for an
        external-URI artifact. Re-runs the full confinement (defence in depth even though
        registration already checked). Raises :class:`ArtifactPathError` on a violation."""
        if artifact.local_path is None:
            return None
        return self._confine(artifact.local_path)

    # --- registration -----------------------------------------------------------------
    async def register(
        self,
        *,
        origin_type: str,
        kind: str,
        title: str,
        created_by: str,
        origin_id: str | None = None,
        local_path: str | Path | None = None,
        external_uri: str | None = None,
        content_hash: str | None = None,
        project_id: int | None = None,
        team: str | None = None,
        role: str | None = None,
        model: str | None = None,
        sensitivity: str | None = None,
        provenance_class: str | None = None,
        labels: list[str] | None = None,
    ) -> int:
        """Register (or dedupe/version) an artifact; returns its id.

        Dedupe order: by ``(origin_type, origin_id)`` → update the row in place; else by
        ``content_hash`` → return the existing row; else insert. A local file is confined +
        sensitivity-refused before anything is written.
        """
        # Empty/whitespace-only strings are treated as absent, so the XOR (and DB CHECK) can't
        # be satisfied by a content-less, reference-less artifact (e.g. external_uri="").
        local_path = _blank_to_none(local_path)
        external_uri = _blank_to_none(external_uri)
        if (local_path is None) == (external_uri is None):
            raise ArtifactPathError("exactly one of local_path / external_uri is required")
        if created_by not in _CREATED_BY:
            raise ValueError(f"unknown created_by: {created_by!r}")
        stored_local = self._stored_rel(local_path) if local_path is not None else None
        labels_json = json.dumps(list(labels or []))
        now = _now()
        async with transaction(self.db, self.lock):
            if origin_id is not None:
                cur = await self.db.execute(
                    "SELECT id FROM artifacts WHERE origin_type = ? AND origin_id = ?",
                    (origin_type, origin_id),
                )
                row = await cur.fetchone()
                if row is not None:
                    await self.db.execute(
                        "UPDATE artifacts SET kind = ?, title = ?, local_path = ?, "
                        "external_uri = ?, content_hash = ?, project_id = ?, team = ?, "
                        "role = ?, model = ?, sensitivity = ?, provenance_class = ?, "
                        "labels_json = ?, updated_at = ? WHERE id = ?",
                        (
                            kind, title, stored_local, external_uri, content_hash, project_id,
                            team, role, model, sensitivity, provenance_class, labels_json,
                            now, row[0],
                        ),
                    )
                    return int(row[0])
            if origin_id is None and content_hash is not None:
                # Best-effort dedupe for origin-less artifacts ONLY: identical content within
                # the same producer + project. Scoped so it can never return a different
                # project's / origin's row (content_hash is deliberately not unique).
                cur = await self.db.execute(
                    "SELECT id FROM artifacts WHERE content_hash = ? AND origin_type = ? "
                    "AND project_id IS ?",
                    (content_hash, origin_type, project_id),
                )
                row = await cur.fetchone()
                if row is not None:
                    return int(row[0])
            cursor = await self.db.execute(
                f"INSERT INTO artifacts ({_COLUMNS}) VALUES "
                "(NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)",
                (
                    project_id, kind, title, stored_local, external_uri, content_hash,
                    origin_type, origin_id, created_by, team, role, model, sensitivity,
                    provenance_class, labels_json, now, now,
                ),
            )
            new_id = cursor.lastrowid
        assert new_id is not None
        return new_id

    # --- reads ------------------------------------------------------------------------
    async def get(self, artifact_id: int) -> Artifact | None:
        cursor = await self.db.execute(
            f"SELECT {_COLUMNS} FROM artifacts WHERE id = ?", (artifact_id,)
        )
        row = await cursor.fetchone()
        return _row_to_artifact(row) if row else None

    async def list(
        self,
        *,
        project_id: object = ANY_PROJECT,
        include_global: bool = True,
        kind: str | None = None,
        pinned: bool | None = None,
        limit: int = 50,
    ) -> list[Artifact]:
        clauses: list[str] = []
        params: list[object] = []
        if kind is not None:
            clauses.append("kind = ?")
            params.append(kind)
        if pinned is not None:
            clauses.append("pinned = ?")
            params.append(1 if pinned else 0)
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
            f"SELECT {_COLUMNS} FROM artifacts {where} ORDER BY pinned DESC, id DESC LIMIT ?",
            (*params, limit),
        )
        return [_row_to_artifact(r) for r in await cursor.fetchall()]

    # --- metadata mutations (pin / label / scalar edit) -------------------------------
    async def _write(self, sql: str, params: tuple) -> bool:
        """One single-statement write, with rollback on failure. A failed write must never
        leave the shared aiosqlite connection mid-transaction — that would make the next
        transaction() (BEGIN IMMEDIATE) fail and poison writes across every store."""
        async with self.lock:
            try:
                cursor = await self.db.execute(sql, params)
                await self.db.commit()
            except BaseException:
                await self.db.rollback()
                raise
        return cursor.rowcount > 0

    async def set_pinned(self, artifact_id: int, pinned: bool) -> bool:
        return await self._write(
            "UPDATE artifacts SET pinned = ?, updated_at = ? WHERE id = ?",
            (1 if pinned else 0, _now(), artifact_id),
        )

    async def set_labels(self, artifact_id: int, labels: list[str]) -> bool:
        return await self._write(
            "UPDATE artifacts SET labels_json = ?, updated_at = ? WHERE id = ?",
            (json.dumps(list(labels)), _now(), artifact_id),
        )

    async def update(self, artifact_id: int, **fields: str | None) -> bool:
        """Update whitelisted scalar fields (title/kind/sensitivity/provenance_class). Unknown
        kwargs are rejected; NULL is refused for the NOT NULL columns. Pin/labels use their
        dedicated methods."""
        unknown = set(fields) - set(_UPDATABLE)
        if unknown:
            raise ValueError(f"cannot update unknown artifact field(s): {sorted(unknown)}")
        for col in _NOT_NULL_UPDATABLE:
            if col in fields and fields[col] is None:
                raise ValueError(f"artifact {col} cannot be null")
        sets: list[str] = []
        params: list[object] = []
        for col in _UPDATABLE:
            if col in fields:
                sets.append(f"{col} = ?")
                params.append(fields[col])
        if not sets:
            return await self.get(artifact_id) is not None
        sets.append("updated_at = ?")
        params.append(_now())
        params.append(artifact_id)
        return await self._write(
            f"UPDATE artifacts SET {', '.join(sets)} WHERE id = ?", tuple(params)
        )
