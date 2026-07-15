"""Schema v32 records archive-and-successor project reset lineage."""

from __future__ import annotations

import aiosqlite
import pytest

from kira.persistence import migrations as M


async def _columns(db: aiosqlite.Connection, table: str) -> set[str]:
    return {row[1] for row in await (await db.execute(f"PRAGMA table_info({table})")).fetchall()}


@pytest.mark.asyncio
async def test_v32_adds_project_reset_lineage_idempotently() -> None:
    db = await aiosqlite.connect(":memory:")
    try:
        await M.migrate(db)
        assert await _columns(db, "project_reset_events") == {
            "id",
            "predecessor_project_id",
            "successor_project_id",
            "retained_repositories",
            "created_at",
        }
        await db.executescript(M._SCHEMA_V32)
        assert await M.migrate(db) == M.latest_version()
        assert M.latest_version() >= 32
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_v32_enforces_one_distinct_successor_per_predecessor() -> None:
    db = await aiosqlite.connect(":memory:")
    try:
        await db.execute("PRAGMA foreign_keys = ON")
        await M.migrate(db)
        for name, slug in (("Old", "old"), ("New", "new"), ("Other", "other")):
            await db.execute(
                "INSERT INTO projects (name, slug, created_at, updated_at) VALUES (?, ?, 't', 't')",
                (name, slug),
            )
        await db.execute(
            "INSERT INTO project_reset_events "
            "(predecessor_project_id, successor_project_id, retained_repositories, created_at) "
            "VALUES (1, 2, 1, 't')"
        )
        await db.commit()
        with pytest.raises(aiosqlite.IntegrityError):
            await db.execute(
                "INSERT INTO project_reset_events "
                "(predecessor_project_id, successor_project_id, retained_repositories, "
                "created_at) VALUES (1, 3, 0, 't')"
            )
        await db.rollback()
        with pytest.raises(aiosqlite.IntegrityError):
            await db.execute(
                "INSERT INTO project_reset_events "
                "(predecessor_project_id, successor_project_id, retained_repositories, "
                "created_at) VALUES (3, 3, 0, 't')"
            )
    finally:
        await db.close()
