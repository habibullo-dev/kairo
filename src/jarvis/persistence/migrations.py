"""Tiny schema migration runner.

Schema version is tracked with SQLite's built-in ``PRAGMA user_version`` — no
extra table needed. Migrations are an ordered list of ``(version, sql)``; each is
applied once, in order, when the db's version is behind. This is deliberately
minimal so the data model stays visible (plain SQL, no ORM).
"""

from __future__ import annotations

import aiosqlite

_SCHEMA_V1 = """
CREATE TABLE sessions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    title      TEXT
);

CREATE TABLE messages (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    seq        INTEGER NOT NULL,
    role       TEXT NOT NULL,
    content    TEXT NOT NULL,   -- JSON: a string, or a list of content blocks
    created_at TEXT NOT NULL
);

CREATE INDEX idx_messages_session ON messages(session_id, seq);
"""

# Phase 2: long-term memory + compaction bookkeeping on sessions.
_SCHEMA_V2 = """
CREATE TABLE memories (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    type              TEXT NOT NULL CHECK (type IN ('fact','preference','project','episode')),
    content           TEXT NOT NULL,
    embedding         BLOB NOT NULL,        -- float32[dim] unit vector (little-endian)
    embedding_model   TEXT NOT NULL,        -- never silently mix vector spaces
    source            TEXT NOT NULL,        -- 'user' | 'agent' | 'reflection'
    status            TEXT NOT NULL DEFAULT 'live'
                      CHECK (status IN ('live','superseded','forgotten')),
    superseded_by     INTEGER REFERENCES memories(id),
    source_session_id INTEGER REFERENCES sessions(id) ON DELETE SET NULL,
    source_seq_start  INTEGER,
    source_seq_end    INTEGER,
    evidence_summary  TEXT,
    confidence        REAL,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL,
    last_accessed_at  TEXT,
    access_count      INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX idx_memories_live ON memories(status) WHERE status = 'live';
CREATE INDEX idx_memories_type ON memories(type);

ALTER TABLE sessions ADD COLUMN reflected_at TEXT;
ALTER TABLE sessions ADD COLUMN compaction_summary TEXT;
ALTER TABLE sessions ADD COLUMN compaction_cut INTEGER;
"""

# Phase 3: tasks & scheduling. Two status machines, deliberately split — a task's
# *lifecycle* (tasks.status) vs one *execution's* outcome (task_runs.status). Nothing
# is ever DELETEd: cancel/done/failed/missed are statuses; run history is audit.
#
# sessions.kind is load-bearing: background job transcripts are sessions too, and
# without the column they'd win latest_session_id() (hijacking --resume) and be
# reflected into long-term memory (laundering unattended web content into memories).
_SCHEMA_V3 = """
CREATE TABLE tasks (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    kind                 TEXT NOT NULL CHECK (kind IN ('reminder','job')),
    title                TEXT NOT NULL,
    payload              TEXT NOT NULL,      -- reminder text | job prompt, verbatim
    schedule_kind        TEXT NOT NULL CHECK (schedule_kind IN ('once','cron','interval')),
    schedule_spec        TEXT NOT NULL,      -- once: ISO-8601; cron: 5-field; interval: seconds
    timezone             TEXT NOT NULL,      -- IANA zone cron is evaluated in
    next_run_at          TEXT,               -- UTC ISO; NULL iff not active
    status               TEXT NOT NULL DEFAULT 'active'
                         CHECK (status IN ('active','done','cancelled','failed','missed')),
    created_by           TEXT NOT NULL CHECK (created_by IN ('user','agent')),
    source_session_id    INTEGER REFERENCES sessions(id) ON DELETE SET NULL,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    last_run_at          TEXT,
    last_error           TEXT,
    created_at           TEXT NOT NULL,
    updated_at           TEXT NOT NULL,
    CHECK (status = 'active' OR next_run_at IS NULL)   -- terminal states never look due
);

CREATE INDEX idx_tasks_due ON tasks(next_run_at) WHERE status = 'active';

CREATE TABLE task_runs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id       INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    scheduled_for TEXT NOT NULL,             -- the fire time this run serviced (UTC ISO)
    started_at    TEXT,
    finished_at   TEXT,
    status        TEXT NOT NULL CHECK (status IN ('running','ok','error','missed','aborted')),
    session_id    INTEGER REFERENCES sessions(id) ON DELETE SET NULL,  -- job transcript
    result_text   TEXT,                      -- final text (truncated) / delivery note
    denied_count  INTEGER NOT NULL DEFAULT 0,-- ASK->DENY / demotion events during the run
    error         TEXT,
    cost_usd      REAL,
    created_at    TEXT NOT NULL
);

CREATE INDEX idx_task_runs_task ON task_runs(task_id, id);

ALTER TABLE sessions ADD COLUMN kind TEXT NOT NULL DEFAULT 'interactive'
    CHECK (kind IN ('interactive','task'));
"""

# Phase 4: research + knowledge base ("LLM Wiki").
#
# kb_sources is a PRIMARY/AUDIT record (like memories/tasks) — never DELETEd; a
# re-ingest of changed content 'supersede's the prior row, keeping lineage.
#
# kb_chunks and kb_wiki_links are DERIVED INDEXES — rebuildable caches over the
# markdown artifacts and wiki files — and are the one deliberate exception to the
# "nothing is ever DELETEd" rule (see docs/decisions/0004-*). Re-ingest, page
# rewrite, and `kb rebuild` delete-and-replace their rows inside one transaction();
# auditing a cache audits nothing, and a chunk status machine would be dead weight.
_SCHEMA_V4 = """
CREATE TABLE kb_sources (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    kind              TEXT NOT NULL CHECK (kind IN ('file','url','note')),
    origin            TEXT NOT NULL,        -- resolved absolute path | full URL | 'note'
    title             TEXT,
    content_hash      TEXT NOT NULL,        -- sha256 hex of the raw bytes
    raw_path          TEXT NOT NULL,        -- immutable artifact, relative to knowledge dir
    markdown_path     TEXT NOT NULL,        -- converted markdown artifact
    markdown_hash     TEXT NOT NULL,        -- staleness / hand-edit detection
    converter         TEXT NOT NULL,        -- markitdown|docling|trafilatura|passthrough
    converter_version TEXT NOT NULL,
    byte_size         INTEGER NOT NULL,
    mime              TEXT,
    status            TEXT NOT NULL DEFAULT 'live'
                      CHECK (status IN ('live','superseded','rejected')),
    superseded_by     INTEGER REFERENCES kb_sources(id),
    review_status     TEXT NOT NULL DEFAULT 'reviewed'
                      CHECK (review_status IN ('reviewed','unreviewed')),
    created_by        TEXT NOT NULL CHECK (created_by IN ('user','agent')),
    source_session_id INTEGER REFERENCES sessions(id) ON DELETE SET NULL,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL
);

CREATE UNIQUE INDEX idx_kb_sources_hash   ON kb_sources(content_hash);
CREATE INDEX        idx_kb_sources_origin ON kb_sources(origin);
CREATE INDEX        idx_kb_sources_live   ON kb_sources(status) WHERE status = 'live';

CREATE TABLE kb_chunks (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id       INTEGER REFERENCES kb_sources(id),   -- set for source chunks
    wiki_path       TEXT,                                -- set for wiki-page chunks (wiki-relative)
    heading_path    TEXT NOT NULL DEFAULT '',            -- 'H1 > H2 > H3'
    seq             INTEGER NOT NULL,                    -- order within the document
    text            TEXT NOT NULL,
    embedding       BLOB NOT NULL,                       -- float32 unit vector (memory pattern)
    embedding_model TEXT NOT NULL,                       -- never silently mix vector spaces
    created_at      TEXT NOT NULL,
    CHECK ((source_id IS NOT NULL) <> (wiki_path IS NOT NULL))  -- exactly one owner
);

CREATE INDEX idx_kb_chunks_source ON kb_chunks(source_id);
CREATE INDEX idx_kb_chunks_wiki   ON kb_chunks(wiki_path);

CREATE TABLE kb_wiki_links (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    from_path  TEXT NOT NULL,         -- wiki-relative posix path of the linking page
    to_path    TEXT,                  -- resolved target page, NULL if unresolved (broken)
    to_raw     TEXT NOT NULL,         -- the target as written ('Rust Async' / 'tokio.md')
    link_text  TEXT,                  -- display text / alias
    link_kind  TEXT NOT NULL CHECK (link_kind IN ('wikilink','markdown')),
    created_at TEXT NOT NULL
);

CREATE INDEX idx_kb_links_from ON kb_wiki_links(from_path);
CREATE INDEX idx_kb_links_to   ON kb_wiki_links(to_path);
"""

# (target_version, sql). Append new tuples for future schema changes.
MIGRATIONS: list[tuple[int, str]] = [
    (1, _SCHEMA_V1),
    (2, _SCHEMA_V2),
    (3, _SCHEMA_V3),
    (4, _SCHEMA_V4),
]


async def migrate(db: aiosqlite.Connection) -> int:
    """Apply pending migrations. Returns the resulting schema version."""
    cursor = await db.execute("PRAGMA user_version")
    row = await cursor.fetchone()
    version = row[0] if row else 0
    for target, sql in MIGRATIONS:
        if version < target:
            await db.executescript(sql)
            await db.execute(f"PRAGMA user_version = {target}")  # target is a trusted int constant
            await db.commit()
            version = target
    return version
