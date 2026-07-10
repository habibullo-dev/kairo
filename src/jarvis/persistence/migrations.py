"""Tiny schema migration runner.

Schema version is tracked with SQLite's built-in ``PRAGMA user_version`` — no
extra table needed. Migrations are an ordered list of ``(version, sql)``; each is
applied once, in order, when the db's version is behind. This is deliberately
minimal so the data model stays visible (plain SQL, no ORM).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

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

# Phase 6: multi-agent delegation.
#
# Two changes, and the first is why this migration is imperative rather than a SQL
# string: sessions.kind's CHECK must grow 'subagent', and SQLite cannot ALTER a CHECK
# — the table must be rebuilt. The rebuild has to run with foreign_keys OFF, and that
# PRAGMA is a silent no-op inside a transaction, so it must be toggled in autocommit
# (see _migrate_v5). agent_runs is the delegation audit table (never DELETEd, like
# task_runs): a 'running' row is opened before a child runs, so a crash leaves an
# orphan the startup sweep marks 'aborted'.
_SESSIONS_V5 = """
CREATE TABLE sessions_new (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL,
    title              TEXT,
    reflected_at       TEXT,
    compaction_summary TEXT,
    compaction_cut     INTEGER,
    kind               TEXT NOT NULL DEFAULT 'interactive'
                       CHECK (kind IN ('interactive','task','subagent'))
);
"""

# Run as individual statements inside the rebuild transaction (executescript would
# COMMIT first, breaking the atomic rebuild).
_AGENT_RUNS_V5: tuple[str, ...] = (
    """
    CREATE TABLE agent_runs (
        id                INTEGER PRIMARY KEY AUTOINCREMENT,
        parent_session_id INTEGER REFERENCES sessions(id) ON DELETE SET NULL,
        parent_trace_id   TEXT,                 -- the parent turn's trace id
        child_session_id  INTEGER REFERENCES sessions(id) ON DELETE SET NULL,  -- child transcript
        child_trace_id    TEXT,                 -- the child turn's trace id (links the two)
        title             TEXT NOT NULL,
        prompt            TEXT NOT NULL,        -- verbatim delegated task prompt (audit)
        tools_scope       TEXT NOT NULL,        -- JSON array of the child's allowlisted tools
        status            TEXT NOT NULL DEFAULT 'running'
            CHECK (status IN ('running','ok','error','timeout','cancelled','aborted')),
        iterations        INTEGER NOT NULL DEFAULT 0,
        denied_count      INTEGER NOT NULL DEFAULT 0,
        input_tokens      INTEGER NOT NULL DEFAULT 0,
        output_tokens     INTEGER NOT NULL DEFAULT 0,
        cost_usd          REAL,                 -- NULL when the child model's price is unknown
        result_text       TEXT,                 -- child final text (truncated) / failure note
        error             TEXT,
        started_at        TEXT NOT NULL,
        finished_at       TEXT,
        created_at        TEXT NOT NULL
    );
    """,
    "CREATE INDEX idx_agent_runs_parent  ON agent_runs(parent_session_id, id)",
    "CREATE INDEX idx_agent_runs_running ON agent_runs(status) WHERE status = 'running'",
)


async def _migrate_v5(db: aiosqlite.Connection) -> None:
    """Widen sessions.kind's CHECK to allow 'subagent' (via a full table rebuild) and
    add the agent_runs audit table.

    The standard SQLite table-rebuild procedure: foreign_keys OFF (outside a
    transaction — it's ignored inside one), rebuild inside one atomic transaction,
    then foreign_key_check to prove nothing was orphaned before turning enforcement
    back on. Referencing tables (messages, memories, tasks, task_runs, kb_sources)
    name `sessions` textually, so drop+rename preserves their foreign keys.
    """
    # 1. Disable FK enforcement in autocommit (a no-op if issued inside a transaction).
    await db.execute("PRAGMA foreign_keys = OFF")
    await db.commit()

    # 2. Rebuild sessions + create agent_runs, atomically.
    await db.execute("BEGIN IMMEDIATE")
    try:
        await db.execute(_SESSIONS_V5)
        await db.execute(
            "INSERT INTO sessions_new "
            "(id, created_at, updated_at, title, reflected_at, compaction_summary, "
            "compaction_cut, kind) "
            "SELECT id, created_at, updated_at, title, reflected_at, compaction_summary, "
            "compaction_cut, kind FROM sessions"
        )
        await db.execute("DROP TABLE sessions")
        await db.execute("ALTER TABLE sessions_new RENAME TO sessions")
        for statement in _AGENT_RUNS_V5:
            await db.execute(statement)
    except BaseException:
        await db.rollback()
        await db.execute("PRAGMA foreign_keys = ON")  # best-effort restore before propagating
        raise
    await db.commit()

    # 3. Verify the rebuild orphaned nothing, then re-enable enforcement.
    cursor = await db.execute("PRAGMA foreign_key_check")
    violations = await cursor.fetchall()
    await db.execute("PRAGMA foreign_keys = ON")
    if violations:
        raise RuntimeError(f"schema v5 migration left foreign-key violations: {violations}")


# Phase 9: the Daily Digest. Two changes, and the first is again why this is imperative:
# tasks.kind's CHECK must grow 'digest', and SQLite cannot ALTER a CHECK — the table is
# rebuilt with the same FK-off procedure as v5 (task_runs references tasks textually, so
# drop+rename preserves its foreign key). The digests table stores only minimized content
# (snippets/counts/status) — never raw email bodies or provider error bodies (amendment A4).
_TASKS_V6 = """
CREATE TABLE tasks_new (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    kind                 TEXT NOT NULL CHECK (kind IN ('reminder','job','digest')),
    title                TEXT NOT NULL,
    payload              TEXT NOT NULL,
    schedule_kind        TEXT NOT NULL CHECK (schedule_kind IN ('once','cron','interval')),
    schedule_spec        TEXT NOT NULL,
    timezone             TEXT NOT NULL,
    next_run_at          TEXT,
    status               TEXT NOT NULL DEFAULT 'active'
                         CHECK (status IN ('active','done','cancelled','failed','missed')),
    created_by           TEXT NOT NULL CHECK (created_by IN ('user','agent')),
    source_session_id    INTEGER REFERENCES sessions(id) ON DELETE SET NULL,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    last_run_at          TEXT,
    last_error           TEXT,
    created_at           TEXT NOT NULL,
    updated_at           TEXT NOT NULL,
    CHECK (status = 'active' OR next_run_at IS NULL)
);
"""

_DIGESTS_V6: tuple[str, ...] = (
    """
    CREATE TABLE digests (
        id                     INTEGER PRIMARY KEY AUTOINCREMENT,
        task_id                INTEGER REFERENCES tasks(id) ON DELETE SET NULL,
        date_local             TEXT NOT NULL,     -- the local calendar date covered
        generated_at           TEXT NOT NULL,     -- UTC ISO
        sections_json          TEXT NOT NULL,     -- minimized: snippets/counts/headers/status only
        summary                TEXT NOT NULL,
        suggested_actions_json TEXT NOT NULL,     -- JSON array of plain-text strings
        delivered_to           TEXT NOT NULL,     -- JSON array: ui/telegram/kakao/demo
        cost_usd               REAL,
        created_at             TEXT NOT NULL
    );
    """,
    "CREATE INDEX idx_digests_id ON digests(id)",
)


async def _migrate_v6(db: aiosqlite.Connection) -> None:
    """Widen tasks.kind's CHECK to allow 'digest' (full table rebuild) and add the digests
    table. Same FK-off/rebuild/verify procedure as :func:`_migrate_v5`."""
    await db.execute("PRAGMA foreign_keys = OFF")
    await db.commit()

    await db.execute("BEGIN IMMEDIATE")
    try:
        await db.execute(_TASKS_V6)
        await db.execute(
            "INSERT INTO tasks_new "
            "(id, kind, title, payload, schedule_kind, schedule_spec, timezone, next_run_at, "
            "status, created_by, source_session_id, consecutive_failures, last_run_at, "
            "last_error, created_at, updated_at) "
            "SELECT id, kind, title, payload, schedule_kind, schedule_spec, timezone, next_run_at, "
            "status, created_by, source_session_id, consecutive_failures, last_run_at, "
            "last_error, created_at, updated_at FROM tasks"
        )
        await db.execute("DROP TABLE tasks")
        await db.execute("ALTER TABLE tasks_new RENAME TO tasks")
        await db.execute("CREATE INDEX idx_tasks_due ON tasks(next_run_at) WHERE status = 'active'")
        for statement in _DIGESTS_V6:
            await db.execute(statement)
    except BaseException:
        await db.rollback()
        await db.execute("PRAGMA foreign_keys = ON")
        raise
    await db.commit()

    cursor = await db.execute("PRAGMA foreign_key_check")
    violations = await cursor.fetchall()
    await db.execute("PRAGMA foreign_keys = ON")
    if violations:
        raise RuntimeError(f"schema v6 migration left foreign-key violations: {violations}")


# Phase 10: project workspaces, orchestration studio, cost ledger. Unlike v5/v6 this is
# a plain SQL string migration: it only CREATEs new tables and ADDs nullable columns —
# nothing widens a CHECK, so no FK-off table rebuild is needed. Two ordering rules make
# it safe under `PRAGMA foreign_keys = ON` (set in db.connect before migrate runs):
#   1. projects and orchestration_runs are created BEFORE the ALTERs that reference them
#      (executescript runs statements in order).
#   2. every ADD COLUMN with a REFERENCES clause is nullable with a NULL default — the one
#      shape SQLite permits for ALTER ... ADD COLUMN while FK enforcement is on.
# NULL project_id == global scope (all pre-Phase-10 rows stay global). Never-DELETE holds:
# projects archive via a status flip; model_calls/orchestration_runs are append-only audit.
_SCHEMA_V7 = """
CREATE TABLE projects (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT NOT NULL,
    slug          TEXT NOT NULL UNIQUE,          -- stable handle for CLI / export dirs
    description   TEXT,
    status        TEXT NOT NULL DEFAULT 'active'
                  CHECK (status IN ('active','paused','archived')),
    color         TEXT,
    icon          TEXT,
    repos_json    TEXT NOT NULL DEFAULT '[]',    -- linked repo/folder absolute paths (JSON)
    settings_json TEXT NOT NULL DEFAULT '{}',    -- model-route/budget/roster OVERRIDES; never keys
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    archived_at   TEXT
);

-- Orchestration run audit (10B fills it; the table exists now so v7 is one migration and
-- agent_runs can FK-reference it). Metadata + short summaries only — never verbatim prompts.
CREATE TABLE orchestration_runs (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id            INTEGER NOT NULL REFERENCES projects(id),
    workflow              TEXT NOT NULL,          -- template id (code constant)
    title                 TEXT NOT NULL,          -- sanitized; never raw user/email text
    config_json           TEXT NOT NULL,          -- roster/routes/budgets snapshot (no keys)
    context_manifest_json TEXT NOT NULL,          -- selected context: ids/hashes only, no bodies
    status                TEXT NOT NULL DEFAULT 'running'
        CHECK (status IN ('running','ok','rejected','revise','error',
                          'cancelled','aborted','budget_stopped')),
    stage                 TEXT,                   -- council|synthesis|execution|review|verdict
    verdict               TEXT,                   -- accept|reject|revise
    synthesis_summary     TEXT,                   -- SHORT summary only
    estimated_cost_usd    REAL,
    actual_cost_usd       REAL,
    budget_usd            REAL,                   -- resolved hard per-run cap
    session_id            INTEGER REFERENCES sessions(id) ON DELETE SET NULL,
    trace_id              TEXT,
    started_at            TEXT NOT NULL,
    finished_at           TEXT,
    created_at            TEXT NOT NULL
);

-- The cost ledger: one row per LLM completion. Metadata only — no prompts/bodies/secrets.
-- cost_usd IS NULL means pricing was unknown (fail-closed); a 0.0 is a real priced zero.
CREATE TABLE model_calls (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    ts                   TEXT NOT NULL,           -- UTC ISO
    trace_id             TEXT,
    session_id           INTEGER REFERENCES sessions(id) ON DELETE SET NULL,
    project_id           INTEGER REFERENCES projects(id),
    orchestration_run_id INTEGER REFERENCES orchestration_runs(id),
    agent_role           TEXT,                    -- NULL for plain turns
    purpose              TEXT NOT NULL,           -- turn|subagent|utility|digest|orchestration
    provider             TEXT NOT NULL,           -- anthropic|openai
    model                TEXT NOT NULL,
    effort               TEXT,
    input_tokens         INTEGER NOT NULL DEFAULT 0,
    output_tokens        INTEGER NOT NULL DEFAULT 0,
    cache_write_tokens   INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens    INTEGER NOT NULL DEFAULT 0,
    tool_call_count      INTEGER NOT NULL DEFAULT 0,
    latency_ms           REAL,
    cost_usd             REAL,
    pricing_version      TEXT,
    created_at           TEXT NOT NULL
);

ALTER TABLE sessions   ADD COLUMN project_id INTEGER REFERENCES projects(id);
ALTER TABLE sessions   ADD COLUMN pinned INTEGER NOT NULL DEFAULT 0;
ALTER TABLE memories   ADD COLUMN project_id INTEGER REFERENCES projects(id);
ALTER TABLE tasks      ADD COLUMN project_id INTEGER REFERENCES projects(id);
ALTER TABLE kb_sources ADD COLUMN project_id INTEGER REFERENCES projects(id);
ALTER TABLE digests    ADD COLUMN project_id INTEGER REFERENCES projects(id);
ALTER TABLE agent_runs ADD COLUMN project_id INTEGER REFERENCES projects(id);
ALTER TABLE agent_runs ADD COLUMN orchestration_run_id INTEGER REFERENCES orchestration_runs(id);
ALTER TABLE agent_runs ADD COLUMN role  TEXT;
ALTER TABLE agent_runs ADD COLUMN stage TEXT;

CREATE INDEX idx_sessions_project   ON sessions(project_id, updated_at);
CREATE INDEX idx_memories_project   ON memories(project_id) WHERE status = 'live';
CREATE INDEX idx_kb_sources_project ON kb_sources(project_id) WHERE status = 'live';
CREATE INDEX idx_tasks_project      ON tasks(project_id);
CREATE INDEX idx_model_calls_project_ts ON model_calls(project_id, ts);
CREATE INDEX idx_model_calls_run        ON model_calls(orchestration_run_id);
CREATE INDEX idx_model_calls_ts         ON model_calls(ts);
CREATE INDEX idx_orch_runs_project ON orchestration_runs(project_id, id);
CREATE INDEX idx_orch_runs_running ON orchestration_runs(status) WHERE status = 'running';
"""


# Phase 10B: team-aware orchestration + the service ledger. Additive again (no CHECK widened),
# so a plain SQL string migration: model_calls gains team/stage attribution columns, and a new
# service_calls table records non-LLM service invocations (Semgrep/Gitleaks/Playwright/…) as
# metadata only — never a matched secret value or a body. est_cost_usd NULL = unpriced/unknown
# (fail-closed; never a silent 0.0), a real 0.0 = a known-free local tool.
_SCHEMA_V8 = """
ALTER TABLE model_calls ADD COLUMN team  TEXT;   -- orchestration team (NULL for plain calls)
ALTER TABLE model_calls ADD COLUMN stage TEXT;   -- council|synthesis|execution|review|verdict

CREATE TABLE service_calls (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    ts                   TEXT NOT NULL,           -- UTC ISO
    trace_id             TEXT,
    project_id           INTEGER REFERENCES projects(id),
    orchestration_run_id INTEGER REFERENCES orchestration_runs(id),
    team                 TEXT,
    agent_role           TEXT,
    stage                TEXT,
    service              TEXT NOT NULL,           -- catalog service name (semgrep|gitleaks|…)
    operation            TEXT,                    -- the op invoked (scan|screenshot|…)
    units                REAL,                    -- metered units, when applicable
    est_cost_usd         REAL,                    -- NULL = unpriced/unknown (fail-closed)
    pricing_version      TEXT,
    created_at           TEXT NOT NULL
);
CREATE INDEX idx_service_calls_project_ts ON service_calls(project_id, ts);
CREATE INDEX idx_service_calls_run        ON service_calls(orchestration_run_id);
"""


# Phase 11 (Workstation): artifacts, saved views, project pinning, and the first full-text
# search layer. Purely additive (new tables, one NOT-NULL-with-default ADD COLUMN, FTS5
# virtual tables + triggers + indexes; no CHECK widened, no table rebuilt). Run as an
# imperative step (_migrate_v9) whose statements are ALL idempotent: every CREATE uses
# IF NOT EXISTS and the one ADD COLUMN is guarded by a PRAGMA check. So a crash or partial
# failure before the user_version bump is fully recoverable — a re-run is a clean no-op —
# unlike a bare executescript string step (which auto-commits statement-by-statement).
#
# The FTS5 tables are EXTERNAL CONTENT (content='<base>', content_rowid='id'): they keep no
# copy of the text — they index the base table and read it back by rowid. Sync is by AFTER
# INSERT/DELETE/UPDATE triggers (uniform per domain; the UPDATE trigger is load-bearing for
# orchestration_runs, whose synthesis_summary is filled by a later UPDATE, and harmless where
# a table is only ever delete+reinserted). Backfill of pre-existing rows AND the maintenance
# re-sync both use FTS5's built-in 'rebuild' command (idempotent by construction — see
# persistence/fts.py). Scope / status / visibility filters live in the query JOIN
# (persistence/fts.py), never inside MATCH. This is the first virtual table and the first
# triggers in the schema.
_SCHEMA_V9 = """
-- --- Artifacts: a first-class, searchable record of things Kairo produced ---------------
-- Identity + dedupe is (origin_type, origin_id); content_hash is a NON-UNIQUE version
-- fingerprint (NULL for DB-backed artifacts with no file). Deliberately not unique: identical
-- content legitimately recurs across artifacts (boilerplate/empty docs, duplicated reports).
-- local_path XOR external_uri: a servable managed file, OR a reference (web URL /
-- app-internal handle). local_path is stored relative to the data dir and is confined to a
-- managed subtree + refused if sensitive, at registration time (ArtifactStore.register).
CREATE TABLE IF NOT EXISTS artifacts (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id       INTEGER REFERENCES projects(id),   -- NULL == global
    kind             TEXT NOT NULL,                      -- digest|eval_report|wiki|meeting|design
    title            TEXT NOT NULL,
    local_path       TEXT,                               -- rel. to data dir; XOR external_uri
    external_uri     TEXT,                               -- web URL or app-internal handle (kairo://…)
    content_hash     TEXT,                               -- sha256 version fp (non-unique)
    origin_type      TEXT NOT NULL,                      -- producer system
    origin_id        TEXT,                               -- stable handle within that system
    created_by       TEXT NOT NULL CHECK (created_by IN ('user','agent','system')),
    team             TEXT,
    role             TEXT,
    model            TEXT,
    sensitivity      TEXT,                               -- low|medium|high|quarantined (advisory)
    provenance_class TEXT,                               -- e.g. trusted_local (advisory)
    labels_json      TEXT NOT NULL DEFAULT '[]',
    pinned           INTEGER NOT NULL DEFAULT 0,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL,
    CHECK ((local_path IS NULL) <> (external_uri IS NULL))
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_artifacts_origin ON artifacts(origin_type, origin_id)
    WHERE origin_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_artifacts_project ON artifacts(project_id, id);
CREATE INDEX IF NOT EXISTS idx_artifacts_pinned  ON artifacts(pinned) WHERE pinned = 1;
CREATE INDEX IF NOT EXISTS idx_artifacts_kind    ON artifacts(kind);

-- --- Saved views / smart collections (Projects + Artifacts + Search surfaces) -----------
CREATE TABLE IF NOT EXISTS saved_views (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    scope       TEXT NOT NULL,                    -- projects|artifacts|search
    query_json  TEXT NOT NULL DEFAULT '{}',       -- opaque filter/query spec
    project_id  INTEGER REFERENCES projects(id),  -- NULL == global / all-projects
    created_by  TEXT NOT NULL CHECK (created_by IN ('user','agent','system')),
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_saved_views_scope ON saved_views(scope, id);

-- --- Project pinning (labels ride settings_json; archive stays status/archived_at) ------
-- projects.pinned is added by _migrate_v9's guarded ADD COLUMN (below), NOT here: ALTER TABLE
-- ADD COLUMN has no IF NOT EXISTS form, so it cannot live in this re-runnable script body.

-- --- Full-text search: external-content FTS5 tables + insert/delete/update sync triggers -
-- messages: content only (the session title lives on `sessions`; the search service joins it).
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
    content,
    content='messages', content_rowid='id',
    tokenize='unicode61 remove_diacritics 2'
);
CREATE TRIGGER IF NOT EXISTS messages_fts_ai AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts(rowid, content) VALUES (new.id, new.content);
END;
CREATE TRIGGER IF NOT EXISTS messages_fts_ad AFTER DELETE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, content) VALUES ('delete', old.id, old.content);
END;
CREATE TRIGGER IF NOT EXISTS messages_fts_au AFTER UPDATE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, content) VALUES ('delete', old.id, old.content);
    INSERT INTO messages_fts(rowid, content) VALUES (new.id, new.content);
END;

-- memories: content (the status='live' filter is applied in the query, not the index).
CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
    content,
    content='memories', content_rowid='id',
    tokenize='unicode61 remove_diacritics 2'
);
CREATE TRIGGER IF NOT EXISTS memories_fts_ai AFTER INSERT ON memories BEGIN
    INSERT INTO memories_fts(rowid, content) VALUES (new.id, new.content);
END;
CREATE TRIGGER IF NOT EXISTS memories_fts_ad AFTER DELETE ON memories BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, content) VALUES ('delete', old.id, old.content);
END;
CREATE TRIGGER IF NOT EXISTS memories_fts_au AFTER UPDATE ON memories BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, content) VALUES ('delete', old.id, old.content);
    INSERT INTO memories_fts(rowid, content) VALUES (new.id, new.content);
END;

-- kb_chunks: text (wiki chunks are global; source status/review filtered in the query).
CREATE VIRTUAL TABLE IF NOT EXISTS kb_chunks_fts USING fts5(
    text,
    content='kb_chunks', content_rowid='id',
    tokenize='unicode61 remove_diacritics 2'
);
CREATE TRIGGER IF NOT EXISTS kb_chunks_fts_ai AFTER INSERT ON kb_chunks BEGIN
    INSERT INTO kb_chunks_fts(rowid, text) VALUES (new.id, new.text);
END;
CREATE TRIGGER IF NOT EXISTS kb_chunks_fts_ad AFTER DELETE ON kb_chunks BEGIN
    INSERT INTO kb_chunks_fts(kb_chunks_fts, rowid, text) VALUES ('delete', old.id, old.text);
END;
CREATE TRIGGER IF NOT EXISTS kb_chunks_fts_au AFTER UPDATE ON kb_chunks BEGIN
    INSERT INTO kb_chunks_fts(kb_chunks_fts, rowid, text) VALUES ('delete', old.id, old.text);
    INSERT INTO kb_chunks_fts(rowid, text) VALUES (new.id, new.text);
END;

-- tasks: title + payload.
CREATE VIRTUAL TABLE IF NOT EXISTS tasks_fts USING fts5(
    title, payload,
    content='tasks', content_rowid='id',
    tokenize='unicode61 remove_diacritics 2'
);
CREATE TRIGGER IF NOT EXISTS tasks_fts_ai AFTER INSERT ON tasks BEGIN
    INSERT INTO tasks_fts(rowid, title, payload) VALUES (new.id, new.title, new.payload);
END;
CREATE TRIGGER IF NOT EXISTS tasks_fts_ad AFTER DELETE ON tasks BEGIN
    INSERT INTO tasks_fts(tasks_fts, rowid, title, payload)
        VALUES ('delete', old.id, old.title, old.payload);
END;
CREATE TRIGGER IF NOT EXISTS tasks_fts_au AFTER UPDATE ON tasks BEGIN
    INSERT INTO tasks_fts(tasks_fts, rowid, title, payload)
        VALUES ('delete', old.id, old.title, old.payload);
    INSERT INTO tasks_fts(rowid, title, payload) VALUES (new.id, new.title, new.payload);
END;

-- orchestration_runs: title + synthesis_summary (the summary is filled by a LATER update, so
-- the AFTER UPDATE trigger is required — an insert-only index would never see the summary).
CREATE VIRTUAL TABLE IF NOT EXISTS orchestration_runs_fts USING fts5(
    title, synthesis_summary,
    content='orchestration_runs', content_rowid='id',
    tokenize='unicode61 remove_diacritics 2'
);
CREATE TRIGGER IF NOT EXISTS orch_runs_fts_ai AFTER INSERT ON orchestration_runs BEGIN
    INSERT INTO orchestration_runs_fts(rowid, title, synthesis_summary)
        VALUES (new.id, new.title, new.synthesis_summary);
END;
CREATE TRIGGER IF NOT EXISTS orch_runs_fts_ad AFTER DELETE ON orchestration_runs BEGIN
    INSERT INTO orchestration_runs_fts(orchestration_runs_fts, rowid, title, synthesis_summary)
        VALUES ('delete', old.id, old.title, old.synthesis_summary);
END;
CREATE TRIGGER IF NOT EXISTS orch_runs_fts_au AFTER UPDATE ON orchestration_runs BEGIN
    INSERT INTO orchestration_runs_fts(orchestration_runs_fts, rowid, title, synthesis_summary)
        VALUES ('delete', old.id, old.title, old.synthesis_summary);
    INSERT INTO orchestration_runs_fts(rowid, title, synthesis_summary)
        VALUES (new.id, new.title, new.synthesis_summary);
END;

-- digests: summary (always global — digests.project_id is never written).
CREATE VIRTUAL TABLE IF NOT EXISTS digests_fts USING fts5(
    summary,
    content='digests', content_rowid='id',
    tokenize='unicode61 remove_diacritics 2'
);
CREATE TRIGGER IF NOT EXISTS digests_fts_ai AFTER INSERT ON digests BEGIN
    INSERT INTO digests_fts(rowid, summary) VALUES (new.id, new.summary);
END;
CREATE TRIGGER IF NOT EXISTS digests_fts_ad AFTER DELETE ON digests BEGIN
    INSERT INTO digests_fts(digests_fts, rowid, summary) VALUES ('delete', old.id, old.summary);
END;
CREATE TRIGGER IF NOT EXISTS digests_fts_au AFTER UPDATE ON digests BEGIN
    INSERT INTO digests_fts(digests_fts, rowid, summary) VALUES ('delete', old.id, old.summary);
    INSERT INTO digests_fts(rowid, summary) VALUES (new.id, new.summary);
END;

-- artifacts: title + labels (pin/label/version updates re-sync via the AFTER UPDATE trigger).
CREATE VIRTUAL TABLE IF NOT EXISTS artifacts_fts USING fts5(
    title, labels_json,
    content='artifacts', content_rowid='id',
    tokenize='unicode61 remove_diacritics 2'
);
CREATE TRIGGER IF NOT EXISTS artifacts_fts_ai AFTER INSERT ON artifacts BEGIN
    INSERT INTO artifacts_fts(rowid, title, labels_json)
        VALUES (new.id, new.title, new.labels_json);
END;
CREATE TRIGGER IF NOT EXISTS artifacts_fts_ad AFTER DELETE ON artifacts BEGIN
    INSERT INTO artifacts_fts(artifacts_fts, rowid, title, labels_json)
        VALUES ('delete', old.id, old.title, old.labels_json);
END;
CREATE TRIGGER IF NOT EXISTS artifacts_fts_au AFTER UPDATE ON artifacts BEGIN
    INSERT INTO artifacts_fts(artifacts_fts, rowid, title, labels_json)
        VALUES ('delete', old.id, old.title, old.labels_json);
    INSERT INTO artifacts_fts(rowid, title, labels_json)
        VALUES (new.id, new.title, new.labels_json);
END;

-- Backfill pre-existing rows into every FTS index (idempotent 'rebuild'; empty tables no-op).
INSERT INTO messages_fts(messages_fts) VALUES ('rebuild');
INSERT INTO memories_fts(memories_fts) VALUES ('rebuild');
INSERT INTO kb_chunks_fts(kb_chunks_fts) VALUES ('rebuild');
INSERT INTO tasks_fts(tasks_fts) VALUES ('rebuild');
INSERT INTO orchestration_runs_fts(orchestration_runs_fts) VALUES ('rebuild');
INSERT INTO digests_fts(digests_fts) VALUES ('rebuild');
INSERT INTO artifacts_fts(artifacts_fts) VALUES ('rebuild');
"""


async def _migrate_v9(db: aiosqlite.Connection) -> None:
    """Phase 11 schema (additive), made crash-safe. Every statement is idempotent so a partial
    failure or a crash before the user_version bump is recoverable — a re-run is a clean no-op:
    the one ADD COLUMN is guarded by a table_info check, and the _SCHEMA_V9 body uses
    ``CREATE ... IF NOT EXISTS`` throughout (the 'rebuild' backfills are idempotent by
    construction). foreign_keys stays ON — nothing is rebuilt (contrast _migrate_v5/_v6)."""
    cursor = await db.execute("PRAGMA table_info(projects)")
    has_pinned = any(row[1] == "pinned" for row in await cursor.fetchall())
    if not has_pinned:
        await db.execute("ALTER TABLE projects ADD COLUMN pinned INTEGER NOT NULL DEFAULT 0")
    await db.executescript(_SCHEMA_V9)


# Phase 12 (Action Connectors): the two-phase outward-write substrate. Purely additive — two new
# tables, no ADD COLUMN, no rebuild — so a plain idempotent script step is crash-safe: every
# statement is CREATE ... IF NOT EXISTS, so a re-run after a partial failure is a clean no-op.
#
# write_intents is the OPERATIONAL record of a proposed outward write (calendar/drive/gmail). It
# holds request_json — Kairo's resolved payload — so execution is byte-faithful to the approved
# preview: the model approves/rejects a STORED intent and can never forge a different payload at
# execute time, and there is deliberately no method to mutate request_json after the draft. The
# UNIQUE idempotency_key makes one logical intent exactly one row, so a replayed execute cannot
# double-write. The forward-looking columns (source, priority, project_id, the per-state
# timestamps) let the Phase 16 attention model absorb this queue without a reshape.
#
# connector_writes is the JOURNAL / outbox: metadata-only, like model_calls / service_calls —
# verb, scope, a remote handle, a rollback handle, status, timestamps. It carries NO title, body,
# attendee, or secret: the content lives on write_intents (needed to execute faithfully); the
# journal records only that a write happened and how to reverse it.
_SCHEMA_V10 = """
CREATE TABLE IF NOT EXISTS write_intents (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    idempotency_key TEXT NOT NULL UNIQUE,   -- one logical intent = one row (guards double-create)
    provider        TEXT NOT NULL,          -- 'google'
    kind            TEXT NOT NULL,          -- verb (see IntentKind: calendar_*/doc_*/gmail_draft_*)
    state           TEXT NOT NULL,          -- see IntentState (draft…executed…undone)
    project_id      INTEGER REFERENCES projects(id),   -- NULL == global
    source          TEXT NOT NULL CHECK (source IN ('user','agent','system')),
    priority        TEXT NOT NULL DEFAULT 'normal',     -- Phase-16 routing hint (vocabulary TBD)
    session_id      INTEGER,                -- attribution (best-effort; no FK — sessions may prune)
    trace_id        TEXT,
    summary         TEXT NOT NULL,          -- short human label for the queue (metadata only)
    request_json    TEXT NOT NULL,          -- resolved write payload — executed verbatim on approve
    preview_json    TEXT,                   -- rendered preview shown to the human (at 'previewed')
    result_json     TEXT,                   -- remote id + metadata after execute
    error           TEXT,                   -- friendly failure text (never a provider body)
    created_at      TEXT NOT NULL,
    previewed_at    TEXT,
    decided_at      TEXT,                   -- approved OR rejected timestamp
    executed_at     TEXT,
    undone_at       TEXT,
    updated_at      TEXT NOT NULL,
    CHECK (state IN ('draft','previewed','approved','executed','failed','rejected','undone'))
);
CREATE INDEX IF NOT EXISTS idx_write_intents_state   ON write_intents(state, id);
CREATE INDEX IF NOT EXISTS idx_write_intents_project ON write_intents(project_id, id);

CREATE TABLE IF NOT EXISTS connector_writes (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            TEXT NOT NULL,            -- UTC ISO of the write
    intent_id     INTEGER REFERENCES write_intents(id),
    provider      TEXT NOT NULL,
    verb          TEXT NOT NULL,            -- same vocabulary as write_intents.kind
    scope         TEXT,                     -- the OAuth scope exercised (e.g. calendar.events)
    project_id    INTEGER REFERENCES projects(id),
    remote_id     TEXT,                     -- created event/doc/draft id — a handle, never content
    rollback_kind TEXT,                     -- delete|trash|restore_revision|reinsert|none
    rollback_ref  TEXT,                     -- handle undo needs (revision/remote id); metadata
    status        TEXT NOT NULL CHECK (status IN ('executed','failed','undone')),
    egress_ref    TEXT,                     -- category/link into the egress log
    trace_id      TEXT,
    created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_connector_writes_intent     ON connector_writes(intent_id);
CREATE INDEX IF NOT EXISTS idx_connector_writes_project_ts ON connector_writes(project_id, ts);
"""


#: S7 Context Reuse: normalized cross-provider cache columns on model_calls (metadata only —
#: token counts + a mode label + a stable-prefix hash, never prompt text). Additive + guarded so
#: a partial-failure re-run is a clean no-op (ALTER ADD COLUMN has no IF NOT EXISTS). NULL is the
#: honest "not reported / not cached" — never a fabricated 0.
_V11_COLUMNS: tuple[tuple[str, str], ...] = (
    ("cached_input_tokens", "INTEGER"),  # OpenAI cached_tokens / DeepSeek hit / Gemini cached
    ("provider_cache_mode", "TEXT"),  # off|automatic_prefix|explicit_breakpoint|... (the mode used)
    ("provider_cache_hit_tokens", "INTEGER"),  # normalized "served from cache" count
    ("estimated_cache_savings_usd", "REAL"),  # derived; NULL when unpriced/unknown
    ("stable_prefix_hash", "TEXT"),  # the cached prefix's identity (from the assembler)
)


async def _migrate_v11(db: aiosqlite.Connection) -> None:
    cursor = await db.execute("PRAGMA table_info(model_calls)")
    existing = {row[1] for row in await cursor.fetchall()}
    for name, sql_type in _V11_COLUMNS:  # name/type are trusted constants, never caller input
        if name not in existing:
            await db.execute(f"ALTER TABLE model_calls ADD COLUMN {name} {sql_type}")


_SCHEMA_V12 = """
-- --- Phase 15: Memory Graph + Knowledge Topology ----------------------------------------
-- Three layers (mirrors kb_sources[primary] vs kb_chunks[derived]):
--   * DERIVED edges are a rebuildable CACHE over existing rows/FKs (origin='derived'); the
--     builder may delete+re-derive them (the one sanctioned DELETE). They hold no truth.
--   * ASSERTED nodes (graph_nodes) + edges (origin='asserted') are human-approved and
--     NEVER-DELETE: retract by status, never DROP a row (the memory/kb audit posture).
--   * SUGGESTIONS (graph_suggestions) are QUARANTINED: never FTS-indexed, never retrievable,
--     never exported, until a human approves (the ADR-0004 review gate). No auto-approve path.
-- Every node/edge carries provenance metadata; trust_class can never exceed its source's.

-- Asserted entities that have no existing row of their own (people/decisions/topics/refs).
-- Derived nodes are NOT stored here; they reference existing rows by (kind, ref_id) on edges.
CREATE TABLE IF NOT EXISTS graph_nodes (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    kind             TEXT NOT NULL,            -- person|decision|topic|external_ref|custom
    title            TEXT NOT NULL,
    summary          TEXT NOT NULL DEFAULT '',           -- short, bodies-free
    embedding        BLOB,                               -- numpy unit vector (NULL until indexed)
    embedding_model  TEXT,
    content_hash     TEXT,                     -- sha256(title+summary): re-embed on change
    project_id       INTEGER REFERENCES projects(id),    -- NULL == global
    trust_class      TEXT NOT NULL CHECK (trust_class IN
                        ('trusted_local','reviewed','untrusted_external','model_generated')),
    sensitivity      TEXT,                               -- low|medium|high|private (advisory)
    source_kind      TEXT,
    created_by       TEXT NOT NULL CHECK (created_by IN ('user','agent','system')),
    model            TEXT,
    status           TEXT NOT NULL DEFAULT 'live' CHECK (status IN ('live','retracted')),
    labels_json      TEXT NOT NULL DEFAULT '[]',
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_graph_nodes_project ON graph_nodes(project_id, id);
CREATE INDEX IF NOT EXISTS idx_graph_nodes_kind    ON graph_nodes(kind, status);
CREATE INDEX IF NOT EXISTS idx_graph_nodes_hash    ON graph_nodes(content_hash);

-- Edges over (kind, ref_id) endpoints: derived kinds reference existing rows (project:3,
-- artifact:41, wiki:pages/x.md, memory:17, run:9, source:12, task:5, chat:22, team:security,
-- service:firecrawl); asserted kinds reference graph_nodes.id (person:2, decision:4). ref ids
-- are TEXT to hold both integer row-ids and wiki paths.
CREATE TABLE IF NOT EXISTS graph_edges (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    src_kind      TEXT NOT NULL,
    src_id        TEXT NOT NULL,
    dst_kind      TEXT NOT NULL,
    dst_id        TEXT NOT NULL,
    edge_kind     TEXT NOT NULL,                         -- produced_by|cited_by|ran_in|relates_to|…
    origin        TEXT NOT NULL CHECK (origin IN ('derived','asserted')),
    project_id    INTEGER REFERENCES projects(id),
    trust_class   TEXT NOT NULL CHECK (trust_class IN
                     ('trusted_local','reviewed','untrusted_external','model_generated')),
    sensitivity   TEXT,
    created_by    TEXT NOT NULL CHECK (created_by IN ('user','agent','system')),
    model         TEXT,
    team          TEXT,
    evidence_json TEXT NOT NULL DEFAULT '[]',            -- pointers only (kind:id), bodies-free
    status        TEXT NOT NULL DEFAULT 'live' CHECK (status IN ('live','retracted')),
    created_at    TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_graph_edges_identity
    ON graph_edges(src_kind, src_id, dst_kind, dst_id, edge_kind, origin);
CREATE INDEX IF NOT EXISTS idx_graph_edges_src     ON graph_edges(src_kind, src_id, status);
CREATE INDEX IF NOT EXISTS idx_graph_edges_dst     ON graph_edges(dst_kind, dst_id, status);
CREATE INDEX IF NOT EXISTS idx_graph_edges_project ON graph_edges(project_id, id);
CREATE INDEX IF NOT EXISTS idx_graph_edges_origin  ON graph_edges(origin, status);

-- Quarantined proposals: NEVER FTS-indexed, retrievable, or exported until approved. Approving
-- materializes a real memories row / asserted graph node|edge. trust_class = worst evidence.
CREATE TABLE IF NOT EXISTS graph_suggestions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    kind            TEXT NOT NULL CHECK (kind IN ('memory','node','edge')),
    payload_json    TEXT NOT NULL,                       -- proposed memory/node/edge fields
    evidence_json   TEXT NOT NULL DEFAULT '[]',          -- pointers only, bodies-free
    project_id      INTEGER REFERENCES projects(id),
    trust_class     TEXT NOT NULL CHECK (trust_class IN
                       ('trusted_local','reviewed','untrusted_external','model_generated')),
    sensitivity     TEXT,
    extractor_model TEXT,
    est_cost_usd    REAL,                                -- NULL == unpriced (fail-closed upstream)
    status          TEXT NOT NULL DEFAULT 'pending'
                       CHECK (status IN ('pending','approved','rejected')),
    created_at      TEXT NOT NULL,
    resolved_at     TEXT,
    resolved_by     TEXT
);
CREATE INDEX IF NOT EXISTS idx_graph_suggestions_queue ON graph_suggestions(status, project_id, id);

-- Reversible journal for dedup merge/split (nodes are retracted, never deleted; edges re-point).
CREATE TABLE IF NOT EXISTS graph_merges (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    action         TEXT NOT NULL CHECK (action IN ('merge','split')),
    canonical_kind TEXT NOT NULL,
    canonical_id   TEXT NOT NULL,
    merged_kind    TEXT NOT NULL,
    merged_id      TEXT NOT NULL,
    undo_json      TEXT NOT NULL DEFAULT '{}',
    created_by     TEXT NOT NULL CHECK (created_by IN ('user','agent','system')),
    created_at     TEXT NOT NULL,
    undone_at      TEXT
);

-- FTS 'entities' domain over asserted nodes' title+summary (status filtered in the query;
-- suggestions are a SEPARATE table and thus never indexed — quarantine by construction).
CREATE VIRTUAL TABLE IF NOT EXISTS graph_nodes_fts USING fts5(
    title, summary,
    content='graph_nodes', content_rowid='id',
    tokenize='unicode61 remove_diacritics 2'
);
CREATE TRIGGER IF NOT EXISTS graph_nodes_fts_ai AFTER INSERT ON graph_nodes BEGIN
    INSERT INTO graph_nodes_fts(rowid, title, summary) VALUES (new.id, new.title, new.summary);
END;
CREATE TRIGGER IF NOT EXISTS graph_nodes_fts_ad AFTER DELETE ON graph_nodes BEGIN
    INSERT INTO graph_nodes_fts(graph_nodes_fts, rowid, title, summary)
        VALUES ('delete', old.id, old.title, old.summary);
END;
CREATE TRIGGER IF NOT EXISTS graph_nodes_fts_au AFTER UPDATE ON graph_nodes BEGIN
    INSERT INTO graph_nodes_fts(graph_nodes_fts, rowid, title, summary)
        VALUES ('delete', old.id, old.title, old.summary);
    INSERT INTO graph_nodes_fts(rowid, title, summary) VALUES (new.id, new.title, new.summary);
END;
"""


async def _migrate_v13(db: aiosqlite.Connection) -> None:
    # Phase 15.5: archive a chat (never delete) — a display-status flip on sessions, so a
    # long chat list can be tidied without losing history. Additive + guarded (ALTER ADD COLUMN
    # has no IF NOT EXISTS), so a partial-failure re-run is a clean no-op — the _migrate_v11 shape.
    cursor = await db.execute("PRAGMA table_info(sessions)")
    existing = {row[1] for row in await cursor.fetchall()}
    if "archived" not in existing:
        await db.execute("ALTER TABLE sessions ADD COLUMN archived INTEGER NOT NULL DEFAULT 0")


async def _migrate_v14(db: aiosqlite.Connection) -> None:
    # Phase 15.6: cost-aware routing mode attribution. Every model_calls row records HOW its model
    # was chosen — 'auto' (the cost-aware router), 'manual' (a human-pinned model), or NULL (no
    # router: REPL / sub-agents / evals — byte-identical to before). Additive + guarded (nullable,
    # no default), so a partial-failure re-run is a clean no-op.
    cursor = await db.execute("PRAGMA table_info(model_calls)")
    existing = {row[1] for row in await cursor.fetchall()}
    if "routing_mode" not in existing:
        await db.execute("ALTER TABLE model_calls ADD COLUMN routing_mode TEXT")


# Phase 16: the ONE attention queue. A durable row per item wanting the human's judgment — a live
# Gate ASK, a write-intent, a graph suggestion, a dreaming proposal, or a system alert. It UNIFIES
# those sources (source + source_ref point at them; it never duplicates their authority — approve/
# reject still hit the existing gated routes). payload_json is NEVER auto-injected into any model
# context (self-injection quarantine, the graph_suggestions precedent); dreaming rows default to the
# untrusted trust_class. dedupe_key is UNIQUE (SQLite treats NULLs as distinct, so ad-hoc items
# without a key coexist while a re-run of a keyed producer is idempotent).
_SCHEMA_V15 = """
CREATE TABLE IF NOT EXISTS attention_items (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    kind          TEXT NOT NULL CHECK (kind IN ('approval','review','proposal','alert')),
    source        TEXT NOT NULL,                 -- intent|graph_suggestion|dreaming|gate|system
    source_ref    TEXT,                          -- pointer into the source (bodies-free)
    project_id    INTEGER REFERENCES projects(id),
    priority      TEXT NOT NULL DEFAULT 'normal' CHECK (priority IN ('urgent','normal','low')),
    state         TEXT NOT NULL DEFAULT 'open'
                     CHECK (state IN ('open','done','dismissed','snoozed','expired')),
    trust_class   TEXT NOT NULL DEFAULT 'model_generated'
                     CHECK (trust_class IN
                        ('trusted_local','reviewed','untrusted_external','model_generated')),
    title         TEXT NOT NULL,                 -- short + safe: what a minimized push may show
    category      TEXT,                          -- routing category (title/count/category only)
    payload_json  TEXT NOT NULL DEFAULT '{}',    -- detail; NEVER auto-injected into model context
    evidence_json TEXT NOT NULL DEFAULT '[]',    -- pointers only, bodies-free
    dedupe_key    TEXT UNIQUE,                   -- idempotent producer re-runs
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    resolved_at   TEXT,
    snooze_until  TEXT
);
CREATE INDEX IF NOT EXISTS idx_attention_queue ON attention_items(state, priority, project_id, id);
CREATE INDEX IF NOT EXISTS idx_attention_source ON attention_items(source, source_ref);
"""


# A migration is either a SQL script (run via executescript) or an async callable that
# needs imperative control (v5's FK toggling + verification).
MigrationStep = str | Callable[[aiosqlite.Connection], Awaitable[None]]

# (target_version, step). Append new tuples for future schema changes.
MIGRATIONS: list[tuple[int, MigrationStep]] = [
    (1, _SCHEMA_V1),
    (2, _SCHEMA_V2),
    (3, _SCHEMA_V3),
    (4, _SCHEMA_V4),
    (5, _migrate_v5),
    (6, _migrate_v6),
    (7, _SCHEMA_V7),
    (8, _SCHEMA_V8),
    (9, _migrate_v9),
    (10, _SCHEMA_V10),
    (11, _migrate_v11),
    (12, _SCHEMA_V12),
    (13, _migrate_v13),
    (14, _migrate_v14),
    (15, _SCHEMA_V15),
]


def latest_version() -> int:
    """The target schema version, exposed so startup can snapshot before a real upgrade."""
    return MIGRATIONS[-1][0] if MIGRATIONS else 0


async def migrate(db: aiosqlite.Connection) -> int:
    """Apply pending migrations. Returns the resulting schema version."""
    cursor = await db.execute("PRAGMA user_version")
    row = await cursor.fetchone()
    version = row[0] if row else 0
    for target, step in MIGRATIONS:
        if version < target:
            if isinstance(step, str):
                await db.executescript(step)
            else:
                await step(db)
            await db.execute(f"PRAGMA user_version = {target}")  # target is a trusted int constant
            await db.commit()
            version = target
    return version
