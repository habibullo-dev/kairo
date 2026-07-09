"""GraphStore + migration v12 invariants (Phase 15 Task 1). The graph's persistence must honor:
asserted rows are never-DELETE (retract only), derived edges are a delete+rebuild cache that can
never touch asserted rows, suggestions are quarantined (own table, no FTS index) with a single
pending→resolved transition, and the entities FTS surfaces only live asserted nodes. Keyless: a
temp DB via connect() (which runs migrate)."""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import numpy as np
import pytest

from jarvis.graph import GraphStore
from jarvis.persistence.db import connect
from jarvis.persistence.fts import query_domain
from jarvis.persistence.migrations import _SCHEMA_V12, migrate
from jarvis.projects import ProjectStore

_OPEN: list = []


@pytest.fixture(autouse=True)
async def _close():
    yield
    while _OPEN:
        await _OPEN.pop().close()


async def _store(tmp_path: Path) -> GraphStore:
    db = await connect(tmp_path / "graph.db")
    _OPEN.append(db)
    return GraphStore(db, asyncio.Lock())


# --- migration -------------------------------------------------------------
async def test_migration_v12_applied_and_tables_exist(tmp_path: Path) -> None:
    db = await connect(tmp_path / "g.db")
    _OPEN.append(db)
    assert (await (await db.execute("PRAGMA user_version")).fetchone())[0] == 12
    names = {
        r[0] for r in await (await db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")).fetchall()
    }
    assert {"graph_nodes", "graph_edges", "graph_suggestions", "graph_merges"} <= names


async def test_migration_v12_script_is_idempotent(tmp_path: Path) -> None:
    db = await connect(tmp_path / "g.db")
    _OPEN.append(db)
    # Every statement is IF NOT EXISTS: re-running the whole v12 script is a safe no-op, and
    # migrate() itself is a no-op once at v12.
    await db.executescript(_SCHEMA_V12)
    await db.executescript(_SCHEMA_V12)
    assert await migrate(db) == 12


# --- asserted nodes: never-DELETE (retract) --------------------------------
async def test_node_roundtrip_and_retract_keeps_the_row(tmp_path: Path) -> None:
    s = await _store(tmp_path)
    nid = await s.add_node(kind="decision", title="Adopt SQLite", summary="chose sqlite+numpy",
                           trust_class="reviewed", created_by="user", labels=["arch"])
    node = await s.get_node(nid)
    assert node and node.kind == "decision" and node.trust_class == "reviewed"
    assert node.ref == f"decision:{nid}" and node.labels == ["arch"] and node.status == "live"

    assert await s.retract_node(nid) is True
    assert await s.retract_node(nid) is False  # idempotent: already retracted
    assert (await s.get_node(nid)).status == "retracted"  # row survives (audit)
    assert nid not in {n.id for n in await s.list_nodes()}  # gone from live reads


async def test_node_trust_class_is_constrained(tmp_path: Path) -> None:
    s = await _store(tmp_path)
    with pytest.raises(sqlite3.IntegrityError):
        await s.add_node(kind="topic", title="x", trust_class="totally_trusted", created_by="user")


async def test_node_project_scope(tmp_path: Path) -> None:
    db = await connect(tmp_path / "g.db")
    _OPEN.append(db)
    lock = asyncio.Lock()
    await ProjectStore(db, lock).create(name="P")  # id 1 (FK target)
    s = GraphStore(db, lock)
    g = await s.add_node(kind="topic", title="global", trust_class="trusted_local",
                         created_by="user")
    p = await s.add_node(kind="topic", title="scoped", trust_class="trusted_local",
                         created_by="user", project_id=1)
    # project scope returns P's + global; global-only excludes P's.
    assert {g, p} == {n.id for n in await s.list_nodes(project_id=1)}
    assert {g} == {n.id for n in await s.list_nodes(project_id=None)}


# --- edges: derived cache vs asserted --------------------------------------
async def test_edge_upsert_is_idempotent_and_preserves_created_at(tmp_path: Path) -> None:
    s = await _store(tmp_path)
    kw = dict(src_kind="project", src_id="1", dst_kind="run", dst_id="9", edge_kind="ran_in",
              origin="derived", trust_class="trusted_local", created_by="system")
    await s.upsert_edge(created_at="2026-01-01T00:00:00+00:00", **kw)
    await s.upsert_edge(created_at="2026-09-09T00:00:00+00:00", **kw)  # same identity → update
    edges = await s.neighbors("project", "1")
    assert len(edges) == 1  # one row, not two
    # the original created_at is preserved on conflict (rebuild determinism)
    assert edges[0].created_at == "2026-01-01T00:00:00+00:00"


async def test_delete_derived_never_touches_asserted(tmp_path: Path) -> None:
    s = await _store(tmp_path)
    await s.upsert_edge(src_kind="project", src_id="1", dst_kind="chat", dst_id="2",
                        edge_kind="has_chat", origin="derived", trust_class="trusted_local",
                        created_by="system", created_at="2026-01-01T00:00:00+00:00")
    await s.upsert_edge(src_kind="decision", src_id="4", dst_kind="topic", dst_id="5",
                        edge_kind="relates_to", origin="asserted", trust_class="reviewed",
                        created_by="user", created_at="2026-01-01T00:00:00+00:00")
    assert await s.delete_derived_edges() == 1
    remaining = await s.list_edges()
    assert [e.origin for e in remaining] == ["asserted"]  # only the asserted edge survives


# --- suggestions: quarantined, single transition ---------------------------
async def test_suggestion_is_pending_then_resolves_once(tmp_path: Path) -> None:
    s = await _store(tmp_path)
    sid = await s.add_suggestion(
        kind="memory", payload={"content": "X uses Skiff"}, trust_class="untrusted_external",
        evidence=[{"kind": "chat", "id": 2}], extractor_model="claude-opus-4-8", est_cost_usd=0.001)
    assert [x.id for x in await s.list_suggestions(status="pending")] == [sid]
    assert (await s.get_suggestion(sid)).trust_class == "untrusted_external"

    assert await s.resolve_suggestion(sid, status="approved", resolved_by="user") is True
    # a second resolve of an already-resolved suggestion is a no-op
    assert await s.resolve_suggestion(sid, status="rejected", resolved_by="user") is False
    assert (await s.get_suggestion(sid)).status == "approved"
    assert await s.list_suggestions(status="pending") == []


async def test_suggestions_never_appear_in_the_entities_fts(tmp_path: Path) -> None:
    # A suggestion is NOT a graph_nodes row, so the entities FTS can never surface it — quarantine
    # by construction. Only an asserted node with matching text is found.
    s = await _store(tmp_path)
    await s.add_suggestion(kind="node", payload={"title": "Zorblax reactor"},
                           trust_class="untrusted_external")
    assert await query_domain(s.db, "entities", "Zorblax") == []
    nid = await s.add_node(kind="topic", title="Zorblax reactor", trust_class="reviewed",
                           created_by="user")
    assert [rid for rid, _ in await query_domain(s.db, "entities", "Zorblax")] == [nid]


async def test_entities_fts_excludes_retracted_nodes(tmp_path: Path) -> None:
    s = await _store(tmp_path)
    nid = await s.add_node(kind="person", title="Ada Lovelace", trust_class="reviewed",
                           created_by="user")
    assert [rid for rid, _ in await query_domain(s.db, "entities", "Lovelace")] == [nid]
    await s.retract_node(nid)
    assert await query_domain(s.db, "entities", "Lovelace") == []  # static_where status='live'


# --- merge journal ---------------------------------------------------------
async def test_merge_journal_records_and_undoes(tmp_path: Path) -> None:
    s = await _store(tmp_path)
    mid = await s.record_merge(action="merge", canonical_kind="person", canonical_id="1",
                               merged_kind="person", merged_id="2", created_by="user",
                               undo={"reassigned_edges": [7]})
    assert [m.id for m in await s.list_merges()] == [mid]
    assert (await s.list_merges())[0].undo == {"reassigned_edges": [7]}
    assert await s.mark_merge_undone(mid) is True
    assert await s.list_merges() == []  # undone hidden by default
    assert [m.id for m in await s.list_merges(include_undone=True)] == [mid]


async def test_embedding_roundtrips_as_unit_vector(tmp_path: Path) -> None:
    s = await _store(tmp_path)
    nid = await s.add_node(kind="topic", title="t", trust_class="trusted_local", created_by="user")
    await s.set_embedding(nid, [3.0, 4.0], model="voyage-3-large", content_hash="abc")
    node = await s.get_node(nid)
    assert node.embedding_model == "voyage-3-large" and node.content_hash == "abc"
    assert np.allclose(node.embedding, [0.6, 0.8])  # unit-normalized
