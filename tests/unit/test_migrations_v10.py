"""Schema v10 migration (Phase 12): the outward-write substrate — write_intents +
connector_writes. Additive-only, so the test proves a populated v9 db migrates with every row
surviving, the new tables have the right shape (write_intents holds the payload; the journal is
metadata-only), FK enforcement is intact, and the idempotent script is re-runnable. Keyless."""

from __future__ import annotations

from pathlib import Path

import aiosqlite

from jarvis.persistence import migrations as M
from jarvis.persistence.migrations import migrate

_NOW = "2026-01-01T00:00:00+00:00"


async def _build_v9(path: Path) -> aiosqlite.Connection:
    """A database migrated to exactly v9 (head before this task), with one project + one chat so
    the additive v10 runs against real data."""
    db = await aiosqlite.connect(path)
    await db.execute("PRAGMA foreign_keys = ON")
    for target, step in M.MIGRATIONS:
        if target > 9:
            break
        if isinstance(step, str):
            await db.executescript(step)
        else:
            await step(db)
        await db.execute(f"PRAGMA user_version = {target}")
        await db.commit()
    await db.execute(
        "INSERT INTO projects (id, name, slug, repos_json, settings_json, created_at, updated_at) "
        "VALUES (1, 'P', 'p', '[]', '{}', ?, ?)",
        (_NOW, _NOW),
    )
    await db.execute(
        "INSERT INTO sessions (id, created_at, updated_at, title, kind) "
        "VALUES (1, ?, ?, 'chat', 'interactive')",
        (_NOW, _NOW),
    )
    await db.commit()
    return db


async def test_v9_to_v10_is_additive(tmp_path: Path) -> None:
    db = await _build_v9(tmp_path / "m.db")
    try:
        assert await migrate(db) == 13  # v10..v12 apply; v10's tables are asserted below

        # Pre-existing rows survive untouched.
        cur = await db.execute("SELECT name FROM projects WHERE id = 1")
        assert (await cur.fetchone())[0] == "P"

        cur = await db.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = {r[0] for r in await cur.fetchall()}
        assert {"write_intents", "connector_writes"} <= tables

        # write_intents holds the operational payload (needed to execute faithfully).
        cur = await db.execute("PRAGMA table_info(write_intents)")
        icols = {r[1] for r in await cur.fetchall()}
        assert {"idempotency_key", "state", "request_json", "preview_json", "source",
                "priority", "project_id"} <= icols

        # connector_writes is metadata-only: handles + status, no content column.
        cur = await db.execute("PRAGMA table_info(connector_writes)")
        jcols = {r[1] for r in await cur.fetchall()}
        assert {"verb", "scope", "remote_id", "rollback_kind", "status"} <= jcols
        assert not (jcols & {"body", "content", "title", "summary", "secret", "request_json"})

        # FK enforcement intact after the additive migration.
        cur = await db.execute("PRAGMA foreign_key_check")
        assert await cur.fetchall() == []
    finally:
        await db.close()


async def test_idempotency_key_is_unique(tmp_path: Path) -> None:
    db = await _build_v9(tmp_path / "m.db")
    try:
        await migrate(db)
        await db.execute(
            "INSERT INTO write_intents (idempotency_key, provider, kind, state, source, summary, "
            "request_json, created_at, updated_at) "
            "VALUES ('k', 'google', 'calendar_create', 'draft', 'agent', 's', '{}', ?, ?)",
            (_NOW, _NOW),
        )
        await db.commit()
        try:
            await db.execute(
                "INSERT INTO write_intents (idempotency_key, provider, kind, state, source, "
                "summary, request_json, created_at, updated_at) "
                "VALUES ('k', 'google', 'doc_create', 'draft', 'agent', 's', '{}', ?, ?)",
                (_NOW, _NOW),
            )
            raised = False
        except aiosqlite.IntegrityError:
            raised = True
        assert raised, "write_intents.idempotency_key must be UNIQUE (one logical intent = one row)"
    finally:
        await db.close()


async def test_state_and_status_checks(tmp_path: Path) -> None:
    db = await _build_v9(tmp_path / "m.db")
    try:
        await migrate(db)
        for bad in ("sent", "pending", ""):
            try:
                await db.execute(
                    "INSERT INTO write_intents (idempotency_key, provider, kind, state, source, "
                    "summary, request_json, created_at, updated_at) "
                    "VALUES (?, 'google', 'calendar_create', ?, 'agent', 's', '{}', ?, ?)",
                    (f"key-{bad}", bad, _NOW, _NOW),
                )
                raised = False
            except aiosqlite.IntegrityError:
                raised = True
            await db.rollback()
            assert raised, f"write_intents.state must reject {bad!r}"
    finally:
        await db.close()


async def test_v10_migration_is_rerunnable(tmp_path: Path) -> None:
    # A partial-failure re-run must be a clean no-op — every v10 statement is idempotent.
    db = await _build_v9(tmp_path / "m.db")
    try:
        await migrate(db)
        # Simulate a crash before the version bump: rewind user_version and re-run the step.
        await db.execute("PRAGMA user_version = 9")
        await db.commit()
        assert await migrate(db) == 13  # re-applying v10 (+v11+v12) over itself does not error
        cur = await db.execute("PRAGMA foreign_key_check")
        assert await cur.fetchall() == []
    finally:
        await db.close()
