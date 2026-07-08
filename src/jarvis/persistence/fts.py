"""Full-text search over the SQLite domains (schema v9).

The seven FTS5 tables created in migration v9 are **external content** tables: they keep no
copy of the text, they index a base table and read it back by rowid. Kept in sync by AFTER
INSERT/DELETE/UPDATE triggers (see ``migrations.py``). This module is the low-level query
layer over them — the federated, snippet-producing search *service* is built on top of it
(``jarvis.search``).

Three non-negotiables live here, not in the caller:

* **Scope / status / visibility in SQL, never in MATCH.** ``MATCH`` decides *what text hit*;
  the JOIN to the base table decides *whether the caller may see it* (project scope,
  ``status='live'``, kb review status, wiki-global, chat ``kind``). A cross-project leak is a
  scoping bug, so scoping is applied uniformly by :meth:`Domain.scope_clause`.
* **User input never reaches the FTS grammar raw.** :func:`fts_match_query` tokenises to word
  characters and re-quotes each token as an FTS5 string literal, so quotes / ``*`` / ``:`` /
  ``-`` / ``(`` in the query box can neither error nor inject column-filters or NEAR clauses.
* **The index is rebuilt, never hand-patched.** Backfill and maintenance both use FTS5's
  built-in ``'rebuild'`` — idempotent by construction.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass

import aiosqlite

# Scope sentinel, distinct from None (mirrors MemoryStore.ANY_PROJECT):
#   ANY_PROJECT -> no project filter (search everything the domain allows)
#   None        -> global only (project_id IS NULL)
#   int P       -> project P (plus global when include_global=True)
ANY_PROJECT: object = object()


@dataclass(frozen=True)
class Domain:
    """One searchable domain: its FTS table, the base table + joins that carry scope, and the
    static visibility predicates FTS itself cannot express."""

    name: str
    fts: str
    id_expr: str  # the base-row id to return, e.g. "m.id"
    join_sql: str  # JOIN(s) from the FTS table (by its own name) to base + scope tables
    static_where: str  # extra visibility predicates (status/kind), or ""
    project_col: str | None  # column for the standard scope clause; None => never scoped
    wiki_global: bool = False  # kb_chunks: wiki_path-owned chunks are visible in every scope

    def scope_clause(self, project_id: object, include_global: bool) -> tuple[str, list[object]]:
        """The `` AND …`` fragment (and its bound params) that enforces project scope +
        per-domain visibility. Returns ``("", [])`` only for ANY_PROJECT on an unscoped/normal
        domain."""
        std, params = _standard_scope(self.project_col, project_id, include_global)
        if self.wiki_global:
            # Source-owned chunks must be live + reviewed + in scope; wiki-owned chunks
            # (source_id NULL) are global and always visible.
            return (
                " AND (c.wiki_path IS NOT NULL OR "
                f"(ks.status = 'live' AND ks.review_status = 'reviewed'{std}))",
                params,
            )
        return std, params


def _standard_scope(
    project_col: str | None, project_id: object, include_global: bool
) -> tuple[str, list[object]]:
    if project_col is None or project_id is ANY_PROJECT:
        return "", []
    if project_id is None:
        return f" AND {project_col} IS NULL", []
    if include_global:
        return f" AND ({project_col} = ? OR {project_col} IS NULL)", [project_id]
    return f" AND {project_col} = ?", [project_id]


# The seven domains. Column choices mirror what each FTS table indexes (migrations.py v9).
DOMAINS: dict[str, Domain] = {
    "chats": Domain(
        name="chats",
        fts="messages_fts",
        id_expr="m.id",
        join_sql=(
            "JOIN messages m ON m.id = messages_fts.rowid "
            "JOIN sessions s ON s.id = m.session_id"
        ),
        static_where="AND s.kind = 'interactive'",
        project_col="s.project_id",
    ),
    "memories": Domain(
        name="memories",
        fts="memories_fts",
        id_expr="m.id",
        join_sql="JOIN memories m ON m.id = memories_fts.rowid",
        static_where="AND m.status = 'live'",
        project_col="m.project_id",
    ),
    "knowledge": Domain(
        name="knowledge",
        fts="kb_chunks_fts",
        id_expr="c.id",
        join_sql=(
            "JOIN kb_chunks c ON c.id = kb_chunks_fts.rowid "
            "LEFT JOIN kb_sources ks ON ks.id = c.source_id"
        ),
        static_where="",
        project_col="ks.project_id",
        wiki_global=True,
    ),
    "tasks": Domain(
        name="tasks",
        fts="tasks_fts",
        id_expr="t.id",
        join_sql="JOIN tasks t ON t.id = tasks_fts.rowid",
        static_where="",
        project_col="t.project_id",
    ),
    "orchestration": Domain(
        name="orchestration",
        fts="orchestration_runs_fts",
        id_expr="o.id",
        join_sql="JOIN orchestration_runs o ON o.id = orchestration_runs_fts.rowid",
        static_where="",
        project_col="o.project_id",
    ),
    "digests": Domain(
        name="digests",
        fts="digests_fts",
        id_expr="d.id",
        join_sql="JOIN digests d ON d.id = digests_fts.rowid",
        static_where="",
        project_col="d.project_id",
    ),
    "artifacts": Domain(
        name="artifacts",
        fts="artifacts_fts",
        id_expr="a.id",
        join_sql="JOIN artifacts a ON a.id = artifacts_fts.rowid",
        # Quarantined artifacts (e.g. an unreviewed meeting transcript) are never searchable —
        # visibility lives in SQL, mirroring the kb review/status gate (defence in depth).
        static_where="AND (a.sensitivity IS NULL OR a.sensitivity != 'quarantined')",
        project_col="a.project_id",
    ),
}

#: Every FTS table (for rebuild / integrity-check maintenance).
FTS_TABLES: tuple[str, ...] = tuple(d.fts for d in DOMAINS.values())

_WORD_SPLIT = re.compile(r"[^\w]+", re.UNICODE)


def fts_match_query(raw: str | None, *, prefix: bool = True) -> str | None:
    """Turn a free-text query box into a safe FTS5 MATCH string, or ``None`` if it has no
    searchable tokens (the caller should then return no results — an empty MATCH is an error).

    Every token is split on non-word characters and re-quoted as an FTS5 string literal, so no
    FTS operator (``"`` ``*`` ``:`` ``-`` ``(`` ``NEAR`` ``OR`` ``AND``) survives from user
    input. Tokens are implicitly AND-ed; the final token is a prefix query for type-ahead.
    """
    tokens = [t for t in _WORD_SPLIT.split(raw or "") if t]
    if not tokens:
        return None
    quoted = [f'"{t}"' for t in tokens]
    if prefix:
        quoted[-1] = quoted[-1] + "*"
    return " ".join(quoted)


async def query_domain(
    db: aiosqlite.Connection,
    domain: str,
    raw_query: str | None,
    *,
    project_id: object = ANY_PROJECT,
    include_global: bool = True,
    limit: int = 20,
) -> list[tuple[int, float]]:
    """Return ``[(base_row_id, bm25_score)]`` for ``domain`` matching ``raw_query``, ranked
    best-first, with project scope + visibility enforced in the JOIN. ``[]`` if the query has
    no tokens. Read-only (no lock)."""
    dom = DOMAINS[domain]
    match = fts_match_query(raw_query)
    if match is None:
        return []
    scope_sql, scope_params = dom.scope_clause(project_id, include_global)
    # All interpolated names are trusted module constants (Domain fields); only ? params
    # carry user input (the sanitised MATCH string, scope ids, limit).
    sql = (
        f"SELECT {dom.id_expr} AS ref_id, bm25({dom.fts}) AS score "
        f"FROM {dom.fts} {dom.join_sql} "
        f"WHERE {dom.fts} MATCH ? {dom.static_where}{scope_sql} "
        "ORDER BY score LIMIT ?"
    )
    cursor = await db.execute(sql, (match, *scope_params, limit))
    return [(int(row[0]), float(row[1])) for row in await cursor.fetchall()]


async def rebuild_all(db: aiosqlite.Connection, lock: asyncio.Lock | None = None) -> None:
    """Rebuild every FTS index from its content table — idempotent maintenance (also how the
    v9 migration backfills). Safe to run any time; re-syncs an index the triggers ever missed.
    Holds the write lock (this is a write)."""
    lock = lock or asyncio.Lock()
    async with lock:
        for fts in FTS_TABLES:
            await db.execute(f"INSERT INTO {fts}({fts}) VALUES ('rebuild')")
        await db.commit()


async def integrity_check_all(db: aiosqlite.Connection) -> None:
    """Assert every FTS index is consistent with its external content table. Raises
    ``aiosqlite``/``sqlite3`` error (SQLITE_CORRUPT_VTAB) if a trigger ever let an index drift
    from its base table — the rigorous trigger↔base parity check."""
    for fts in FTS_TABLES:
        await db.execute(f"INSERT INTO {fts}({fts}, rank) VALUES ('integrity-check', 1)")
