"""Schema v13 (Phase 15.5): a guarded ``archived`` flag on sessions — archive a chat without
deleting it. Additive + re-runnable (guarded ALTER, the v11 shape). Keyless."""

from __future__ import annotations

from pathlib import Path

import aiosqlite

from jarvis.persistence import migrations as M
from jarvis.persistence.migrations import migrate


async def _build_v12(path: Path) -> aiosqlite.Connection:
    db = await aiosqlite.connect(path)
    await db.execute("PRAGMA foreign_keys = ON")
    for target, step in M.MIGRATIONS:
        if target > 12:
            break
        if isinstance(step, str):
            await db.executescript(step)
        else:
            await step(db)
        await db.execute(f"PRAGMA user_version = {target}")
        await db.commit()
    return db


async def test_v12_to_v13_adds_archived_flag_defaulting_off() -> None:
    db = await _build_v12(":memory:")
    try:
        assert await migrate(db) == 15
        cur = await db.execute("PRAGMA table_info(sessions)")
        cols = {r[1] for r in await cur.fetchall()}
        assert "archived" in cols
        # a fresh (and therefore every pre-existing) session defaults to NOT archived, so the
        # migration never hides a chat that was visible before.
        await db.execute(
            "INSERT INTO sessions (created_at, updated_at, title, kind) "
            "VALUES ('t', 't', 'c', 'interactive')"
        )
        await db.commit()
        row = await (await db.execute("SELECT archived FROM sessions")).fetchone()
        assert row[0] == 0
    finally:
        await db.close()


async def test_v13_is_rerunnable() -> None:
    db = await _build_v12(":memory:")
    try:
        await migrate(db)  # -> 13
        await db.execute("PRAGMA user_version = 12")  # simulate a crash before the version bump
        await db.commit()
        assert await migrate(db) == 15  # guarded ADD COLUMN ⇒ clean no-op re-run
    finally:
        await db.close()
