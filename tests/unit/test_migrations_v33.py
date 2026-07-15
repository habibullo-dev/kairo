"""Schema v33 makes meeting capture receipts a durable at-most-once boundary."""

from __future__ import annotations

import aiosqlite
import pytest

from kira.knowledge.store import KnowledgeStore, NewChunk
from kira.persistence import migrations as M


async def _add(store: KnowledgeStore, *, origin: str, content_hash: str) -> int:
    return await store.add_source(
        kind="note",
        origin=origin,
        title="Meeting note",
        content_hash=content_hash,
        raw_path=f"raw/{content_hash}.md",
        markdown_path=f"markdown/{content_hash}.md",
        markdown_hash=f"markdown-{content_hash}",
        converter="passthrough",
        converter_version="1",
        byte_size=10,
        review_status="unreviewed",
        created_by="user",
        project_id=None,
    )


@pytest.mark.asyncio
async def test_v33_enforces_one_source_per_meeting_capture_receipt() -> None:
    db = await aiosqlite.connect(":memory:")
    try:
        assert await M.migrate(db) == M.latest_version() == 33
        await db.executescript(M._SCHEMA_V33)
        store = KnowledgeStore(db)
        origin = "meeting-capture:project:7:123e4567-e89b-42d3-a456-426614174000"
        first_id = await _add(store, origin=origin, content_hash="first")
        with pytest.raises(aiosqlite.IntegrityError):
            await _add(store, origin=origin, content_hash="second")
        assert db.in_transaction is False

        # The intentional uniqueness failure must not poison the shared connection's next
        # transaction-backed derived-index write.
        await store.replace_chunks(
            source_id=first_id,
            chunks=[NewChunk("", 0, "receipt stays usable", [1.0, 0.0])],
            embedding_model="test",
        )

        # Legacy note origins were not receipts and remain outside this uniqueness contract.
        await _add(store, origin="meeting:7", content_hash="legacy-a")
        await _add(store, origin="meeting:7", content_hash="legacy-b")
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_v33_tolerates_sparse_version_only_backup_fixture() -> None:
    db = await aiosqlite.connect(":memory:")
    try:
        await db.execute("CREATE TABLE backup_canary (value TEXT)")
        await db.execute("PRAGMA user_version = 32")
        await db.commit()
        assert await M.migrate(db) == 33
        assert await (
            await db.execute("SELECT name FROM sqlite_master WHERE name='backup_canary'")
        ).fetchone() == ("backup_canary",)
    finally:
        await db.close()
