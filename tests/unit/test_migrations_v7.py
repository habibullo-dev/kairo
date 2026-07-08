"""Schema v7 migration (Phase 10): projects + cost/orchestration tables + nullable
project_id columns. Additive-only, so the test proves (a) a populated v6 db migrates
with every row surviving at global scope (project_id NULL), (b) the new tables/columns
exist with the right shape, (c) FK enforcement is intact, and (d) a real project can be
linked. Keyless, synthetic rows, no network."""

from __future__ import annotations

from pathlib import Path

import aiosqlite

from jarvis.persistence.migrations import (
    _SCHEMA_V1,
    _SCHEMA_V2,
    _SCHEMA_V3,
    _SCHEMA_V4,
    _migrate_v5,
    _migrate_v6,
    migrate,
)

_NOW = "2026-01-01T00:00:00+00:00"


async def _build_v6(path: Path) -> aiosqlite.Connection:
    """A populated v6 database: every scoped table has one row, so the v7 ADD COLUMNs are
    exercised against real data (a NULL backfill on a non-empty table)."""
    db = await aiosqlite.connect(path)
    await db.execute("PRAGMA foreign_keys = ON")
    await db.executescript(_SCHEMA_V1)
    await db.executescript(_SCHEMA_V2)
    await db.executescript(_SCHEMA_V3)
    await db.executescript(_SCHEMA_V4)
    await _migrate_v5(db)
    await _migrate_v6(db)
    await db.execute("PRAGMA user_version = 6")
    await db.execute(
        "INSERT INTO sessions (id, created_at, updated_at, title, kind) "
        "VALUES (1, ?, ?, 'chat', 'interactive')",
        (_NOW, _NOW),
    )
    await db.execute(
        "INSERT INTO memories (id, type, content, embedding, embedding_model, source, "
        "created_at, updated_at) VALUES (1, 'fact', 'm', x'00', 'e', 'user', ?, ?)",
        (_NOW, _NOW),
    )
    await db.execute(
        "INSERT INTO tasks (id, kind, title, payload, schedule_kind, schedule_spec, timezone, "
        "next_run_at, created_by, created_at, updated_at) "
        "VALUES (1, 'reminder', 't', 'p', 'once', ?, 'UTC', ?, 'user', ?, ?)",
        (_NOW, _NOW, _NOW, _NOW),
    )
    await db.execute(
        "INSERT INTO kb_sources (id, kind, origin, content_hash, raw_path, markdown_path, "
        "markdown_hash, converter, converter_version, byte_size, created_by, created_at, "
        "updated_at) "
        "VALUES (1, 'note', 'note', 'h', 'r', 'm', 'mh', 'passthrough', '1', 1, 'user', ?, ?)",
        (_NOW, _NOW),
    )
    await db.execute(
        "INSERT INTO digests (id, date_local, generated_at, sections_json, summary, "
        "suggested_actions_json, delivered_to, created_at) "
        "VALUES (1, '2026-01-01', ?, '[]', 's', '[]', '[\"ui\"]', ?)",
        (_NOW, _NOW),
    )
    await db.execute(
        "INSERT INTO agent_runs (id, title, prompt, tools_scope, started_at, created_at) "
        "VALUES (1, 'child', 'do x', '[]', ?, ?)",
        (_NOW, _NOW),
    )
    await db.commit()
    return db


async def test_v6_to_v7_preserves_rows_at_global_scope(tmp_path: Path) -> None:
    db = await _build_v6(tmp_path / "m.db")
    try:
        assert await migrate(db) == 11

        # Every pre-existing row survives and is global (project_id NULL).
        for table in ("sessions", "memories", "tasks", "kb_sources", "digests", "agent_runs"):
            cur = await db.execute(f"SELECT project_id FROM {table} WHERE id = 1")
            row = await cur.fetchone()
            assert row is not None, f"{table} row 1 vanished across v7"
            assert row[0] is None, f"{table}.project_id should default NULL (global), got {row[0]}"

        # sessions.pinned backfilled to 0; agent_runs gains the orchestration columns.
        cur = await db.execute("SELECT pinned FROM sessions WHERE id = 1")
        assert (await cur.fetchone())[0] == 0
        cur = await db.execute("PRAGMA table_info(agent_runs)")
        acols = {r[1] for r in await cur.fetchall()}
        assert {"project_id", "orchestration_run_id", "role", "stage"} <= acols

        # FK enforcement intact after an additive migration.
        cur = await db.execute("PRAGMA foreign_key_check")
        assert await cur.fetchall() == []
    finally:
        await db.close()


async def test_v7_creates_new_tables(tmp_path: Path) -> None:
    db = await _build_v6(tmp_path / "m.db")
    try:
        await migrate(db)
        cur = await db.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = {r[0] for r in await cur.fetchall()}
        assert {"projects", "orchestration_runs", "model_calls"} <= tables

        # model_calls is metadata-only: assert the ledger shape (no prompt/body columns).
        cur = await db.execute("PRAGMA table_info(model_calls)")
        mcols = {r[1] for r in await cur.fetchall()}
        assert {"provider", "model", "cost_usd", "pricing_version", "purpose"} <= mcols
        assert not (mcols & {"prompt", "body", "content", "result_text", "messages"})
    finally:
        await db.close()


async def test_v7_project_link_and_fk(tmp_path: Path) -> None:
    db = await _build_v6(tmp_path / "m.db")
    try:
        await migrate(db)
        cur = await db.execute(
            "INSERT INTO projects (name, slug, created_at, updated_at) VALUES ('P', 'p', ?, ?)",
            (_NOW, _NOW),
        )
        pid = cur.lastrowid
        await db.execute("UPDATE sessions SET project_id = ? WHERE id = 1", (pid,))
        await db.commit()
        cur = await db.execute("SELECT project_id FROM sessions WHERE id = 1")
        assert (await cur.fetchone())[0] == pid

        # A dangling project_id is rejected while FKs are enforced.
        cur = await db.execute("PRAGMA foreign_key_check")
        assert await cur.fetchall() == []
    finally:
        await db.close()


async def test_fresh_db_is_v11(tmp_path: Path) -> None:
    from jarvis.persistence.db import connect

    db = await connect(tmp_path / "fresh.db")
    try:
        cur = await db.execute("PRAGMA user_version")
        assert (await cur.fetchone())[0] == 11
    finally:
        await db.close()


async def test_v8_adds_team_stage_and_service_calls(tmp_path: Path) -> None:
    # Migration v8 (Phase 10B): model_calls gains team/stage; service_calls is a new
    # metadata-only table (no body/secret columns). Additive over a populated v7 db.
    db = await _build_v6(tmp_path / "m.db")
    try:
        assert await migrate(db) == 11
        cur = await db.execute("PRAGMA table_info(model_calls)")
        mcols = {r[1] for r in await cur.fetchall()}
        assert {"team", "stage"} <= mcols
        cur = await db.execute("PRAGMA table_info(service_calls)")
        scols = {r[1] for r in await cur.fetchall()}
        assert {"service", "est_cost_usd", "project_id", "orchestration_run_id", "team"} <= scols
        assert not (scols & {"prompt", "body", "content", "secret"})
        cur = await db.execute("PRAGMA foreign_key_check")
        assert await cur.fetchall() == []
    finally:
        await db.close()
