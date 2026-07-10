"""MemoryStore tests: migration, blob round-trip, ranking, status semantics."""

from __future__ import annotations

from pathlib import Path

import aiosqlite
import numpy as np

from jarvis.memory.store import MemoryStore, Provenance
from jarvis.persistence.db import connect
from jarvis.persistence.migrations import _SCHEMA_V1, migrate

MODEL = "voyage-3-large"


async def _store(tmp_path: Path) -> MemoryStore:
    return MemoryStore(await connect(tmp_path / "mem.db"))


async def _add(store: MemoryStore, content: str, vec: list[float], **kw) -> int:
    return await store.add(
        type=kw.get("type", "fact"),
        content=content,
        embedding=vec,
        embedding_model=kw.get("embedding_model", MODEL),
        source=kw.get("source", "user"),
        provenance=kw.get("provenance"),
    )


# --- migration v1 -> v2 ----------------------------------------------------


async def test_v1_to_v2_migration_preserves_sessions_and_messages(tmp_path: Path) -> None:
    db = await aiosqlite.connect(tmp_path / "m.db")
    try:
        await db.executescript(_SCHEMA_V1)
        await db.execute("PRAGMA user_version = 1")
        now = "2026-01-01T00:00:00+00:00"
        await db.execute(
            "INSERT INTO sessions (created_at, updated_at, title) VALUES (?, ?, ?)",
            (now, now, "kept"),
        )
        await db.execute(
            "INSERT INTO messages (session_id, seq, role, content, created_at) "
            "VALUES (1, 0, 'user', ?, ?)",
            ('"hi"', now),
        )
        await db.commit()

        assert await migrate(db) == 15  # migrate() applies ALL pending onto a populated v1 db

        cur = await db.execute("SELECT title FROM sessions WHERE id=1")
        assert (await cur.fetchone())[0] == "kept"
        cur = await db.execute("SELECT content FROM messages WHERE session_id=1")
        assert (await cur.fetchone())[0] == '"hi"'
        # new columns present and null
        cur = await db.execute(
            "SELECT reflected_at, compaction_summary, compaction_cut FROM sessions WHERE id=1"
        )
        assert await cur.fetchone() == (None, None, None)
        # memories table exists and is empty
        cur = await db.execute("SELECT count(*) FROM memories")
        assert (await cur.fetchone())[0] == 0
    finally:
        await db.close()


# --- blob round-trip + normalization ---------------------------------------


async def test_add_get_roundtrip_and_unit_normalization(tmp_path: Path) -> None:
    store = await _store(tmp_path)
    try:
        mid = await _add(store, "the sky is blue", [3.0, 4.0, 0.0])  # norm 5 -> unit
        mem = await store.get(mid)
        assert mem is not None
        assert mem.content == "the sky is blue"
        assert mem.type == "fact"
        assert mem.status == "live"
        # stored unit-normalized, float32, dimension preserved
        assert mem.embedding.dtype == np.float32
        assert mem.embedding.shape == (3,)
        np.testing.assert_allclose(mem.embedding, [0.6, 0.8, 0.0], atol=1e-6)
        assert abs(float(np.linalg.norm(mem.embedding)) - 1.0) < 1e-6
    finally:
        await store.db.close()


async def test_provenance_roundtrip(tmp_path: Path) -> None:
    store = await _store(tmp_path)
    try:
        # a real session must exist — source_session_id is a FK to sessions(id)
        now = "2026-01-01T00:00:00+00:00"
        await store.db.execute(
            "INSERT INTO sessions (created_at, updated_at, title) VALUES (?, ?, NULL)", (now, now)
        )
        await store.db.commit()
        prov = Provenance(
            source_session_id=1,
            source_seq_start=2,
            source_seq_end=5,
            evidence_summary="user said so",
            confidence=0.9,
        )
        mid = await _add(store, "x", [1.0, 0.0], provenance=prov)
        mem = await store.get(mid)
        assert mem is not None
        assert mem.provenance == prov
    finally:
        await store.db.close()


# --- search ranking + thresholds -------------------------------------------


async def test_search_ranks_by_cosine_and_applies_floor(tmp_path: Path) -> None:
    store = await _store(tmp_path)
    try:
        a = await _add(store, "A", [1.0, 0.0, 0.0])  # cosine 1.0 vs query
        await _add(store, "B", [0.0, 1.0, 0.0])  # cosine 0.0 -> below floor
        c = await _add(store, "C", [0.8, 0.6, 0.0])  # cosine 0.8
        hits = await store.search([1.0, 0.0, 0.0], MODEL, top_k=5, min_similarity=0.5)
        assert [h.memory.id for h in hits] == [a, c]  # ordered by score, B filtered out
        assert hits[0].score > hits[1].score
    finally:
        await store.db.close()


async def test_search_top_k_limits_results(tmp_path: Path) -> None:
    store = await _store(tmp_path)
    try:
        for i in range(5):
            await _add(store, f"m{i}", [1.0, float(i) * 0.01, 0.0])
        hits = await store.search([1.0, 0.0, 0.0], MODEL, top_k=2, min_similarity=0.0)
        assert len(hits) == 2
    finally:
        await store.db.close()


async def test_search_filters_by_embedding_model(tmp_path: Path) -> None:
    store = await _store(tmp_path)
    try:
        await _add(store, "other-space", [1.0, 0.0], embedding_model="some-other-model")
        hits = await store.search([1.0, 0.0], MODEL, top_k=5, min_similarity=0.0)
        assert hits == []  # different embedding space is never mixed in
    finally:
        await store.db.close()


# --- status semantics: live-only recall, but audit-fetchable ----------------


async def test_supersede_drops_from_live_but_keeps_lineage(tmp_path: Path) -> None:
    store = await _store(tmp_path)
    try:
        old = await _add(store, "prefers tabs", [1.0, 0.0])
        new = await _add(store, "prefers spaces", [1.0, 0.0])
        await store.supersede(old, new)

        live_ids = [m.id for m in await store.all_live()]
        assert old not in live_ids and new in live_ids
        # superseded row still fetchable by id, with lineage recorded
        old_mem = await store.get(old)
        assert old_mem is not None
        assert old_mem.status == "superseded"
        assert old_mem.superseded_by == new
        # and excluded from search
        hits = await store.search([1.0, 0.0], MODEL, top_k=5, min_similarity=0.0)
        assert old not in [h.memory.id for h in hits]
    finally:
        await store.db.close()


async def test_forget_marks_forgotten_and_is_idempotent(tmp_path: Path) -> None:
    store = await _store(tmp_path)
    try:
        mid = await _add(store, "ephemeral", [1.0, 0.0])
        assert await store.forget(mid) is True
        assert await store.forget(mid) is False  # already forgotten -> no-op
        assert [m.id for m in await store.all_live()] == []
        # gone from recall...
        assert await store.search([1.0, 0.0], MODEL, top_k=5, min_similarity=0.0) == []
        # ...but still auditable by id
        mem = await store.get(mid)
        assert mem is not None and mem.status == "forgotten"
    finally:
        await store.db.close()


async def test_touch_bumps_access_count(tmp_path: Path) -> None:
    store = await _store(tmp_path)
    try:
        mid = await _add(store, "x", [1.0, 0.0])
        await store.touch([mid])
        await store.touch([mid])
        mem = await store.get(mid)
        assert mem is not None
        assert mem.access_count == 2
        assert mem.last_accessed_at is not None
    finally:
        await store.db.close()
