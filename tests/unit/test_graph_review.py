"""Suggestion review + materialization (Phase 15 Task 5). Approve is the ONLY door from a
quarantined proposal to durable truth: it claims the suggestion (idempotent — no double-materialize)
then creates a real memory / asserted node / asserted edge, carrying the suggestion's worst-evidence
trust through UNCHANGED. Reject is terminal + materializes nothing. Keyless: temp DB."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from jarvis.graph import GraphStore
from jarvis.graph.review import UNINDEXED, approve, reject
from jarvis.memory.store import MemoryStore
from jarvis.persistence.db import connect
from jarvis.persistence.fts import query_domain

_OPEN: list = []


@pytest.fixture(autouse=True)
async def _close():
    yield
    while _OPEN:
        await _OPEN.pop().close()


async def _store(tmp_path: Path) -> GraphStore:
    db = await connect(tmp_path / "g.db")
    _OPEN.append(db)
    return GraphStore(db, asyncio.Lock())


async def test_approve_memory_lifts_quarantine(tmp_path: Path) -> None:
    store = await _store(tmp_path)
    sid = await store.add_suggestion(kind="memory", trust_class="model_generated",
                                     payload={"type": "fact", "content": "codeword is Falcon"})
    # Quarantined: the content is NOT a memory yet — nothing to retrieve.
    assert await query_domain(store.db, "memories", "Falcon") == []

    res = await approve(store, sid, resolved_by="user")
    assert res["ok"] and res["materialized"] == "memory"
    mem = MemoryStore(store.db, store.lock)
    rows = await mem.all_live()
    assert len(rows) == 1 and rows[0].source == "reviewed_suggestion"
    assert rows[0].embedding_model == UNINDEXED  # awaits real embedding (Task 6 reindex)
    assert await query_domain(store.db, "memories", "Falcon")  # now FTS-retrievable
    assert (await store.get_suggestion(sid)).status == "approved"


async def test_approve_is_idempotent_no_double_materialize(tmp_path: Path) -> None:
    store = await _store(tmp_path)
    sid = await store.add_suggestion(kind="memory", trust_class="model_generated",
                                     payload={"content": "once"})
    first = await approve(store, sid, resolved_by="user")
    second = await approve(store, sid, resolved_by="user")
    assert first["ok"] is True and second["ok"] is False  # claim-first ⇒ only one wins
    assert len(await MemoryStore(store.db, store.lock).all_live()) == 1  # never twice


async def test_approve_node_asserts_and_carries_trust(tmp_path: Path) -> None:
    store = await _store(tmp_path)
    sid = await store.add_suggestion(
        kind="node", trust_class="untrusted_external",  # worst-of-evidence, from Task 4
        payload={"kind": "topic", "title": "Zorblax reactor", "summary": "an external claim"})
    res = await approve(store, sid, resolved_by="user")
    assert res["materialized"] == "node"
    nodes = await store.list_nodes(kind="topic")
    assert len(nodes) == 1 and nodes[0].created_by == "user"
    assert nodes[0].trust_class == "untrusted_external"  # NEVER upgraded on approval
    assert [rid for rid, _ in await query_domain(store.db, "entities", "Zorblax")] == [nodes[0].id]


async def test_approve_edge_asserts(tmp_path: Path) -> None:
    store = await _store(tmp_path)
    sid = await store.add_suggestion(
        kind="edge", trust_class="reviewed",
        payload={"src": "decision:1", "dst": "topic:2", "edge_kind": "decision_about"})
    res = await approve(store, sid, resolved_by="user")
    assert res["materialized"] == "edge"
    edges = await store.list_edges(origin="asserted")
    assert len(edges) == 1 and edges[0].edge_kind == "decision_about"
    assert edges[0].src_id == "1" and edges[0].dst_kind == "topic"


async def test_reject_materializes_nothing(tmp_path: Path) -> None:
    store = await _store(tmp_path)
    sid = await store.add_suggestion(kind="memory", trust_class="untrusted_external",
                                     payload={"content": "spam"})
    res = await reject(store, sid, resolved_by="user")
    assert res["ok"] and (await store.get_suggestion(sid)).status == "rejected"
    assert await MemoryStore(store.db, store.lock).all_live() == []
    # a reject cannot then be approved (terminal)
    assert (await approve(store, sid, resolved_by="user"))["ok"] is False


async def test_approve_unknown_suggestion(tmp_path: Path) -> None:
    store = await _store(tmp_path)
    assert (await approve(store, 999, resolved_by="user"))["ok"] is False
