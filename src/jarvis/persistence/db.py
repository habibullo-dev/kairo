"""SQLite connection helper (aiosqlite) + the shared write lock.

The whole app runs on **one** aiosqlite connection (a second connection to the
same file deadlocks on the first concurrent write). That makes interleaved writes
the hazard: with sqlite3's legacy implicit transactions, two coroutines writing
across await points share one open transaction, and either one's ``commit()``
commits the other's half-done work — a crash at the wrong moment can then lose
data (e.g. a ``save_messages`` DELETE committed without its INSERT).

Correctness therefore lives here, not in call-site discipline: every store takes
a shared ``asyncio.Lock``, single-statement writes hold it around execute+commit,
and multi-statement writes go through :func:`transaction` (BEGIN IMMEDIATE …
COMMIT/ROLLBACK under the lock, atomic on disk).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import aiosqlite

from jarvis.persistence.backup import create_pre_migration_snapshot, existing_database_version
from jarvis.persistence.migrations import latest_version, migrate


async def connect(path: Path) -> aiosqlite.Connection:
    """Open the database at ``path`` (creating parent dirs), enable foreign keys,
    and run migrations. Returns a ready-to-use connection."""
    path.parent.mkdir(parents=True, exist_ok=True)
    # A real, older database is snapshotted before any DDL. The online SQLite backup API makes
    # this safe even if another Kairo process currently has the database open. Snapshot failure
    # intentionally blocks migration rather than risking the only copy of user state.
    current_version = existing_database_version(path)
    target_version = latest_version()
    if current_version is not None and current_version < target_version:
        create_pre_migration_snapshot(
            path, current_version=current_version, target_version=target_version
        )
    db = await aiosqlite.connect(path)
    await db.execute("PRAGMA foreign_keys = ON")
    await migrate(db)
    return db


@asynccontextmanager
async def transaction(db: aiosqlite.Connection, lock: asyncio.Lock) -> AsyncIterator[None]:
    """One atomic multi-statement write: BEGIN IMMEDIATE … COMMIT under ``lock``.

    Any exception rolls the whole block back and re-raises. Statements inside the
    block must use ``db.execute`` directly — not store methods that re-acquire the
    lock (asyncio locks are not re-entrant).
    """
    async with lock:
        await db.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            await db.rollback()
            raise
        else:
            await db.commit()
