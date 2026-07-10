"""Schema v11 (S7 Context Reuse): normalized cross-provider cache columns on model_calls.

Additive + guarded (re-runnable), and model_calls stays metadata-only. Keyless."""

from __future__ import annotations

from pathlib import Path

import aiosqlite

from jarvis.persistence import migrations as M
from jarvis.persistence.migrations import migrate

_NOW = "2026-01-01T00:00:00+00:00"


async def _build_v10(path: Path) -> aiosqlite.Connection:
    db = await aiosqlite.connect(path)
    await db.execute("PRAGMA foreign_keys = ON")
    for target, step in M.MIGRATIONS:
        if target > 10:
            break
        if isinstance(step, str):
            await db.executescript(step)
        else:
            await step(db)
        await db.execute(f"PRAGMA user_version = {target}")
        await db.commit()
    return db


async def test_v10_to_v11_adds_normalized_cache_columns() -> None:
    db = await _build_v10(":memory:")
    try:
        assert await migrate(db) == 15
        cur = await db.execute("PRAGMA table_info(model_calls)")
        cols = {r[1] for r in await cur.fetchall()}
        assert {
            "cached_input_tokens",
            "provider_cache_mode",
            "provider_cache_hit_tokens",
            "estimated_cache_savings_usd",
            "stable_prefix_hash",
        } <= cols
        # still metadata-only — no prompt/body/content column crept in.
        assert not (cols & {"prompt", "body", "content", "messages", "result_text"})
    finally:
        await db.close()


async def test_v11_is_rerunnable() -> None:
    db = await _build_v10(":memory:")
    try:
        await migrate(db)  # -> 11
        await db.execute("PRAGMA user_version = 10")  # simulate a crash before the bump
        await db.commit()
        assert await migrate(db) == 15  # guarded ADD COLUMN ⇒ clean no-op re-run
    finally:
        await db.close()
    # (that the new columns accept NULL when caching is off AND real values when on is exercised
    #  end-to-end through CostLedger.record in test_context_reuse_ledger.py.)
