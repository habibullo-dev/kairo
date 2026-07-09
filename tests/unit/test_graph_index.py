"""Cost-aware graph indexing (Phase 15 Task 6). The indexer fails CLOSED on an unpriced embedding
model, tracks spend, and re-embeds only content that changed (content-hash keyed); it also embeds
the 'unindexed' memories that suggestion-approval created. Keyless: a FakeEmbedder (offline) whose
model is priced or not in the real pricing table."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from jarvis.graph import GraphStore
from jarvis.graph.index import CostAwareEmbedder, UnpricedEmbedderError, reindex
from jarvis.graph.review import UNINDEXED, approve
from jarvis.memory.embeddings import FakeEmbedder
from jarvis.memory.store import MemoryStore
from jarvis.observability.cost import load_pricing
from jarvis.persistence.db import connect

_OPEN: list = []
# The REAL pricing table (has the Phase-15 Voyage rows); load_pricing() with no path is the
# Anthropic-only code fallback, so tests must point at config/pricing.yaml.
_PRICING = load_pricing(Path(__file__).resolve().parents[2] / "config" / "pricing.yaml")


@pytest.fixture(autouse=True)
async def _close():
    yield
    while _OPEN:
        await _OPEN.pop().close()


async def _store(tmp_path: Path) -> GraphStore:
    db = await connect(tmp_path / "g.db")
    _OPEN.append(db)
    return GraphStore(db, asyncio.Lock())


def _priced() -> CostAwareEmbedder:
    # voyage-3-large IS in pricing.yaml (Phase 15) ⇒ priced.
    return CostAwareEmbedder(FakeEmbedder(model="voyage-3-large"), _PRICING)


async def test_unpriced_model_fails_closed(tmp_path: Path) -> None:
    ce = CostAwareEmbedder(FakeEmbedder(model="fake-embedder"), _PRICING)  # no pricing row
    with pytest.raises(UnpricedEmbedderError):
        await ce.embed_query("hello")
    assert ce.spent_usd == 0.0


async def test_priced_model_tracks_spend(tmp_path: Path) -> None:
    ce = _priced()
    await ce.embed_documents(["some text to embed"])
    assert ce.calls == 1 and ce.spent_usd > 0.0


async def test_reindex_is_content_hash_keyed(tmp_path: Path) -> None:
    store = await _store(tmp_path)
    nid = await store.add_node(kind="topic", title="Rust async", summary="tokio runtime",
                               trust_class="reviewed", created_by="user")
    assert (await reindex(store, _priced()))["entities_embedded"] == 1
    r2 = await reindex(store, _priced())
    assert r2["entities_embedded"] == 0 and r2["skipped"] == 1  # unchanged ⇒ not re-embedded
    await store.update_node(nid, title="Rust async runtimes")  # content changed
    assert (await reindex(store, _priced()))["entities_embedded"] == 1  # re-embedded


async def test_reindex_embeds_unindexed_memories(tmp_path: Path) -> None:
    store = await _store(tmp_path)
    sid = await store.add_suggestion(kind="memory", trust_class="model_generated",
                                     payload={"content": "codeword Falcon"})
    await approve(store, sid, resolved_by="user")  # -> a memory with the UNINDEXED sentinel
    mem = MemoryStore(store.db, store.lock)
    assert (await mem.all_live())[0].embedding_model == UNINDEXED
    r = await reindex(store, _priced())
    assert r["memories_embedded"] == 1
    assert (await mem.all_live())[0].embedding_model == "voyage-3-large"  # now embedded for recall


async def test_reindex_dry_run_spends_nothing(tmp_path: Path) -> None:
    store = await _store(tmp_path)
    await store.add_node(kind="topic", title="X", trust_class="reviewed", created_by="user")
    ce = _priced()
    r = await reindex(store, ce, dry_run=True)
    assert r["entities_embedded"] == 1 and r["spent_usd"] == 0.0 and ce.calls == 0
