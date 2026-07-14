"""Graph-first project context and model-facing project isolation."""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path

import pytest

from jarvis.agents import SPAWNABLE
from jarvis.graph import GraphStore
from jarvis.intelligence.context import build_project_overview
from jarvis.knowledge.store import KnowledgeStore
from jarvis.persistence.db import connect
from jarvis.projects import ProjectContext, ProjectStore, seal_snapshot
from jarvis.tools import ToolContext
from jarvis.tools.builtin.project_graph import (
    _MAX_OUTPUT_CHARS,
    QueryProjectGraphTool,
    _render_bounded,
)

_OPEN: list = []


@pytest.fixture(autouse=True)
async def _close() -> None:
    yield
    while _OPEN:
        await _OPEN.pop().close()


async def _fixture(tmp_path: Path):
    db = await connect(tmp_path / "graph-tool.db")
    _OPEN.append(db)
    lock = asyncio.Lock()
    projects = ProjectStore(db, lock)
    knowledge = KnowledgeStore(db, lock)
    graph = GraphStore(db, lock)
    first = await projects.create(name="First")
    second = await projects.create(name="Second")
    return knowledge, graph, first, second


async def _source(
    knowledge: KnowledgeStore, *, project_id: int, path: str, content: bytes
) -> int:
    digest = hashlib.sha256(content).hexdigest()
    return await knowledge.add_source(
        kind="file",
        origin=f"chat-upload:{project_id}:{path}",
        title=path,
        content_hash=digest,
        raw_path=f"raw/{digest[:16]}",
        markdown_path=f"markdown/{digest[:16]}.md",
        markdown_hash=digest,
        converter="passthrough",
        converter_version="1",
        byte_size=len(content),
        mime="text/plain",
        review_status="reviewed",
        created_by="user",
        project_id=project_id,
    )


async def _edge(
    graph: GraphStore,
    *,
    project_id: int,
    src_kind: str,
    src_id: str,
    dst_kind: str,
    dst_id: str,
    edge_kind: str,
) -> None:
    await graph.upsert_edge(
        src_kind=src_kind,
        src_id=src_id,
        dst_kind=dst_kind,
        dst_id=dst_id,
        edge_kind=edge_kind,
        origin="derived",
        trust_class="reviewed",
        created_by="system",
        created_at="2026-07-14T00:00:00+00:00",
        project_id=project_id,
    )


def _context(project_id: int, graph: GraphStore) -> ToolContext:
    project = ProjectContext(project_id, "Active", (), "")
    return ToolContext(graph=graph, project=lambda: project)


async def test_query_project_graph_returns_only_active_project_dependencies(
    tmp_path: Path,
) -> None:
    knowledge, graph, first, second = await _fixture(tmp_path)
    app = await _source(knowledge, project_id=first, path="repo/app.py", content=b"app")
    core = await _source(knowledge, project_id=first, path="repo/core.py", content=b"core")
    foreign = await _source(
        knowledge, project_id=second, path="other/private.py", content=b"foreign"
    )
    await _edge(
        graph,
        project_id=first,
        src_kind="source",
        src_id=str(app),
        dst_kind="source",
        dst_id=str(core),
        edge_kind="imports",
    )
    await _edge(
        graph,
        project_id=second,
        src_kind="project",
        src_id=str(second),
        dst_kind="source",
        dst_id=str(foreign),
        edge_kind="contains",
    )

    tool = QueryProjectGraphTool(_context(first, graph))
    raw = await tool.run(tool.Params(view="dependencies", limit=20))
    assert isinstance(raw, str)
    data = json.loads(raw)
    labels = {node["label"] for node in data["nodes"]}
    assert labels == {"repo/app.py", "repo/core.py"}
    assert "other/private.py" not in raw
    assert data["project_id"] == first and len(data["edges"]) == 1


async def test_graph_tool_requires_an_active_project_and_is_read_only_spawnable(
    tmp_path: Path,
) -> None:
    _knowledge, graph, _first, _second = await _fixture(tmp_path)
    assert "query_project_graph" in SPAWNABLE
    assert QueryProjectGraphTool.is_available(ToolContext(graph=graph)) is False
    tool = QueryProjectGraphTool(ToolContext(graph=graph, project=lambda: None))
    result = await tool.run(tool.Params())
    assert result.is_error and "Select a project" in result.content
    assert tool.permission_default.value == "allow"
    assert tool.egress is False and tool.reads_private is False


def test_graph_renderer_terminates_with_one_edge_and_oversized_nodes() -> None:
    nodes = [
        {
            "id": f"source:{index}",
            "kind": "source",
            "label": f"repo/{'nested-' * 12}{index}.py",
            "degree": 1,
            "trust_class": "reviewed",
        }
        for index in range(120)
    ]
    rendered = _render_bounded(
        {
            "project_id": 1,
            "view": "dependencies",
            "counts": {"by_kind": {"source": 120}},
            "nodes": nodes,
            "edges": [
                {
                    "src": "source:0",
                    "dst": "source:1",
                    "edge_kind": "imports",
                    "origin": "derived",
                    "trust_class": "reviewed",
                }
            ],
        },
        limit=120,
    )
    payload = json.loads(rendered)
    assert len(rendered) <= _MAX_OUTPUT_CHARS
    assert payload["truncated"] is True
    assert len(payload["nodes"]) < len(nodes)


async def test_host_overview_is_bounded_bodies_free_and_secret_redacted(
    tmp_path: Path,
) -> None:
    knowledge, graph, first, _second = await _fixture(tmp_path)
    token = "sk-proj-" + "a" * 30
    secret_file = await _source(
        knowledge,
        project_id=first,
        path=f"repo/{token}.py",
        content=b"secret filename only",
    )
    ordinary = await _source(
        knowledge, project_id=first, path="repo/core.py", content=b"body-canary"
    )
    await _edge(
        graph,
        project_id=first,
        src_kind="source",
        src_id=str(secret_file),
        dst_kind="source",
        dst_id=str(ordinary),
        edge_kind="imports",
    )
    snapshot = await seal_snapshot(knowledge, graph, first)
    overview = await build_project_overview(
        snapshot, graph, max_files=1, max_nodes=20, max_chars=800
    )
    assert token not in overview.text
    assert "[REDACTED_SECRET:openai_key]" in overview.text
    assert "body-canary" not in overview.text  # source bodies are never read
    assert len(overview.text) <= 800
    assert overview.coverage["files_total"] == 2
    assert overview.coverage["files_listed"] == 1
    assert overview.coverage["files_omitted"] == 1
    assert overview.coverage["context_secret_hits"] >= 1


async def test_host_overview_rejects_nonpositive_limits(tmp_path: Path) -> None:
    knowledge, graph, first, _second = await _fixture(tmp_path)
    await _source(knowledge, project_id=first, path="repo/app.py", content=b"app")
    snapshot = await seal_snapshot(knowledge, graph, first)
    with pytest.raises(ValueError, match="positive"):
        await build_project_overview(snapshot, graph, max_files=0)
