"""Unified semantic + graph search (Phase 15 Task 6). One query returns ranked, trust-badged node
cards from FTS + semantic (cosine) hits, is quarantine-aware (pending suggestions + retracted nodes
never surface), expands 1 hop, and degrades to FTS-only without an embedder. Keyless: seed + a
FakeEmbedder (offline)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from kira.graph import GraphStore
from kira.graph.index import CostAwareEmbedder, reindex
from kira.graph.search import unified_search
from kira.memory.embeddings import FakeEmbedder
from kira.memory.store import MemoryStore
from kira.observability.cost import load_pricing
from kira.persistence.db import connect
from kira.projects import ProjectStore

_OPEN: list = []
_FAKE = FakeEmbedder(model="voyage-3-large")  # priced model so the indexer runs
_PRICING = load_pricing(Path(__file__).resolve().parents[2] / "config" / "pricing.yaml")


@pytest.fixture(autouse=True)
async def _close():
    yield
    while _OPEN:
        await _OPEN.pop().close()


async def _seed(tmp_path: Path):
    db = await connect(tmp_path / "g.db")
    _OPEN.append(db)
    lock = asyncio.Lock()
    await ProjectStore(db, lock).create(name="P")  # id 1
    store = GraphStore(db, lock)
    topic = await store.add_node(kind="topic", title="Zorblax reactor", summary="a fusion topic",
                                 trust_class="reviewed", created_by="user", project_id=1)
    dec = await store.add_node(kind="decision", title="Cool the Zorblax", trust_class="reviewed",
                               created_by="user", project_id=1)
    await store.upsert_edge(src_kind="decision", src_id=str(dec), dst_kind="topic",
                            dst_id=str(topic), edge_kind="decision_about", origin="asserted",
                            trust_class="reviewed", created_by="user", project_id=1,
                            created_at="2026-01-01T00:00:00+00:00")
    # a memory (embedded in the query's model so semantic search can match it)
    mem = MemoryStore(db, lock)
    vec = (await _FAKE.embed_documents(["Zorblax operating notes"]))[0]
    await mem.add(type="fact", content="Zorblax operating notes", embedding=vec,
                  embedding_model="voyage-3-large", source="user", project_id=1)
    # quarantined + retracted decoys that must NEVER surface
    await store.add_suggestion(kind="node", trust_class="untrusted_external",
                               payload={"title": "SecretQuarantined topic"}, project_id=1)
    gone = await store.add_node(kind="topic", title="GoneTopic obsolete", trust_class="reviewed",
                                created_by="user", project_id=1)
    await store.retract_node(gone)
    await reindex(store, CostAwareEmbedder(FakeEmbedder(model="voyage-3-large"), _PRICING))
    return store, topic, dec


async def test_search_finds_entity_and_memory_with_badges(tmp_path: Path) -> None:
    store, _t, _d = await _seed(tmp_path)
    res = await unified_search(store, _FAKE, "Zorblax", project_id=1)
    kinds = {r["kind"] for r in res["results"]}
    assert "topic" in kinds and "memory" in kinds
    assert all("trust_class" in r and "score" in r for r in res["results"])  # badged + ranked


async def test_quarantined_suggestion_never_surfaces(tmp_path: Path) -> None:
    store, _t, _d = await _seed(tmp_path)
    res = await unified_search(store, _FAKE, "SecretQuarantined", project_id=1)
    assert all("SecretQuarantined" not in (r["label"] or "") for r in res["results"])


async def test_retracted_node_never_surfaces(tmp_path: Path) -> None:
    store, _t, _d = await _seed(tmp_path)
    res = await unified_search(store, _FAKE, "GoneTopic", project_id=1)
    assert all("GoneTopic" not in (r["label"] or "") for r in res["results"])


async def test_degrades_to_fts_only_without_embedder(tmp_path: Path) -> None:
    store, _t, _d = await _seed(tmp_path)
    res = await unified_search(store, None, "Zorblax", project_id=1)
    assert res["results"]  # FTS still finds the entity/memory by keyword
    assert all("semantic" not in r["sources"] for r in res["results"])  # no embedding happened


async def test_one_hop_expansion(tmp_path: Path) -> None:
    store, topic, dec = await _seed(tmp_path)
    res = await unified_search(store, _FAKE, "Zorblax", project_id=1)
    # match kind+ref_id: a memory and a node can share a bare ref_id (both id 1).
    tid = str(topic)
    topic_hit = next(r for r in res["results"] if r["kind"] == "topic" and r["ref_id"] == tid)
    # the decision is connected to the topic via the asserted decision_about edge.
    assert any(c["node"]["ref_id"] == str(dec) for c in topic_hit.get("connected", []))
