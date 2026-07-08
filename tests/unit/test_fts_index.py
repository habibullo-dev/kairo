"""FTS5 search index (schema v9): trigger↔base parity, idempotent rebuild, MATCH sanitising,
and — the load-bearing property — cross-project scope enforcement in SQL (never in MATCH).

All keyless: data goes in through the REAL stores so the sync triggers fire exactly as in
production (including the messages delete+reinsert bulk-save path)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import aiosqlite
import pytest

from jarvis.memory.store import MemoryStore
from jarvis.persistence.artifacts import ArtifactStore
from jarvis.persistence.db import connect
from jarvis.persistence.fts import (
    ANY_PROJECT,
    DOMAINS,
    FTS_TABLES,
    fts_match_query,
    integrity_check_all,
    query_domain,
    rebuild_all,
)
from jarvis.persistence.sessions import SessionStore
from jarvis.projects.store import ProjectStore

_OPEN: list = []
_EMB = [1.0, 2.0, 3.0]  # any non-zero vector; FTS indexes content, not the embedding


@pytest.fixture(autouse=True)
async def _close():
    yield
    while _OPEN:
        await _OPEN.pop().close()


async def _db(tmp_path: Path):
    db = await connect(tmp_path / "fts.db")
    _OPEN.append(db)
    return db


async def _mem(store: MemoryStore, content: str, project_id: int | None) -> int:
    return await store.add(
        type="fact",
        content=content,
        embedding=_EMB,
        embedding_model="fake",
        source="user",
        project_id=project_id,
    )


def _ids(rows: list[tuple[int, float]]) -> list[int]:
    return [r[0] for r in rows]


# --- sanitiser ------------------------------------------------------------------------
def test_fts_match_query_empty_is_none() -> None:
    assert fts_match_query("") is None
    assert fts_match_query("   ") is None
    assert fts_match_query(None) is None
    assert fts_match_query("!!!  ---") is None  # punctuation-only → no tokens


def test_fts_match_query_quotes_tokens_and_prefixes_last() -> None:
    assert fts_match_query("hello world") == '"hello" "world"*'
    assert fts_match_query("hello world", prefix=False) == '"hello" "world"'


def test_fts_match_query_neutralises_operators_and_quotes() -> None:
    # An attempt to inject FTS boolean/column/NEAR syntax becomes inert quoted tokens.
    q = fts_match_query('cats OR dogs" NEAR(x) col:v -bad')
    assert '"OR"' in q and '"NEAR"' in q and '"col"' in q  # keywords are literals, not operators
    # Every token is wrapped in a pair of quotes → an even, non-zero quote count. An unbalanced
    # count would be an FTS5 MATCH syntax error at query time.
    assert q.count('"') % 2 == 0 and q.count('"') >= 2
    # It also must not raise when actually run against FTS (see scoping tests below).


# --- structure ------------------------------------------------------------------------
async def test_seven_domains_and_empty_db_passes_integrity(tmp_path: Path) -> None:
    db = await _db(tmp_path)
    assert len(FTS_TABLES) == 7
    assert set(DOMAINS) == {
        "chats", "memories", "knowledge", "tasks", "orchestration", "digests", "artifacts"
    }
    await integrity_check_all(db)  # all indexes empty but consistent → no raise


# --- cross-project scoping (direct project_id) ----------------------------------------
async def test_memory_scope_is_enforced_in_sql(tmp_path: Path) -> None:
    db = await _db(tmp_path)
    lock = asyncio.Lock()
    projects = ProjectStore(db, lock)
    a = await projects.create(name="Alpha")  # id 1
    b = await projects.create(name="Bravo")  # id 2
    mem = MemoryStore(db, lock)
    m_a = await _mem(mem, "zulucanary alpha secret", a)
    m_b = await _mem(mem, "yankeecanary bravo secret", b)
    m_g = await _mem(mem, "globalcanary secret", None)

    # A's canary is visible for A, never for B (the cross-project leak test).
    assert _ids(await query_domain(db, "memories", "zulucanary", project_id=a,
                                   include_global=False)) == [m_a]
    assert await query_domain(db, "memories", "zulucanary", project_id=b,
                              include_global=False) == []

    # Global rows are visible to a project only with include_global=True.
    assert m_g in _ids(await query_domain(db, "memories", "secret", project_id=a))
    assert m_g not in _ids(await query_domain(db, "memories", "secret", project_id=a,
                                              include_global=False))

    # ANY_PROJECT sees everything that matches.
    all_secret = _ids(await query_domain(db, "memories", "secret", project_id=ANY_PROJECT))
    assert {m_a, m_b, m_g} <= set(all_secret)

    # global-only scope (project_id=None) sees only the global row.
    assert _ids(await query_domain(db, "memories", "secret", project_id=None)) == [m_g]


# --- cross-project scoping (join-based, via sessions.project_id) -----------------------
async def test_chat_scope_is_enforced_via_session_join(tmp_path: Path) -> None:
    db = await _db(tmp_path)
    lock = asyncio.Lock()
    projects = ProjectStore(db, lock)
    a = await projects.create(name="Alpha")
    b = await projects.create(name="Bravo")
    sessions = SessionStore(db, lock)
    s_a = await sessions.create_session(project_id=a)
    s_b = await sessions.create_session(project_id=b)
    await sessions.save_messages(s_a, [{"role": "user", "content": "alphacanary hello there"}])
    await sessions.save_messages(s_b, [{"role": "user", "content": "bravocanary hello there"}])

    hit_a = await query_domain(db, "chats", "alphacanary", project_id=a, include_global=False)
    assert len(hit_a) == 1
    assert await query_domain(db, "chats", "alphacanary", project_id=b,
                              include_global=False) == []
    # injection-shaped query must not error and must not leak across projects
    assert await query_domain(db, "chats", 'alphacanary OR bravocanary" col:x',
                              project_id=b, include_global=False) == []


# --- parity across the churny paths + integrity-check ---------------------------------
async def test_bulk_resave_removes_stale_hits_and_keeps_parity(tmp_path: Path) -> None:
    db = await _db(tmp_path)
    lock = asyncio.Lock()
    projects = ProjectStore(db, lock)
    a = await projects.create(name="Alpha")
    sessions = SessionStore(db, lock)
    sid = await sessions.create_session(project_id=a)

    await sessions.save_messages(sid, [{"role": "user", "content": "orangecanary one"}])
    assert len(await query_domain(db, "chats", "orangecanary", project_id=a)) == 1

    # The turn-by-turn path: DELETE all rows for the session then reinsert with new ids.
    await sessions.save_messages(sid, [{"role": "user", "content": "purplecanary two"}])
    assert await query_domain(db, "chats", "orangecanary", project_id=a) == []  # stale gone
    assert len(await query_domain(db, "chats", "purplecanary", project_id=a)) == 1

    await integrity_check_all(db)  # trigger↔base index parity holds across the churn


async def test_memory_update_and_forget_reflect_in_search(tmp_path: Path) -> None:
    db = await _db(tmp_path)
    lock = asyncio.Lock()
    projects = ProjectStore(db, lock)
    a = await projects.create(name="Alpha")
    mem = MemoryStore(db, lock)

    m1 = await _mem(mem, "beforeword content", a)
    await mem.update_content(m1, "afterword content")
    assert await query_domain(db, "memories", "beforeword", project_id=a) == []  # AU trigger
    assert len(await query_domain(db, "memories", "afterword", project_id=a)) == 1

    m2 = await _mem(mem, "gonecanary content", a)
    assert len(await query_domain(db, "memories", "gonecanary", project_id=a)) == 1
    await mem.forget(m2)
    # forget() flips status to 'forgotten' (row + index kept); the query's status='live'
    # JOIN filter excludes it — visibility lives in SQL, not the index.
    assert await query_domain(db, "memories", "gonecanary", project_id=a) == []
    await integrity_check_all(db)


async def test_rebuild_all_is_idempotent(tmp_path: Path) -> None:
    db = await _db(tmp_path)
    lock = asyncio.Lock()
    projects = ProjectStore(db, lock)
    a = await projects.create(name="Alpha")
    mem = MemoryStore(db, lock)
    await _mem(mem, "rebuildcanary content", a)

    before = _ids(await query_domain(db, "memories", "rebuildcanary", project_id=a))
    await rebuild_all(db, lock)
    await rebuild_all(db, lock)  # twice — must not double, must stay consistent
    after = _ids(await query_domain(db, "memories", "rebuildcanary", project_id=a))
    assert before == after  # stable ids across two rebuilds
    assert len(after) == 1  # no doubling
    await integrity_check_all(db)


# --- the leak-prone knowledge branch: wiki-global + status/review gate + cross-project -----
_TS = "2026-01-01T00:00:00+00:00"


async def _kb_source(db, *, project_id: int, key: str, status: str = "live",
                     review: str = "reviewed") -> int:
    cur = await db.execute(
        "INSERT INTO kb_sources (kind, origin, title, content_hash, raw_path, markdown_path, "
        "markdown_hash, converter, converter_version, byte_size, status, review_status, "
        "created_by, created_at, updated_at, project_id) "
        "VALUES ('note', ?, 'T', ?, 'r', 'm', 'mh', 'passthrough', '1', 1, ?, ?, 'user', ?, ?, ?)",
        (f"o-{key}", f"h-{key}", status, review, _TS, _TS, project_id),
    )
    await db.commit()
    assert cur.lastrowid is not None
    return cur.lastrowid


async def _kb_chunk(db, *, text: str, source_id: int | None = None,
                    wiki_path: str | None = None) -> None:
    await db.execute(
        "INSERT INTO kb_chunks (source_id, wiki_path, heading_path, seq, text, embedding, "
        "embedding_model, created_at) VALUES (?, ?, '', 0, ?, ?, 'fake', ?)",
        (source_id, wiki_path, text, b"\x00\x00\x00\x00", _TS),
    )
    await db.commit()


async def test_knowledge_scope_wiki_global_and_review_gate(tmp_path: Path) -> None:
    db = await _db(tmp_path)
    lock = asyncio.Lock()
    projects = ProjectStore(db, lock)
    a = await projects.create(name="Alpha")
    b = await projects.create(name="Bravo")

    s_a = await _kb_source(db, project_id=a, key="a-live")
    await _kb_chunk(db, text="kbcanaryalpha content", source_id=s_a)
    s_unrev = await _kb_source(db, project_id=a, key="a-unrev", review="unreviewed")
    await _kb_chunk(db, text="kbcanaryunrev content", source_id=s_unrev)
    s_sup = await _kb_source(db, project_id=a, key="a-sup", status="superseded")
    await _kb_chunk(db, text="kbcanarysup content", source_id=s_sup)
    s_b = await _kb_source(db, project_id=b, key="b-live")
    await _kb_chunk(db, text="kbcanarybravo content", source_id=s_b)
    await _kb_chunk(db, text="kbcanarywiki content", wiki_path="wiki/page.md")

    # A source chunk is visible for its own project, NEVER for another (cross-project isolation).
    assert len(await query_domain(db, "knowledge", "kbcanaryalpha", project_id=a,
                                  include_global=False)) == 1
    assert await query_domain(db, "knowledge", "kbcanaryalpha", project_id=b,
                              include_global=False) == []
    # Unreviewed and superseded source chunks are never returned.
    assert await query_domain(db, "knowledge", "kbcanaryunrev", project_id=a) == []
    assert await query_domain(db, "knowledge", "kbcanarysup", project_id=a) == []
    # Wiki chunks are global — visible in every scope, even strict include_global=False.
    assert len(await query_domain(db, "knowledge", "kbcanarywiki", project_id=a,
                                  include_global=False)) == 1
    assert len(await query_domain(db, "knowledge", "kbcanarywiki", project_id=b,
                                  include_global=False)) == 1
    await integrity_check_all(db)


async def test_orchestration_digest_artifact_search_paths(tmp_path: Path) -> None:
    db = await _db(tmp_path)
    lock = asyncio.Lock()
    projects = ProjectStore(db, lock)
    a = await projects.create(name="Alpha")

    # orchestration_runs.synthesis_summary is filled by a LATER UPDATE — the AU trigger must
    # index it (an insert-only index would never see the summary).
    await db.execute(
        "INSERT INTO orchestration_runs (project_id, workflow, title, config_json, "
        "context_manifest_json, status, started_at, created_at) "
        "VALUES (?, 'wf', 'orchcanarytitle', '{}', '{}', 'running', ?, ?)",
        (a, _TS, _TS),
    )
    await db.commit()
    assert len(await query_domain(db, "orchestration", "orchcanarytitle", project_id=a)) == 1
    await db.execute(
        "UPDATE orchestration_runs SET synthesis_summary = 'orchcanarysummary' "
        "WHERE title = 'orchcanarytitle'"
    )
    await db.commit()
    assert len(await query_domain(db, "orchestration", "orchcanarysummary", project_id=a)) == 1

    # Digests are global (project_id NULL): surfaced under a project scope with include_global,
    # hidden under a strict project-only scope (fail-closed — never leaks, deliberately).
    await db.execute(
        "INSERT INTO digests (date_local, generated_at, sections_json, summary, "
        "suggested_actions_json, delivered_to, created_at) "
        "VALUES ('2026-01-01', ?, '{}', 'digestcanary summary', '[]', '[]', ?)",
        (_TS, _TS),
    )
    await db.commit()
    assert len(await query_domain(db, "digests", "digestcanary", project_id=a,
                                  include_global=True)) == 1
    assert await query_domain(db, "digests", "digestcanary", project_id=a,
                              include_global=False) == []

    # artifacts: a set_labels UPDATE must re-sync the FTS index (AU trigger).
    store = ArtifactStore(db, lock, data_dir=tmp_path / "data", managed_roots={})
    aid = await store.register(origin_type="x", origin_id="a1", kind="design",
                               title="artcanarytitle", external_uri="https://e/x",
                               created_by="agent", project_id=a)
    assert len(await query_domain(db, "artifacts", "artcanarytitle", project_id=a)) == 1
    await store.set_labels(aid, ["artcanarylabel"])
    assert len(await query_domain(db, "artifacts", "artcanarylabel", project_id=a)) == 1
    await integrity_check_all(db)


async def test_v8_to_v9_backfill_indexes_preexisting_rows(tmp_path: Path) -> None:
    from jarvis.persistence import migrations as M

    db = await aiosqlite.connect(tmp_path / "bf.db")
    _OPEN.append(db)
    await db.execute("PRAGMA foreign_keys = ON")
    # Apply v1..v8 only, then seed rows the FTS triggers cannot have seen (tables not yet made).
    for target, step in M.MIGRATIONS:
        if target > 8:
            break
        if isinstance(step, str):
            await db.executescript(step)
        else:
            await step(db)
        await db.execute(f"PRAGMA user_version = {target}")
        await db.commit()
    await db.execute(
        "INSERT INTO projects (name, slug, repos_json, settings_json, created_at, updated_at) "
        "VALUES ('P', 'p', '[]', '{}', ?, ?)",
        (_TS, _TS),
    )
    await db.execute(
        "INSERT INTO sessions (created_at, updated_at, title, kind, project_id) "
        "VALUES (?, ?, 'S', 'interactive', 1)",
        (_TS, _TS),
    )
    await db.execute(
        "INSERT INTO messages (session_id, seq, role, content, created_at) "
        "VALUES (1, 0, 'user', ?, ?)",
        (json.dumps("backfillcanary hello"), _TS),
    )
    await db.execute(
        "INSERT INTO memories (type, content, embedding, embedding_model, source, created_at, "
        "updated_at, project_id) "
        "VALUES ('fact', 'membackfill content', ?, 'fake', 'user', ?, ?, 1)",
        (b"\x00\x00\x00\x00", _TS, _TS),
    )
    await db.commit()

    # Apply v9. Its in-migration 'rebuild' backfill is the ONLY path that indexes these
    # pre-existing rows (the triggers didn't exist when they were inserted).
    await M._migrate_v9(db)
    await db.execute("PRAGMA user_version = 9")
    await db.commit()

    assert len(await query_domain(db, "chats", "backfillcanary", project_id=1)) == 1
    assert len(await query_domain(db, "memories", "membackfill", project_id=1)) == 1
    await integrity_check_all(db)
    cur = await db.execute("PRAGMA table_info(projects)")
    assert any(r[1] == "pinned" for r in await cur.fetchall())
    for table in ("artifacts", "saved_views"):
        cur = await db.execute(f"SELECT count(*) FROM {table}")
        assert (await cur.fetchone())[0] == 0


async def test_v9_migration_is_rerunnable(tmp_path: Path) -> None:
    # A partial-failure re-run must be a clean no-op — every v9 statement is idempotent.
    from jarvis.persistence import migrations as M

    db = await connect(tmp_path / "rerun.db")  # connect() already migrated to v9
    _OPEN.append(db)
    await M._migrate_v9(db)  # run the whole v9 step AGAIN over the already-migrated db
    await db.commit()
    await integrity_check_all(db)
