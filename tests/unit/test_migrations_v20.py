"""Schema v20 stores only bounded head-result metadata, never child reports."""

import aiosqlite

from jarvis.persistence import migrations as M


async def _build_v19() -> aiosqlite.Connection:
    db = await aiosqlite.connect(":memory:")
    await db.execute("PRAGMA foreign_keys = ON")
    for target, step in M.MIGRATIONS:
        if target > 19:
            break
        if isinstance(step, str):
            await db.executescript(step)
        else:
            await step(db)
        await db.execute(f"PRAGMA user_version = {target}")
        await db.commit()
    return db


async def test_v20_adds_head_result_columns_idempotently() -> None:
    db = await _build_v19()
    try:
        await M._migrate_v20(db)
        await M._migrate_v20(db)
        rows = await (await db.execute("PRAGMA table_info(orchestration_runs)")).fetchall()
        columns = {row[1] for row in rows}
        assert {"verdict_rationale", "synthesis_findings_json", "action_items_json"} <= columns
    finally:
        await db.close()
