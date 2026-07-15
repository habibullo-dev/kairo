"""MemoryStore: the SQLite + numpy persistence layer for long-term memory.

The data model (schema v2) is deliberately plain SQL — no ORM — so it stays
visible. Two design points carry weight:

* **Vectors are stored unit-normalized** (``float32`` BLOB via numpy). Cosine
  similarity is then just a dot product, so :meth:`search` is one vectorized
  matmul over the live matrix — milliseconds at personal-assistant scale
  (<100k rows). The upgrade path if that ever changes is sqlite-vec.
* **Nothing is ever deleted.** ``supersede`` marks the loser (keeping lineage);
  ``forget`` marks a row ``forgotten`` (gone from recall, kept for audit). Only
  ``status='live'`` rows are retrievable — but any row stays fetchable by id, so
  "what did I forget, and why did I believe that?" always has an answer.

Runs on the *same* aiosqlite connection as :class:`~kira.persistence.sessions.SessionStore`
(a second connection to one file would deadlock on the first concurrent write) —
and, since Phase 3, on the same shared write lock, so a memory write can never
land inside another store's open transaction (see :mod:`kira.persistence.db`).
"""

from __future__ import annotations

import asyncio
import datetime as _dt
from dataclasses import dataclass, field

import aiosqlite
import numpy as np

_COLUMNS = (
    "id, type, content, embedding, embedding_model, source, status, superseded_by, "
    "source_session_id, source_seq_start, source_seq_end, evidence_summary, confidence, "
    "created_at, updated_at, last_accessed_at, access_count, project_id"
)

#: Sentinel for "any project" (no scope filter) in search/all_live — distinct from ``None``,
#: which means the *global* scope (project_id IS NULL). See :func:`_scope_clause`.
ANY_PROJECT: object = object()


def _now() -> str:
    return _dt.datetime.now(_dt.UTC).isoformat()


def _scope_clause(project_id: object, *, include_global: bool) -> tuple[str, list[object]]:
    """SQL fragment + params for a project-scope filter (Phase 10). Two axes:

    * ``project_id`` — ``ANY_PROJECT`` (no filter), ``None`` (global only), or an int P.
    * ``include_global`` — for an int P: True gives ``(project_id = P OR IS NULL)`` (RECALL:
      global memories are visible inside a project); False gives ``project_id = P`` exactly
      (DEDUP: a project write must only ever compare against its own scope, so it can never
      supersede a global memory or another project's — the pre-mortem's cross-scope corruption).

    ``None`` (global scope) is always exact ``IS NULL`` — global recall must not surface any
    project's memories.
    """
    if project_id is ANY_PROJECT:
        return "", []
    if project_id is None:
        return " AND project_id IS NULL", []
    if include_global:
        return " AND (project_id = ? OR project_id IS NULL)", [project_id]
    return " AND project_id = ?", [project_id]


def _to_unit_blob(vec: np.ndarray | list[float]) -> tuple[bytes, np.ndarray]:
    """Normalize ``vec`` to a unit float32 vector; return (blob, unit_vector).

    A zero vector (no signal) is stored as-is rather than dividing by zero — it
    will simply never be a nearest neighbor.
    """
    arr = np.asarray(vec, dtype=np.float32)
    norm = float(np.linalg.norm(arr))
    unit = arr / norm if norm > 0 else arr
    return unit.tobytes(), unit


def _from_blob(blob: bytes) -> np.ndarray:
    return np.frombuffer(blob, dtype=np.float32)


@dataclass(frozen=True)
class Provenance:
    """Why Kira believes a memory — answers 'where did that come from?'."""

    source_session_id: int | None = None
    source_seq_start: int | None = None
    source_seq_end: int | None = None
    evidence_summary: str | None = None
    confidence: float | None = None


@dataclass
class Memory:
    """One row of the ``memories`` table (embedding as a numpy unit vector)."""

    id: int
    type: str
    content: str
    embedding: np.ndarray
    embedding_model: str
    source: str
    status: str
    superseded_by: int | None
    provenance: Provenance
    created_at: str
    updated_at: str
    last_accessed_at: str | None
    access_count: int
    project_id: int | None = None  # Phase 10: scope (None == global)


@dataclass
class ScoredMemory:
    """A memory plus its cosine similarity to a query (search result)."""

    memory: Memory
    score: float


@dataclass
class MemoryStore:
    db: aiosqlite.Connection
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)  # share with SessionStore

    # --- writes ------------------------------------------------------------

    async def add(
        self,
        *,
        type: str,
        content: str,
        embedding: np.ndarray | list[float],
        embedding_model: str,
        source: str,
        provenance: Provenance | None = None,
        project_id: int | None = None,
    ) -> int:
        """Insert a live memory (embedding stored unit-normalized). Returns its id.

        ``project_id`` scopes the memory (None == global). A project session's reflection
        must pass its project id, never NULL, or the memory leaks into global recall."""
        prov = provenance or Provenance()
        blob, _ = _to_unit_blob(embedding)
        now = _now()
        async with self.lock:
            cursor = await self.db.execute(
                f"INSERT INTO memories ({_COLUMNS}) VALUES "
                "(NULL, ?, ?, ?, ?, ?, 'live', NULL, ?, ?, ?, ?, ?, ?, ?, NULL, 0, ?)",
                (
                    type,
                    content,
                    blob,
                    embedding_model,
                    source,
                    prov.source_session_id,
                    prov.source_seq_start,
                    prov.source_seq_end,
                    prov.evidence_summary,
                    prov.confidence,
                    now,
                    now,
                    project_id,
                ),
            )
            await self.db.commit()
        assert cursor.lastrowid is not None
        return cursor.lastrowid

    async def supersede(self, old_id: int, new_id: int) -> None:
        """Mark ``old_id`` superseded by ``new_id`` (keeps lineage; drops from recall)."""
        async with self.lock:
            await self.db.execute(
                "UPDATE memories SET status='superseded', superseded_by=?, updated_at=? WHERE id=?",
                (new_id, _now(), old_id),
            )
            await self.db.commit()

    async def forget(self, memory_id: int) -> bool:
        """Mark a memory ``forgotten`` (gone from recall, kept for audit).

        Returns True if a live memory was forgotten, False if there was no such
        live memory (already forgotten/superseded, or unknown id)."""
        async with self.lock:
            cursor = await self.db.execute(
                "UPDATE memories SET status='forgotten', updated_at=? WHERE id=? AND status='live'",
                (_now(), memory_id),
            )
            await self.db.commit()
        return cursor.rowcount > 0

    async def touch(self, ids: list[int]) -> None:
        """Bump ``last_accessed_at``/``access_count`` for recalled memories."""
        if not ids:
            return
        now = _now()
        async with self.lock:
            await self.db.executemany(
                "UPDATE memories SET last_accessed_at=?, access_count=access_count+1 WHERE id=?",
                [(now, i) for i in ids],
            )
            await self.db.commit()

    async def update_content(self, memory_id: int, content: str) -> None:
        """Touch ``updated_at`` (used when remember() sees a near-verbatim duplicate)."""
        async with self.lock:
            await self.db.execute(
                "UPDATE memories SET content=?, updated_at=? WHERE id=?",
                (content, _now(), memory_id),
            )
            await self.db.commit()

    # --- reads -------------------------------------------------------------

    async def get(self, memory_id: int) -> Memory | None:
        """Fetch a memory of *any* status (live, superseded, or forgotten)."""
        cursor = await self.db.execute(f"SELECT {_COLUMNS} FROM memories WHERE id=?", (memory_id,))
        row = await cursor.fetchone()
        return _row_to_memory(row) if row else None

    async def all_live(
        self, *, project_id: object = ANY_PROJECT, include_global: bool = True
    ) -> list[Memory]:
        """All live memories, optionally scoped to a project. Default is unscoped (every
        live memory). A project scope (``project_id=P``) returns P's memories plus global
        ones — "what Kira knows about this project"."""
        scope_sql, scope_params = _scope_clause(project_id, include_global=include_global)
        cursor = await self.db.execute(
            f"SELECT {_COLUMNS} FROM memories WHERE status='live'{scope_sql} ORDER BY id",
            tuple(scope_params),
        )
        return [_row_to_memory(r) for r in await cursor.fetchall()]

    async def search(
        self,
        query_vec: np.ndarray | list[float],
        embedding_model: str,
        *,
        top_k: int,
        min_similarity: float,
        project_id: object = ANY_PROJECT,
        include_global: bool = True,
    ) -> list[ScoredMemory]:
        """Top-k live memories (same embedding model) with cosine ≥ ``min_similarity``,
        within the project scope (see :func:`_scope_clause`).

        Filtering by ``embedding_model`` prevents comparing vectors from different
        embedding spaces (a switch of model must not silently pollute results). The scope
        filter is applied in SQL, so the matmul runs over only the in-scope rows.
        """
        scope_sql, scope_params = _scope_clause(project_id, include_global=include_global)
        cursor = await self.db.execute(
            f"SELECT {_COLUMNS} FROM memories WHERE status='live' AND embedding_model=?{scope_sql}",
            (embedding_model, *scope_params),
        )
        rows = await cursor.fetchall()
        if not rows:
            return []
        memories = [_row_to_memory(r) for r in rows]
        matrix = np.vstack([m.embedding for m in memories])  # rows already unit vectors
        _, q_unit = _to_unit_blob(query_vec)
        scores = matrix @ q_unit  # cosine, since both sides are unit-normalized
        order = np.argsort(-scores)[:top_k]
        return [
            ScoredMemory(memory=memories[i], score=float(scores[i]))
            for i in order
            if scores[i] >= min_similarity
        ]


def _row_to_memory(row: tuple) -> Memory:
    return Memory(
        id=row[0],
        type=row[1],
        content=row[2],
        embedding=_from_blob(row[3]),
        embedding_model=row[4],
        source=row[5],
        status=row[6],
        superseded_by=row[7],
        provenance=Provenance(
            source_session_id=row[8],
            source_seq_start=row[9],
            source_seq_end=row[10],
            evidence_summary=row[11],
            confidence=row[12],
        ),
        created_at=row[13],
        updated_at=row[14],
        last_accessed_at=row[15],
        access_count=row[16],
        project_id=row[17],
    )
