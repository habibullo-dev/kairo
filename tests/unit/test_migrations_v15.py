"""Schema v15 (Phase 16): the ``attention_items`` queue — one durable row per thing wanting the
human's judgment. Additive CREATE TABLE (re-runnable), the v12 script shape. Keyless."""

from __future__ import annotations

from pathlib import Path

import aiosqlite

from jarvis.persistence import migrations as M
from jarvis.persistence.migrations import migrate

_LATEST = M.MIGRATIONS[-1][0]


async def _build_v14(path: Path) -> aiosqlite.Connection:
    db = await aiosqlite.connect(path)
    await db.execute("PRAGMA foreign_keys = ON")
    for target, step in M.MIGRATIONS:
        if target > 14:
            break
        if isinstance(step, str):
            await db.executescript(step)
        else:
            await step(db)
        await db.execute(f"PRAGMA user_version = {target}")
        await db.commit()
    return db


async def test_v14_to_v15_creates_attention_items() -> None:
    db = await _build_v14(":memory:")
    try:
        assert await migrate(db) == _LATEST
        cur = await db.execute("PRAGMA table_info(attention_items)")
        cols = {r[1] for r in await cur.fetchall()}
        assert {"kind", "source", "state", "priority", "trust_class", "title", "dedupe_key"} <= cols
        # dedupe_key is UNIQUE but NULLs are distinct: two keyless rows coexist, a keyed re-insert
        # is rejected (the store returns the existing id instead).
        await db.execute("INSERT INTO projects (name, slug, created_at, updated_at) "
                         "VALUES ('P', 'p', 't', 't')")
        for _ in range(2):
            await db.execute("INSERT INTO attention_items (kind, source, state, priority, "
                             "trust_class, title, created_at, updated_at) VALUES "
                             "('alert','system','open','normal','model_generated','x','t','t')")
        await db.commit()
        n = await (await db.execute("SELECT COUNT(*) FROM attention_items")).fetchone()
        assert n[0] == 2  # two keyless (NULL dedupe_key) rows coexist
    finally:
        await db.close()


async def test_v15_is_rerunnable() -> None:
    db = await _build_v14(":memory:")
    try:
        await migrate(db)  # -> 15
        await db.execute("PRAGMA user_version = 14")  # simulate a crash before the version bump
        await db.commit()
        assert await migrate(db) == _LATEST  # CREATE TABLE IF NOT EXISTS ⇒ clean no-op re-run
    finally:
        await db.close()
