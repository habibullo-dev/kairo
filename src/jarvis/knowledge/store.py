"""KnowledgeStore: SQLite persistence for the knowledge base (schema v4).

Three tables, two trust levels:

* ``kb_sources`` is a **primary/audit record** — never DELETEd. A re-ingest of
  changed content ``supersede``s the prior row (lineage kept); a rejected
  unattended ingest is marked ``rejected`` (kept for audit, invisible to search).
* ``kb_chunks`` and ``kb_wiki_links`` are **derived indexes** — rebuildable caches
  over the markdown artifacts and wiki files, and the one deliberate exception to
  the never-DELETE rule: :meth:`replace_chunks` / :meth:`replace_links` delete and
  re-insert per owner inside a single ``transaction()`` (see docs/decisions/0004-*).

Vectors follow the memory store exactly: unit-normalized ``float32`` BLOBs, cosine
= one numpy matmul over the candidate matrix, filtered by ``embedding_model`` so a
model switch never silently compares across embedding spaces. Retrieval excludes
superseded/rejected sources and — by default — ``unreviewed`` ones (unattended
ingests are quarantined until a human runs ``kb review``); wiki-page chunks are
always eligible (a page on disk is curated, human-facing content).

Runs on the *same* aiosqlite connection and shared write lock as the other stores
(a second connection to one file deadlocks; the shared lock keeps a knowledge
write from landing inside another store's open transaction — see
:mod:`jarvis.persistence.db`).
"""

from __future__ import annotations

import asyncio
import datetime as _dt
from dataclasses import dataclass, field

import aiosqlite
import numpy as np

from jarvis.persistence.db import transaction

_SOURCE_COLUMNS = (
    "id, kind, origin, title, content_hash, raw_path, markdown_path, markdown_hash, "
    "converter, converter_version, byte_size, mime, status, superseded_by, review_status, "
    "created_by, source_session_id, created_at, updated_at, project_id"
)

#: Sentinel for "any project" (no scope filter) in search — distinct from ``None`` (global
#: only, project_id IS NULL). Mirrors the memory store's scope contract.
ANY_PROJECT: object = object()
_CHUNK_COLUMNS = (
    "id, source_id, wiki_path, heading_path, seq, text, embedding, embedding_model, created_at"
)


def _now() -> str:
    return _dt.datetime.now(_dt.UTC).isoformat()


def _to_unit_blob(vec: np.ndarray | list[float]) -> tuple[bytes, np.ndarray]:
    """Normalize ``vec`` to a unit float32 vector; return (blob, unit_vector).

    A zero vector is stored as-is (never a nearest neighbor) rather than dividing
    by zero — same rule as the memory store."""
    arr = np.asarray(vec, dtype=np.float32)
    norm = float(np.linalg.norm(arr))
    unit = arr / norm if norm > 0 else arr
    return unit.tobytes(), unit


def _from_blob(blob: bytes) -> np.ndarray:
    return np.frombuffer(blob, dtype=np.float32)


@dataclass(frozen=True)
class Source:
    """One row of ``kb_sources`` — an ingested source with provenance."""

    id: int
    kind: str  # 'file' | 'url' | 'note'
    origin: str
    title: str | None
    content_hash: str
    raw_path: str
    markdown_path: str
    markdown_hash: str
    converter: str
    converter_version: str
    byte_size: int
    mime: str | None
    status: str  # 'live' | 'superseded' | 'rejected'
    superseded_by: int | None
    review_status: str  # 'reviewed' | 'unreviewed'
    created_by: str  # 'user' | 'agent'
    source_session_id: int | None
    created_at: str
    updated_at: str
    project_id: int | None = None  # Phase 10: scope (None == global)


@dataclass(frozen=True)
class NewChunk:
    """A chunk to insert (no id/owner yet — the store assigns the owner)."""

    heading_path: str
    seq: int
    text: str
    embedding: np.ndarray | list[float]


@dataclass
class Chunk:
    """One row of ``kb_chunks`` (embedding as a numpy unit vector)."""

    id: int
    source_id: int | None
    wiki_path: str | None
    heading_path: str
    seq: int
    text: str
    embedding: np.ndarray
    embedding_model: str
    created_at: str


@dataclass(frozen=True)
class ScoredChunk:
    """A chunk plus its cosine score and the citation context it needs at render
    time (denormalized from the source join; source fields are None for wiki chunks)."""

    chunk: Chunk
    score: float
    source_kind: str | None
    source_origin: str | None
    source_title: str | None
    source_created_by: str | None
    source_created_at: str | None


@dataclass(frozen=True)
class WikiLink:
    """A derived link from one wiki page to another (or an unresolved target)."""

    from_path: str
    to_path: str | None  # None ⇒ broken/unresolved
    to_raw: str
    link_text: str | None
    link_kind: str  # 'wikilink' | 'markdown'


@dataclass
class KnowledgeStore:
    db: aiosqlite.Connection
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)  # share with SessionStore

    # --- sources -----------------------------------------------------------

    async def add_source(
        self,
        *,
        kind: str,
        origin: str,
        title: str | None,
        content_hash: str,
        raw_path: str,
        markdown_path: str,
        markdown_hash: str,
        converter: str,
        converter_version: str,
        byte_size: int,
        mime: str | None = None,
        review_status: str = "reviewed",
        created_by: str,
        source_session_id: int | None = None,
        project_id: int | None = None,
    ) -> int:
        """Insert a live source; returns its id. ``project_id`` scopes it (None == global);
        retrieval only surfaces sources in the querying project's scope (Phase 10 A1)."""
        now = _now()
        async with self.lock:
            cursor = await self.db.execute(
                f"INSERT INTO kb_sources ({_SOURCE_COLUMNS}) VALUES "
                "(NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'live', NULL, ?, ?, ?, ?, ?, ?)",
                (
                    kind,
                    origin,
                    title,
                    content_hash,
                    raw_path,
                    markdown_path,
                    markdown_hash,
                    converter,
                    converter_version,
                    byte_size,
                    mime,
                    review_status,
                    created_by,
                    source_session_id,
                    now,
                    now,
                    project_id,
                ),
            )
            await self.db.commit()
        assert cursor.lastrowid is not None
        return cursor.lastrowid

    async def get_source(self, source_id: int) -> Source | None:
        """Fetch a source of *any* status by id."""
        cursor = await self.db.execute(
            f"SELECT {_SOURCE_COLUMNS} FROM kb_sources WHERE id=?", (source_id,)
        )
        row = await cursor.fetchone()
        return _row_to_source(row) if row else None

    async def find_by_hash(self, content_hash: str) -> Source | None:
        """The source with this exact raw-bytes hash, if already ingested (any status)."""
        cursor = await self.db.execute(
            f"SELECT {_SOURCE_COLUMNS} FROM kb_sources WHERE content_hash=?", (content_hash,)
        )
        row = await cursor.fetchone()
        return _row_to_source(row) if row else None

    async def find_live_by_origin(self, origin: str) -> Source | None:
        """The current live source for an origin (for re-ingest supersede)."""
        cursor = await self.db.execute(
            f"SELECT {_SOURCE_COLUMNS} FROM kb_sources WHERE origin=? AND status='live' "
            "ORDER BY id DESC LIMIT 1",
            (origin,),
        )
        row = await cursor.fetchone()
        return _row_to_source(row) if row else None

    async def supersede_source(self, old_id: int, new_id: int) -> None:
        """Mark ``old_id`` superseded by ``new_id`` (keeps lineage; drops from search)."""
        async with self.lock:
            await self.db.execute(
                "UPDATE kb_sources SET status='superseded', superseded_by=?, updated_at=? "
                "WHERE id=?",
                (new_id, _now(), old_id),
            )
            await self.db.commit()

    async def set_review_status(self, source_id: int, review_status: str) -> None:
        """Promote a source to 'reviewed' (or back to 'unreviewed')."""
        async with self.lock:
            await self.db.execute(
                "UPDATE kb_sources SET review_status=?, updated_at=? WHERE id=?",
                (review_status, _now(), source_id),
            )
            await self.db.commit()

    async def reject_source(self, source_id: int) -> bool:
        """Mark a source 'rejected' (kept for audit, invisible to search). Returns
        True if a non-terminal source was rejected."""
        async with self.lock:
            cursor = await self.db.execute(
                "UPDATE kb_sources SET status='rejected', updated_at=? "
                "WHERE id=? AND status != 'rejected'",
                (_now(), source_id),
            )
            await self.db.commit()
        return cursor.rowcount > 0

    async def list_sources(
        self, *, status: str | None = "live", review_status: str | None = None
    ) -> list[Source]:
        """List sources, filtered by status (default live) and/or review_status."""
        clauses, params = [], []
        if status is not None:
            clauses.append("status=?")
            params.append(status)
        if review_status is not None:
            clauses.append("review_status=?")
            params.append(review_status)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        cursor = await self.db.execute(
            f"SELECT {_SOURCE_COLUMNS} FROM kb_sources {where} ORDER BY id", params
        )
        return [_row_to_source(r) for r in await cursor.fetchall()]

    # --- chunks (derived index: delete-and-replace per owner) --------------

    async def replace_chunks(
        self,
        *,
        source_id: int | None = None,
        wiki_path: str | None = None,
        chunks: list[NewChunk],
        embedding_model: str,
    ) -> None:
        """Atomically replace all chunks for one owner (a source *or* a wiki page).

        Delete + re-insert in one ``transaction()`` — a failure mid-way rolls back,
        leaving the prior chunks intact (they are a rebuildable cache, but a torn
        rebuild would silently drop retrieval). Exactly one of ``source_id`` /
        ``wiki_path`` must be given (the table's owner CHECK enforces it too)."""
        if (source_id is None) == (wiki_path is None):
            raise ValueError("replace_chunks needs exactly one of source_id / wiki_path")
        now = _now()
        rows = [
            (
                source_id,
                wiki_path,
                c.heading_path,
                c.seq,
                c.text,
                _to_unit_blob(c.embedding)[0],
                embedding_model,
                now,
            )
            for c in chunks
        ]
        col = "source_id" if source_id is not None else "wiki_path"
        owner = source_id if source_id is not None else wiki_path
        async with transaction(self.db, self.lock):
            await self.db.execute(f"DELETE FROM kb_chunks WHERE {col}=?", (owner,))
            if rows:
                await self.db.executemany(
                    "INSERT INTO kb_chunks (source_id, wiki_path, heading_path, seq, text, "
                    "embedding, embedding_model, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    rows,
                )

    async def chunks_for_source(self, source_id: int) -> list[Chunk]:
        cursor = await self.db.execute(
            f"SELECT {_CHUNK_COLUMNS} FROM kb_chunks WHERE source_id=? ORDER BY seq", (source_id,)
        )
        return [_row_to_chunk(r) for r in await cursor.fetchall()]

    async def chunk_count(self) -> int:
        cursor = await self.db.execute("SELECT COUNT(*) FROM kb_chunks")
        (n,) = await cursor.fetchone()
        return n

    async def foreign_model_chunks(self, embedding_model: str) -> int:
        """Count chunks embedded with a *different* model (need re-embed via rebuild)."""
        cursor = await self.db.execute(
            "SELECT COUNT(*) FROM kb_chunks WHERE embedding_model != ?", (embedding_model,)
        )
        (n,) = await cursor.fetchone()
        return n

    async def wiki_paths_with_chunks(self) -> set[str]:
        cursor = await self.db.execute(
            "SELECT DISTINCT wiki_path FROM kb_chunks WHERE wiki_path IS NOT NULL"
        )
        return {r[0] for r in await cursor.fetchall()}

    async def search(
        self,
        query_vec: np.ndarray | list[float],
        embedding_model: str,
        *,
        top_k: int,
        min_similarity: float,
        include_unreviewed: bool = False,
        project_id: object = ANY_PROJECT,
    ) -> list[ScoredChunk]:
        """Top-k chunks (same embedding model) with cosine ≥ ``min_similarity``.

        Excludes chunks of superseded/rejected sources and — unless
        ``include_unreviewed`` — of unreviewed sources. Wiki-page chunks (no source)
        are always eligible: a page on disk is curated content.

        ``project_id`` scopes SOURCE chunks (Phase 10 A1): an int P admits P's sources plus
        global (project_id IS NULL); ``None`` admits only global sources; ``ANY_PROJECT``
        (default) does not filter. Wiki-page chunks stay eligible in every scope — they are
        curated, project-agnostic pages (chunk-level project scoping is a documented follow-up)."""
        review_ok = "1" if include_unreviewed else "(s.review_status = 'reviewed')"
        params: list[object] = [embedding_model]
        if project_id is ANY_PROJECT:
            scope = "1"
        elif project_id is None:
            scope = "s.project_id IS NULL"
        else:
            scope = "(s.project_id = ? OR s.project_id IS NULL)"
            params.append(project_id)
        cursor = await self.db.execute(
            f"SELECT {_prefixed(_CHUNK_COLUMNS, 'c')}, "
            "s.kind, s.origin, s.title, s.created_by, s.created_at "
            "FROM kb_chunks c LEFT JOIN kb_sources s ON c.source_id = s.id "
            "WHERE c.embedding_model = ? AND ("
            f"  c.wiki_path IS NOT NULL OR (s.status = 'live' AND {review_ok} AND {scope})"
            ")",
            tuple(params),
        )
        rows = await cursor.fetchall()
        if not rows:
            return []
        chunks = [_row_to_chunk(r[:9]) for r in rows]
        matrix = np.vstack([c.embedding for c in chunks])  # rows already unit vectors
        _, q_unit = _to_unit_blob(query_vec)
        scores = matrix @ q_unit
        order = np.argsort(-scores)[:top_k]
        return [
            ScoredChunk(
                chunk=chunks[i],
                score=float(scores[i]),
                source_kind=rows[i][9],
                source_origin=rows[i][10],
                source_title=rows[i][11],
                source_created_by=rows[i][12],
                source_created_at=rows[i][13],
            )
            for i in order
            if scores[i] >= min_similarity
        ]

    # --- wiki links (derived index: delete-and-replace per page) -----------

    async def replace_links(self, from_path: str, links: list[WikiLink]) -> None:
        """Atomically replace all outbound links for one wiki page."""
        now = _now()
        rows = [(from_path, ln.to_path, ln.to_raw, ln.link_text, ln.link_kind, now) for ln in links]
        async with transaction(self.db, self.lock):
            await self.db.execute("DELETE FROM kb_wiki_links WHERE from_path=?", (from_path,))
            if rows:
                await self.db.executemany(
                    "INSERT INTO kb_wiki_links (from_path, to_path, to_raw, link_text, "
                    "link_kind, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                    rows,
                )

    async def links_from(self, from_path: str) -> list[WikiLink]:
        cursor = await self.db.execute(
            "SELECT from_path, to_path, to_raw, link_text, link_kind FROM kb_wiki_links "
            "WHERE from_path=? ORDER BY id",
            (from_path,),
        )
        return [_row_to_link(r) for r in await cursor.fetchall()]

    async def all_links(self) -> list[WikiLink]:
        cursor = await self.db.execute(
            "SELECT from_path, to_path, to_raw, link_text, link_kind FROM kb_wiki_links ORDER BY id"
        )
        return [_row_to_link(r) for r in await cursor.fetchall()]

    async def backlinks(self, to_path: str) -> list[str]:
        """Wiki pages that link *to* ``to_path`` (for orphan detection + backlinks)."""
        cursor = await self.db.execute(
            "SELECT DISTINCT from_path FROM kb_wiki_links WHERE to_path=? ORDER BY from_path",
            (to_path,),
        )
        return [r[0] for r in await cursor.fetchall()]


def _prefixed(columns: str, alias: str) -> str:
    """'a, b' -> 'x.a, x.b' — qualify a column list with a table alias for a join."""
    return ", ".join(f"{alias}.{c.strip()}" for c in columns.split(","))


def _row_to_source(row: tuple) -> Source:
    return Source(
        id=row[0],
        kind=row[1],
        origin=row[2],
        title=row[3],
        content_hash=row[4],
        raw_path=row[5],
        markdown_path=row[6],
        markdown_hash=row[7],
        converter=row[8],
        converter_version=row[9],
        byte_size=row[10],
        mime=row[11],
        status=row[12],
        superseded_by=row[13],
        review_status=row[14],
        created_by=row[15],
        source_session_id=row[16],
        created_at=row[17],
        updated_at=row[18],
        project_id=row[19],
    )


def _row_to_chunk(row: tuple) -> Chunk:
    return Chunk(
        id=row[0],
        source_id=row[1],
        wiki_path=row[2],
        heading_path=row[3],
        seq=row[4],
        text=row[5],
        embedding=_from_blob(row[6]),
        embedding_model=row[7],
        created_at=row[8],
    )


def _row_to_link(row: tuple) -> WikiLink:
    return WikiLink(
        from_path=row[0], to_path=row[1], to_raw=row[2], link_text=row[3], link_kind=row[4]
    )
