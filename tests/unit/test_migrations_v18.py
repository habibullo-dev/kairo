"""Schema v18: rejected/superseded source cache cleanup is audit-preserving and FTS-safe."""

from __future__ import annotations

import aiosqlite

from jarvis.persistence import migrations as M
from jarvis.persistence.fts import integrity_check_all


async def _build_v17() -> aiosqlite.Connection:
    db = await aiosqlite.connect(":memory:")
    await db.execute("PRAGMA foreign_keys = ON")
    for target, step in M.MIGRATIONS:
        if target > 17:
            break
        if isinstance(step, str):
            await db.executescript(step)
        else:
            await step(db)
        await db.execute(f"PRAGMA user_version = {target}")
        await db.commit()
    return db


async def test_v18_purges_terminal_source_chunks_but_preserves_audit_rows() -> None:
    db = await _build_v17()
    try:
        now = "2026-07-11T00:00:00+00:00"
        await db.execute(
            "INSERT INTO kb_sources (kind, origin, content_hash, raw_path, markdown_path, "
            "markdown_hash, converter, converter_version, byte_size, status, review_status, "
            "created_by, created_at, updated_at) VALUES "
            "('file', 'chat-upload:1:wrong/a.py', 'h', 'raw/a', 'markdown/a.md', 'mh', "
            "'passthrough', '1', 1, 'rejected', 'reviewed', 'user', ?, ?)",
            (now, now),
        )
        await db.execute(
            "INSERT INTO kb_chunks (source_id, heading_path, seq, text, embedding, "
            "embedding_model, created_at) VALUES (1, '', 0, 'stale-cache-canary', ?, 'test', ?)",
            (b"\x00\x00\x80?", now),
        )
        await db.commit()
        assert (await (await db.execute("SELECT COUNT(*) FROM kb_chunks")).fetchone())[0] == 1

        await M._migrate_v18(db)
        await db.execute("PRAGMA user_version = 18")
        await db.commit()

        assert (await (await db.execute("SELECT COUNT(*) FROM kb_sources")).fetchone())[0] == 1
        assert (await (await db.execute("SELECT COUNT(*) FROM kb_chunks")).fetchone())[0] == 0
        assert (await (await db.execute("SELECT COUNT(*) FROM kb_chunks_fts")).fetchone())[0] == 0
        await integrity_check_all(db)
    finally:
        await db.close()
