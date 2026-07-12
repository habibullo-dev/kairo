"""Deterministic graph builder (Phase 15 Task 2). The derived edge cache is a rebuildable
projection of existing rows: rebuild delete+re-derives (asserted rows untouched), every edge carries
its SOURCE row's created_at (never wall-clock), trust is mapped from the content endpoint and never
upgraded, and two rebuilds yield identical edge CONTENT. Keyless: a temp DB + real stores + a few
raw seed rows with a fixed timestamp."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from jarvis.agents import AgentRunStore
from jarvis.graph import GraphStore
from jarvis.graph.builder import rebuild
from jarvis.graph.service import dependency_subgraph, subgraph
from jarvis.orchestration import OrchestrationStore
from jarvis.persistence.db import connect
from jarvis.projects import ProjectStore

_OPEN: list = []
_TS = "2026-03-15T00:00:00+00:00"  # a fixed PAST time — proves edges use the source row's time


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
    orch, runs = OrchestrationStore(db, lock), AgentRunStore(db, lock)
    rid = await orch.begin_run(project_id=1, workflow="security_review", title="Sec",
                               config={"team": "security"}, context_manifest=[],
                               estimated_cost_usd=0.1, budget_usd=1.0)
    await runs.begin_run(parent_session_id=None, parent_trace_id=None, title="m", prompt="p",
                         tools_scope=["read_file"], project_id=1, orchestration_run_id=rid,
                         role="security", stage="council")
    # Raw rows with a FIXED created_at (so a derived edge's time can be asserted exactly).
    await db.execute(
        "INSERT INTO sessions (created_at, updated_at, title, kind, project_id) "
        "VALUES (?, ?, 'S', 'interactive', 1)", (_TS, _TS))
    await db.execute(
        "INSERT INTO kb_sources (kind, origin, content_hash, raw_path, markdown_path, "
        "markdown_hash, converter, converter_version, byte_size, status, review_status, "
        "created_by, created_at, updated_at, project_id) VALUES "
        "('url','http://x','h','r','m','mh','trafilatura','1',10,'live','unreviewed','agent',?,?,1)",
        (_TS, _TS))
    await db.execute(
        "INSERT INTO artifacts (project_id, kind, title, external_uri, origin_type, origin_id, "
        "created_by, provenance_class, created_at, updated_at) "
        "VALUES (1,'digest','A','kairo://a','orchestration',?,'agent','trusted_local',?,?)",
        (str(rid), _TS, _TS))
    await db.execute(
        "INSERT INTO kb_wiki_links (from_path, to_path, to_raw, link_kind, created_at) "
        "VALUES ('pages/a.md','pages/b.md','b','wikilink',?)", (_TS,))
    await db.commit()
    return GraphStore(db, lock), rid


def _content(edges):
    """Edge identity + metadata + time, EXCLUDING the autoincrement id (which increments across
    rebuilds); this is what determinism means for the cache."""
    return sorted(
        (e.src_kind, e.src_id, e.dst_kind, e.dst_id, e.edge_kind, e.origin, e.trust_class,
         e.created_at, e.project_id) for e in edges
    )


async def test_rebuild_derives_expected_edges(tmp_path: Path) -> None:
    store, rid = await _seed(tmp_path)
    counts = await rebuild(store)
    assert counts["has_chat"] == 1 and counts["has_run"] == 1 and counts["uses_team"] == 1
    assert counts["has_member"] == 1 and counts["has_source"] == 1 and counts["has_artifact"] == 1
    assert counts["produced_by"] == 1 and counts["links_to"] == 1
    by_kind = {e.edge_kind: e for e in await store.list_edges()}
    # run -> team endpoint is the team constant; artifact -> run connects via origin_id.
    assert by_kind["uses_team"].dst_kind == "team" and by_kind["uses_team"].dst_id == "security"
    assert by_kind["produced_by"].dst_kind == "run" and by_kind["produced_by"].dst_id == str(rid)


async def test_trust_is_mapped_from_content_endpoint(tmp_path: Path) -> None:
    store, _ = await _seed(tmp_path)
    await rebuild(store)
    by_kind = {e.edge_kind: e for e in await store.list_edges()}
    assert by_kind["has_source"].trust_class == "untrusted_external"  # url + unreviewed
    assert by_kind["has_chat"].trust_class == "trusted_local"  # structural


async def test_rebuild_derives_logical_folder_tree_for_uploaded_project_sources(
    tmp_path: Path,
) -> None:
    store, _ = await _seed(tmp_path)
    await store.db.execute(
        "INSERT INTO kb_sources (kind, origin, title, content_hash, raw_path, markdown_path, "
        "markdown_hash, converter, converter_version, byte_size, status, review_status, "
        "created_by, created_at, updated_at, project_id) VALUES "
        "('file','chat-upload:1:repo/src/main.py','repo/src/main.py','folder-hash','r2','m2',"
        "'mh2','passthrough','1',10,'live','reviewed','user',?,?,1)",
        (_TS, _TS),
    )
    await store.db.commit()

    counts = await rebuild(store)
    edges = await store.list_edges(origin="derived")
    assert counts["contains"] == 3
    assert any(
        e.src_kind == "project" and e.dst_kind == "folder" and e.dst_id == "1:repo"
        for e in edges
    )
    assert any(
        e.src_kind == "folder" and e.src_id == "1:repo" and e.dst_id == "1:repo/src"
        for e in edges
    )
    assert any(e.src_kind == "folder" and e.dst_kind == "source" for e in edges)


async def test_rebuild_derives_resolved_local_code_imports_only(tmp_path: Path) -> None:
    store, _ = await _seed(tmp_path)
    source_ids: dict[str, int] = {}
    for title, text in (
        ("repo/src/kairo/app.py", "from .core import runner\n"),
        ("repo/src/kairo/core.py", "def runner(): pass\n"),
        ("repo/src/kairo/other.py", "import external_package\n"),
    ):
        cur = await store.db.execute(
            "INSERT INTO kb_sources (kind, origin, title, content_hash, raw_path, markdown_path, "
            "markdown_hash, converter, converter_version, byte_size, status, review_status, "
            "created_by, created_at, updated_at, project_id) VALUES "
            "('file', ?, ?, ?, 'r', 'm', 'mh', 'passthrough', '1', 10, 'live', 'reviewed', "
            "'user', ?, ?, 1)",
            (f"chat-upload:1:{title}", title, f"hash-{title}", _TS, _TS),
        )
        assert cur.lastrowid is not None
        source_ids[title] = cur.lastrowid
        await store.db.execute(
            "INSERT INTO kb_chunks (source_id, wiki_path, heading_path, seq, text, embedding, "
            "embedding_model, created_at) VALUES (?, NULL, '', 0, ?, X'00', 'fake', ?)",
            (cur.lastrowid, text, _TS),
        )
    await store.db.commit()

    counts = await rebuild(store)
    imports = [
        edge for edge in await store.list_edges(origin="derived") if edge.edge_kind == "imports"
    ]
    assert counts["imports"] == 1
    assert [(edge.src_id, edge.dst_id, edge.trust_class) for edge in imports] == [
        (
            str(source_ids["repo/src/kairo/app.py"]),
            str(source_ids["repo/src/kairo/core.py"]),
            "reviewed",
        )
    ]
    code_map = await dependency_subgraph(store, 1)
    assert code_map["view"] == "dependencies"
    assert [(edge["src"], edge["dst"]) for edge in code_map["edges"]] == [
        (
            f"source:{source_ids['repo/src/kairo/app.py']}",
            f"source:{source_ids['repo/src/kairo/core.py']}",
        )
    ]
    assert {node["community"] for node in code_map["nodes"]} == {"kairo"}
    deep_tree = await subgraph(store, 1, depth=6)
    node_ids = {node["id"] for node in deep_tree["nodes"]}
    assert f"source:{source_ids['repo/src/kairo/app.py']}" in node_ids


async def test_derived_edge_uses_source_row_created_at(tmp_path: Path) -> None:
    store, _ = await _seed(tmp_path)
    await rebuild(store)
    by_kind = {e.edge_kind: e for e in await store.list_edges()}
    # the session/source/wiki rows were seeded at the fixed PAST _TS — the edge carries THAT time,
    # not the (later) rebuild wall-clock.
    assert by_kind["has_chat"].created_at == _TS
    assert by_kind["has_source"].created_at == _TS
    assert by_kind["links_to"].created_at == _TS


async def test_rebuild_is_deterministic(tmp_path: Path) -> None:
    store, _ = await _seed(tmp_path)
    await rebuild(store)
    first = _content(await store.list_edges())
    await rebuild(store)  # run again over the same data
    assert _content(await store.list_edges()) == first  # byte-identical content


async def test_rebuild_never_touches_asserted_edges(tmp_path: Path) -> None:
    store, _ = await _seed(tmp_path)
    await rebuild(store)
    # a human-approved asserted edge (not derivable from any row)
    await store.upsert_edge(src_kind="decision", src_id="1", dst_kind="topic", dst_id="2",
                            edge_kind="relates_to", origin="asserted", trust_class="reviewed",
                            created_by="user", created_at=_TS)
    n_derived_before = len(await store.list_edges(origin="derived"))
    await rebuild(store)  # clears + re-derives the derived cache only
    asserted = await store.list_edges(origin="asserted")
    assert len(asserted) == 1 and asserted[0].edge_kind == "relates_to"  # survived the rebuild
    assert len(await store.list_edges(origin="derived")) == n_derived_before  # re-derived intact


async def test_rebuild_cli_runs_on_empty_db(tmp_path: Path) -> None:
    # The `jarvis graph rebuild` core: a fresh DB migrates to v12 and derives 0 edges (no source
    # rows), returning 0 — proves the CLI path wires config.data_dir -> store -> builder cleanly.
    from jarvis.cli.graph import _run_rebuild

    assert await _run_rebuild(tmp_path) == 0
