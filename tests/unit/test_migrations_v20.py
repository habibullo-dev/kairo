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


async def test_v21_adds_metadata_only_model_failure_ledger() -> None:
    db = await _build_v19()
    try:
        await M._migrate_v20(db)
        await db.executescript(M._SCHEMA_V21)
        await db.executescript(M._SCHEMA_V21)  # version-marker rewind: clean idempotent replay
        columns = {
            row[1]
            for row in await (
                await db.execute("PRAGMA table_info(model_failures)")
            ).fetchall()
        }
        assert {
            "provider", "model", "latency_ms", "error_class", "project_id", "purpose"
        } <= columns
        assert not (columns & {"prompt", "body", "content", "error", "message", "response"})
        indexes = {
            row[1]
            for row in await (
                await db.execute("PRAGMA index_list(model_failures)")
            ).fetchall()
        }
        assert {
            "idx_model_failures_project_ts",
            "idx_model_failures_provider_model_ts",
            "idx_model_failures_run",
        } <= indexes
    finally:
        await db.close()


async def test_v22_adds_idempotent_parked_approval_columns() -> None:
    db = await _build_v19()
    try:
        await M._migrate_v20(db)
        await db.executescript(M._SCHEMA_V21)
        await M._migrate_v22(db)
        await M._migrate_v22(db)  # a version-marker rewind must remain recoverable
        columns = {
            row[1] for row in await (await db.execute("PRAGMA table_info(task_runs)")).fetchall()
        }
        assert {"continuation_json", "approval_state"} <= columns
        indexes = {
            row[1] for row in await (await db.execute("PRAGMA index_list(task_runs)")).fetchall()
        }
        assert "idx_task_runs_parked" in indexes
    finally:
        await db.close()


async def test_v23_adds_idempotent_bodies_free_resume_metadata() -> None:
    db = await _build_v19()
    try:
        await M._migrate_v20(db)
        await db.executescript(M._SCHEMA_V21)
        await M._migrate_v22(db)
        await M._migrate_v23(db)
        await M._migrate_v23(db)  # a version-marker rewind stays recoverable
        columns = {
            row[1]
            for row in await (
                await db.execute("PRAGMA table_info(orchestration_runs)")
            ).fetchall()
        }
        assert {"resume_state", "resume_checkpoint_json"} <= columns
        indexes = {
            row[1]
            for row in await (
                await db.execute("PRAGMA index_list(orchestration_runs)")
            ).fetchall()
        }
        assert "idx_orch_runs_resume_ready" in indexes
    finally:
        await db.close()
