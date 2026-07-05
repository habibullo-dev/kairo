"""SQLite connection helper (aiosqlite)."""

from __future__ import annotations

from pathlib import Path

import aiosqlite

from jarvis.persistence.migrations import migrate


async def connect(path: Path) -> aiosqlite.Connection:
    """Open the database at ``path`` (creating parent dirs), enable foreign keys,
    and run migrations. Returns a ready-to-use connection."""
    path.parent.mkdir(parents=True, exist_ok=True)
    db = await aiosqlite.connect(path)
    await db.execute("PRAGMA foreign_keys = ON")
    await migrate(db)
    return db
