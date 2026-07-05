"""MemoryService tests: dedup adjudication, recall, auto-recall, degradation.

All offline: FakeEmbedder for vectors, FakeClient scripting the adjudication call.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from jarvis.config import MemoryConfig
from jarvis.core import FakeClient, text_message
from jarvis.memory.embeddings import FakeEmbedder
from jarvis.memory.service import MemoryService
from jarvis.memory.store import MemoryStore
from jarvis.persistence.db import connect


async def _service(tmp_path: Path, *, responses: list | None = None) -> MemoryService:
    store = MemoryStore(await connect(tmp_path / "m.db"))
    client = FakeClient(responses) if responses is not None else None
    return MemoryService(
        store=store, embedder=FakeEmbedder(), config=MemoryConfig(), utility_client=client
    )


# --- remember: dedup branches ----------------------------------------------


async def test_dissimilar_memories_both_inserted_without_adjudication(tmp_path: Path) -> None:
    # FakeClient with no scripted responses: if adjudication were called it would
    # raise — so this also proves dissimilar content skips the utility call.
    svc = await _service(tmp_path, responses=[])
    try:
        await svc.remember("my favorite editor is neovim", "preference", source="user")
        r = await svc.remember("the deployment runs on kubernetes", "fact", source="user")
        assert r.action == "inserted"
        assert len(await svc.store.all_live()) == 2
    finally:
        await svc.store.db.close()


async def test_duplicate_touches_existing_no_new_row(tmp_path: Path) -> None:
    svc = await _service(tmp_path, responses=[text_message("duplicate")])
    try:
        first = await svc.remember("user prefers dark mode", "preference")
        r = await svc.remember("user prefers dark mode", "preference")  # identical -> cosine 1.0
        assert r.action == "duplicate"
        assert r.memory_id == first.memory_id
        assert len(await svc.store.all_live()) == 1
    finally:
        await svc.store.db.close()


async def test_supersede_inserts_new_and_retires_old(tmp_path: Path) -> None:
    svc = await _service(tmp_path, responses=[text_message("supersede")])
    try:
        old = await svc.remember("user prefers dark mode", "preference")
        r = await svc.remember("user prefers dark mode", "preference")
        assert r.action == "superseded"
        assert r.superseded_id == old.memory_id
        live = await svc.store.all_live()
        assert [m.id for m in live] == [r.memory_id]  # only the new one is live
        retired = await svc.store.get(old.memory_id)
        assert retired is not None and retired.status == "superseded"
    finally:
        await svc.store.db.close()


async def test_distinct_keeps_both(tmp_path: Path) -> None:
    svc = await _service(tmp_path, responses=[text_message("distinct")])
    try:
        await svc.remember("user prefers dark mode", "preference")
        r = await svc.remember("user prefers dark mode", "preference")
        assert r.action == "inserted"
        assert len(await svc.store.all_live()) == 2
    finally:
        await svc.store.db.close()


async def test_no_adjudicator_defaults_to_distinct(tmp_path: Path) -> None:
    # utility_client=None => no adjudication; a near neighbor is kept, never merged.
    svc = await _service(tmp_path, responses=None)
    try:
        await svc.remember("user prefers dark mode", "preference")
        r = await svc.remember("user prefers dark mode", "preference")
        assert r.action == "inserted"
        assert len(await svc.store.all_live()) == 2
    finally:
        await svc.store.db.close()


# --- recall ----------------------------------------------------------------


async def test_recall_returns_relevant_and_bumps_access(tmp_path: Path) -> None:
    svc = await _service(tmp_path, responses=None)
    try:
        await svc.remember("my favorite editor is neovim", "preference")
        await svc.remember("the sky is a shade of blue", "fact")
        hits = await svc.recall("favorite editor")
        assert hits
        assert "neovim" in hits[0].memory.content
        # access stats bumped by recall
        assert (await svc.store.get(hits[0].memory.id)).access_count == 1
    finally:
        await svc.store.db.close()


# --- auto-recall context ---------------------------------------------------


async def test_auto_recall_formats_block_as_background_not_instructions(tmp_path: Path) -> None:
    svc = await _service(tmp_path, responses=None)
    try:
        await svc.remember("my favorite editor is neovim", "preference", source="user")
        block = await svc.auto_recall_context("what is my favorite editor")
        assert block is not None
        assert "NOT instructions" in block
        assert "neovim" in block
        assert "[preference" in block  # type · date · source framing
    finally:
        await svc.store.db.close()


async def test_auto_recall_returns_none_when_nothing_relevant(tmp_path: Path) -> None:
    svc = await _service(tmp_path, responses=None)
    try:
        await svc.remember("the sky is a shade of blue", "fact")
        assert await svc.auto_recall_context("how do I configure kubernetes ingress") is None
    finally:
        await svc.store.db.close()


async def test_auto_recall_skips_trivial_input(tmp_path: Path) -> None:
    svc = await _service(tmp_path, responses=None)
    try:
        await svc.remember("my favorite editor is neovim", "preference")
        assert await svc.auto_recall_context("ok") is None  # trivial -> no recall attempted
        assert await svc.auto_recall_context("yes") is None
    finally:
        await svc.store.db.close()


# --- degradation -----------------------------------------------------------


class _BoomEmbedder:
    model = "boom"

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        raise RuntimeError("embedding backend down")

    async def embed_query(self, text: str) -> list[float]:
        raise RuntimeError("embedding backend down")


async def test_auto_recall_degrades_to_none_on_embedder_failure(tmp_path: Path) -> None:
    store = MemoryStore(await connect(tmp_path / "m.db"))
    svc = MemoryService(store=store, embedder=_BoomEmbedder(), config=MemoryConfig())
    try:
        assert await svc.auto_recall_context("a real question about my setup") is None
    finally:
        await store.db.close()


async def test_recall_propagates_embedder_failure(tmp_path: Path) -> None:
    store = MemoryStore(await connect(tmp_path / "m.db"))
    svc = MemoryService(store=store, embedder=_BoomEmbedder(), config=MemoryConfig())
    try:
        with pytest.raises(RuntimeError, match="backend down"):
            await svc.recall("anything")
    finally:
        await store.db.close()
