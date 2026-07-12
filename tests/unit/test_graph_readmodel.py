"""GraphService read models (Phase 15 Task 3). subgraph() resolves derived-edge endpoints into
bodies-free node cards, is project-scoped, depth/size-clamped, and filterable by kind/trust;
node_card() gives one node + capped neighbors. Keyless: temp DB, seed rows + the deterministic
builder, then read."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from jarvis.agents import AgentRunStore
from jarvis.graph import GraphStore
from jarvis.graph.builder import rebuild
from jarvis.graph.service import MAX_DEPTH, node_card, subgraph
from jarvis.orchestration import OrchestrationStore
from jarvis.persistence.db import connect
from jarvis.projects import ProjectStore

_OPEN: list = []
_TS = "2026-03-15T00:00:00+00:00"


@pytest.fixture(autouse=True)
async def _close():
    yield
    while _OPEN:
        await _OPEN.pop().close()


async def _seed(tmp_path: Path):
    db = await connect(tmp_path / "g.db")
    _OPEN.append(db)
    lock = asyncio.Lock()
    ps = ProjectStore(db, lock)
    await ps.create(name="Alpha")  # 1
    await ps.create(name="Bravo")  # 2 (for the scoping test)
    orch, runs = OrchestrationStore(db, lock), AgentRunStore(db, lock)
    rid = await orch.begin_run(project_id=1, workflow="security_review", title="Sec review",
                               config={"team": "security"}, context_manifest=[],
                               estimated_cost_usd=0.1, budget_usd=1.0)
    await runs.begin_run(parent_session_id=None, parent_trace_id=None, title="security:lead",
                         prompt="SECRET-PROMPT-CANARY", tools_scope=["read_file"], project_id=1,
                         orchestration_run_id=rid, role="security", stage="council")
    await db.execute("INSERT INTO sessions (created_at, updated_at, title, kind, project_id) "
                     "VALUES (?, ?, 'Alpha chat', 'interactive', 1)", (_TS, _TS))
    await db.execute("INSERT INTO sessions (created_at, updated_at, title, kind, project_id) "
                     "VALUES (?, ?, 'Bravo chat', 'interactive', 2)", (_TS, _TS))
    await db.execute(
        "INSERT INTO kb_sources (kind, origin, content_hash, raw_path, markdown_path, "
        "markdown_hash, converter, converter_version, byte_size, status, review_status, "
        "created_by, created_at, updated_at, project_id) VALUES "
        "('url','http://x','h','r','m','mh','trafilatura','1',10,'live','unreviewed','agent',?,?,1)",
        (_TS, _TS))
    await db.commit()
    store = GraphStore(db, lock)
    await rebuild(store)
    return store, rid


async def test_project_subgraph_has_focus_and_neighbors(tmp_path: Path) -> None:
    store, rid = await _seed(tmp_path)
    sg = await subgraph(store, 1, depth=1)
    assert sg["focus"] == "project:1"
    kinds = {n["kind"] for n in sg["nodes"]}
    assert {"project", "chat", "run", "source"} <= kinds  # project + its direct neighbors
    assert sg["counts"]["by_kind"]["chat"] == 1
    # the run's team + members are 2 hops away — absent at depth 1, present at depth 2.
    assert "member" not in kinds
    deep = await subgraph(store, 1, depth=2)
    assert {"member", "team"} <= {n["kind"] for n in deep["nodes"]}


async def test_subgraph_is_project_scoped(tmp_path: Path) -> None:
    store, _ = await _seed(tmp_path)
    sg = await subgraph(store, 1, depth=2)
    labels = {n["label"] for n in sg["nodes"]}
    assert "Alpha chat" in labels and "Bravo chat" not in labels  # no cross-project leak


async def test_depth_and_limit_are_clamped(tmp_path: Path) -> None:
    store, _ = await _seed(tmp_path)
    assert (await subgraph(store, 1, depth=99))["depth"] == MAX_DEPTH  # clamped to 6
    tiny = await subgraph(store, 1, depth=2, limit=1)
    assert len(tiny["nodes"]) == 1 and tiny["truncated"] is True


async def test_trust_and_kind_filters(tmp_path: Path) -> None:
    store, _ = await _seed(tmp_path)
    sg = await subgraph(store, 1, depth=2)
    src = next(n for n in sg["nodes"] if n["kind"] == "source")
    assert src["trust_class"] == "untrusted_external"  # url + unreviewed, never upgraded
    only_src = await subgraph(store, 1, depth=2, kinds={"source"})
    assert {n["kind"] for n in only_src["nodes"]} == {"source"}


async def test_rejected_source_is_not_resolved_even_if_a_stale_edge_survives(
    tmp_path: Path,
) -> None:
    store, _ = await _seed(tmp_path)
    # Simulate an interrupted lifecycle action: graph edge still exists, but the source
    # itself was rejected.  The read model must not turn that stale edge back into a card.
    await store.db.execute("UPDATE kb_sources SET status='rejected' WHERE project_id=1")
    await store.db.commit()

    sg = await subgraph(store, 1, depth=2)

    assert all(node["kind"] != "source" for node in sg["nodes"])
    assert all(
        ":source:" not in edge["src"] and ":source:" not in edge["dst"]
        for edge in sg["edges"]
    )


async def test_empty_project_degrades_to_focus_only(tmp_path: Path) -> None:
    store, _ = await _seed(tmp_path)
    # project 2 (Bravo) has a chat edge; a project with NO edges returns just its focus node.
    await ProjectStore(store.db, store.lock).create(name="Empty")  # id 3
    sg = await subgraph(store, 3, depth=2)
    assert [n["id"] for n in sg["nodes"]] == ["project:3"] and sg["edges"] == []


async def test_node_card_is_bodies_free(tmp_path: Path) -> None:
    store, rid = await _seed(tmp_path)
    card = await node_card(store, "run", str(rid))
    assert card and card["kind"] == "run"
    # the run's members are neighbors, labelled by their short title — never the prompt body.
    assert any(nb["node"]["kind"] == "member" for nb in card["neighbors"])
    assert "SECRET-PROMPT-CANARY" not in str(card)


async def test_node_card_unknown_returns_none(tmp_path: Path) -> None:
    store, _ = await _seed(tmp_path)
    assert await node_card(store, "run", "99999") is None
