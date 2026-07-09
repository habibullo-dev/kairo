"""Dedup merge/split invariants (Phase 15 Task 9). Merging two asserted entities re-points the
merged node's asserted edges onto the canonical, aliases its title, and RETRACTS it (never
deletes); undo/split reverse it exactly. Derived edges are untouched (a rebuild re-derives them),
and candidate detection is strictly report-only. Keyless: a temp DB via connect()."""

from __future__ import annotations

import asyncio
from pathlib import Path

import numpy as np
import pytest

from jarvis.graph import GraphStore
from jarvis.graph.merge import find_duplicates, locate_merge, split
from jarvis.persistence.db import connect
from jarvis.projects import ProjectStore

_OPEN: list = []
_TS = "2026-03-01T00:00:00+00:00"


@pytest.fixture(autouse=True)
async def _close():
    yield
    while _OPEN:
        await _OPEN.pop().close()


async def _store(tmp_path: Path) -> GraphStore:
    db = await connect(tmp_path / "graph.db")
    _OPEN.append(db)
    await ProjectStore(db, asyncio.Lock()).create(name="P")  # project 1 for FK-backed rows
    return GraphStore(db, asyncio.Lock())


async def _person(store: GraphStore, title: str, **kw) -> int:
    return await store.add_node(
        kind="person", title=title, trust_class="reviewed", created_by="user", project_id=1, **kw)


async def _edge(store: GraphStore, s: str, d: str, *, origin: str = "asserted") -> None:
    sk, si = s.split(":")
    dk, di = d.split(":")
    await store.upsert_edge(
        src_kind=sk, src_id=si, dst_kind=dk, dst_id=di, edge_kind="relates_to", origin=origin,
        trust_class="reviewed", created_by="user", created_at=_TS, project_id=1)


async def _count(store: GraphStore, table: str) -> int:
    return (await (await store.db.execute(f"SELECT COUNT(*) FROM {table}")).fetchone())[0]


# --- merge: re-point + alias + retract -------------------------------------
async def test_merge_repoints_edges_aliases_and_retracts(tmp_path: Path) -> None:
    store = await _store(tmp_path)
    canon = await _person(store, "Ada Lovelace")
    merged = await _person(store, "Ada L.")
    topic = await store.add_node(
        kind="topic", title="Analytical Engine", trust_class="reviewed", created_by="user",
        project_id=1)
    await _edge(store, f"person:{merged}", f"topic:{topic}")

    mid = await store.merge_nodes(canonical_id=canon, merged_id=merged, created_by="user")

    assert (await store.get_node(merged)).status == "retracted"  # never deleted
    assert "Ada L." in (await store.get_node(canon)).labels  # aliased for discoverability
    live = await store.neighbors("person", str(canon))
    assert any(e.dst_kind == "topic" and e.dst_id == str(topic) for e in live)  # re-pointed
    assert not await store.neighbors("person", str(merged))  # merged endpoint has no live edges
    merges = await store.list_merges()
    assert [m.id for m in merges] == [mid] and merges[0].action == "merge"


async def test_undo_restores_everything(tmp_path: Path) -> None:
    store = await _store(tmp_path)
    canon, merged = await _person(store, "Ada Lovelace"), await _person(store, "Ada L.")
    topic = await store.add_node(
        kind="topic", title="T", trust_class="reviewed", created_by="user", project_id=1)
    await _edge(store, f"person:{merged}", f"topic:{topic}")
    mid = await store.merge_nodes(canonical_id=canon, merged_id=merged)

    assert await store.undo_merge(mid) is True

    assert (await store.get_node(merged)).status == "live"  # merged node back
    assert "Ada L." not in (await store.get_node(canon)).labels  # alias removed
    back = await store.neighbors("person", str(merged))
    assert any(e.dst_id == str(topic) for e in back)  # edge re-pointed back to merged
    assert not await store.neighbors("person", str(canon))  # canonical has no live edge again
    assert await store.list_merges() == []  # default excludes undone rows
    assert [m.id for m in await store.list_merges(include_undone=True)] == [mid]  # journaled
    assert await store.undo_merge(mid) is False  # idempotent


async def test_merge_collision_retracts_duplicate_not_a_constraint_error(tmp_path: Path) -> None:
    # If re-pointing the merged edge would duplicate one the canonical ALREADY has, we retract the
    # merged edge (never hit the unique-identity index) — and undo brings it back.
    store = await _store(tmp_path)
    canon, merged = await _person(store, "A"), await _person(store, "B")
    topic = await store.add_node(
        kind="topic", title="T", trust_class="reviewed", created_by="user", project_id=1)
    await _edge(store, f"person:{canon}", f"topic:{topic}")   # canonical already relates_to T
    await _edge(store, f"person:{merged}", f"topic:{topic}")  # merged relates_to T too

    mid = await store.merge_nodes(canonical_id=canon, merged_id=merged)
    live = await store.neighbors("person", str(canon))
    assert len([e for e in live if e.dst_id == str(topic)]) == 1  # exactly one survives

    await store.undo_merge(mid)
    assert any(e.dst_id == str(topic) for e in await store.neighbors("person", str(merged)))


async def test_self_loop_after_fold_is_retracted(tmp_path: Path) -> None:
    store = await _store(tmp_path)
    canon, merged = await _person(store, "A"), await _person(store, "B")
    await _edge(store, f"person:{merged}", f"person:{canon}")  # becomes a self-loop after fold
    await store.merge_nodes(canonical_id=canon, merged_id=merged)
    # No live self-loop edge on the canonical.
    assert not [e for e in await store.neighbors("person", str(canon))
                if e.src_id == str(canon) and e.dst_id == str(canon)]


async def test_merge_never_deletes_rows(tmp_path: Path) -> None:
    store = await _store(tmp_path)
    canon, merged = await _person(store, "A"), await _person(store, "B")
    await _edge(store, f"person:{merged}", f"person:{canon}")
    nodes_before = await _count(store, "graph_nodes")
    edges_before = await _count(store, "graph_edges")
    await store.merge_nodes(canonical_id=canon, merged_id=merged)
    # Row counts are unchanged — merge flips statuses, it does not DELETE.
    assert await _count(store, "graph_nodes") == nodes_before
    assert await _count(store, "graph_edges") == edges_before


async def test_merge_leaves_derived_edges_untouched(tmp_path: Path) -> None:
    store = await _store(tmp_path)
    canon, merged = await _person(store, "A"), await _person(store, "B")
    await _edge(store, f"person:{merged}", "topic:9", origin="derived")  # a derived cache row
    await store.merge_nodes(canonical_id=canon, merged_id=merged)
    derived = await store.list_edges(origin="derived")
    # The derived edge still points at the merged endpoint — merge only re-points asserted edges.
    assert any(e.src_kind == "person" and e.src_id == str(merged) for e in derived)


async def test_merge_survives_a_rebuild(tmp_path: Path) -> None:
    from jarvis.graph.builder import rebuild

    store = await _store(tmp_path)
    canon, merged = await _person(store, "A"), await _person(store, "B")
    topic = await store.add_node(
        kind="topic", title="T", trust_class="reviewed", created_by="user", project_id=1)
    await _edge(store, f"person:{merged}", f"topic:{topic}")
    await store.merge_nodes(canonical_id=canon, merged_id=merged)

    await rebuild(store)  # delete + re-derive the derived cache — must not resurrect the merge

    assert (await store.get_node(merged)).status == "retracted"
    assert any(e.dst_id == str(topic) for e in await store.neighbors("person", str(canon)))


# --- validation ------------------------------------------------------------
async def test_merge_validation_errors(tmp_path: Path) -> None:
    store = await _store(tmp_path)
    a = await _person(store, "A")
    topic = await store.add_node(
        kind="topic", title="T", trust_class="reviewed", created_by="user", project_id=1)
    with pytest.raises(ValueError):
        await store.merge_nodes(canonical_id=a, merged_id=a)  # into itself
    with pytest.raises(ValueError):
        await store.merge_nodes(canonical_id=a, merged_id=9999)  # missing
    with pytest.raises(ValueError):
        await store.merge_nodes(canonical_id=a, merged_id=topic)  # kind mismatch


# --- split -----------------------------------------------------------------
async def test_split_reverses_most_recent_merge(tmp_path: Path) -> None:
    store = await _store(tmp_path)
    canon, merged = await _person(store, "A"), await _person(store, "B")
    mid = await store.merge_nodes(canonical_id=canon, merged_id=merged)
    assert await locate_merge(store, merged) == mid

    assert await split(store, merged) == mid
    assert (await store.get_node(merged)).status == "live"
    assert await split(store, merged) is None  # nothing left to split (merge already undone)


# --- candidate detection: report-only --------------------------------------
async def test_find_duplicates_exact_and_semantic_no_mutation(tmp_path: Path) -> None:
    store = await _store(tmp_path)
    await _person(store, "Ada Lovelace")
    await _person(store, "ada lovelace")  # exact title-key (case-folded) duplicate
    await store.add_node(kind="topic", title="Engine A", trust_class="reviewed", created_by="user",
                         project_id=1, embedding=np.array([1.0, 0.0, 0.0]))
    await store.add_node(kind="topic", title="Engine B", trust_class="reviewed", created_by="user",
                         project_id=1, embedding=np.array([0.99, 0.02, 0.0]))  # cosine ~0.9998
    await store.add_node(kind="topic", title="Unrelated", trust_class="reviewed", created_by="user",
                         project_id=1, embedding=np.array([0.0, 1.0, 0.0]))

    nodes_before = await _count(store, "graph_nodes")
    cands = await find_duplicates(store, threshold=0.9)

    reasons = {c.reason for c in cands}
    assert "exact-title" in reasons and "similar" in reasons
    assert not any(c.b_title == "Unrelated" or c.a_title == "Unrelated" for c in cands)
    assert await _count(store, "graph_nodes") == nodes_before  # REPORT-ONLY: nothing mutated
    assert await store.list_merges() == []
