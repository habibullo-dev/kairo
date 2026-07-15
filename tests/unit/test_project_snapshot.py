"""Deterministic, bodies-free project snapshot sealing."""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path

import pytest

from kira.graph import GraphStore
from kira.knowledge.store import KnowledgeStore
from kira.persistence.db import connect
from kira.projects import ProjectStore, SnapshotError, seal_snapshot

_OPEN: list = []


@pytest.fixture(autouse=True)
async def _close() -> None:
    yield
    while _OPEN:
        await _OPEN.pop().close()


async def _services(tmp_path: Path):
    db = await connect(tmp_path / "snapshot.db")
    _OPEN.append(db)
    lock = asyncio.Lock()
    projects = ProjectStore(db, lock)
    knowledge = KnowledgeStore(db, lock)
    graph = GraphStore(db, lock)
    return projects, knowledge, graph


async def _source(
    knowledge: KnowledgeStore,
    *,
    project_id: int,
    path: str,
    body: bytes,
    reviewed: bool = True,
) -> int:
    digest = hashlib.sha256(body).hexdigest()
    return await knowledge.add_source(
        kind="file",
        origin=f"chat-upload:{project_id}:{path}",
        title=path,
        content_hash=digest,
        raw_path=f"raw/{digest[:16]}",
        markdown_path=f"markdown/{digest[:16]}.md",
        markdown_hash=hashlib.sha256(b"markdown:" + body).hexdigest(),
        converter="plain",
        converter_version="1",
        byte_size=len(body),
        mime="text/plain",
        review_status="reviewed" if reviewed else "unreviewed",
        created_by="user",
        project_id=project_id,
    )


async def _same_title_note(
    knowledge: KnowledgeStore,
    *,
    project_id: int,
    origin_suffix: str,
    reviewed: bool,
) -> int:
    body = b"identical note"
    digest = hashlib.sha256(body).hexdigest()
    return await knowledge.add_source(
        kind="note",
        origin=f"note:{project_id}:{origin_suffix}",
        title="Same title",
        content_hash=digest,
        raw_path=f"raw/{project_id}-{origin_suffix}",
        markdown_path=f"markdown/{project_id}-{origin_suffix}.md",
        markdown_hash=hashlib.sha256(b"markdown:" + body).hexdigest(),
        converter="plain",
        converter_version="1",
        byte_size=len(body),
        mime="text/plain",
        review_status="reviewed" if reviewed else "unreviewed",
        created_by="user",
        project_id=project_id,
    )


async def test_snapshot_is_order_independent_and_does_not_expose_managed_paths(
    tmp_path: Path,
) -> None:
    projects, knowledge, graph = await _services(tmp_path)
    first = await projects.create(name="First")
    second = await projects.create(name="Second")
    await _source(knowledge, project_id=first, path="repo/src/a.py", body=b"from . import b")
    await _source(knowledge, project_id=first, path="repo/src/b.py", body=b"VALUE = 1")
    await _source(knowledge, project_id=second, path="repo/src/b.py", body=b"VALUE = 1")
    await _source(knowledge, project_id=second, path="repo/src/a.py", body=b"from . import b")

    a = await seal_snapshot(knowledge, graph, first)
    b = await seal_snapshot(knowledge, graph, second)
    assert a.snapshot_hash == b.snapshot_hash
    assert [source.logical_path for source in a.sources] == ["repo/src/a.py", "repo/src/b.py"]
    assert all("raw/" not in source.logical_path for source in a.sources)
    assert a.coverage == {
        "files_total": 2,
        "files_reviewed": 2,
        "files_unreviewed": 0,
        "bytes_total": 24,
        "graph_edges": 0,
        "import_edges": 0,
    }


async def test_manifest_ties_do_not_fall_back_to_database_insertion_order(
    tmp_path: Path,
) -> None:
    projects, knowledge, graph = await _services(tmp_path)
    first = await projects.create(name="Tie first")
    second = await projects.create(name="Tie second")
    await _same_title_note(
        knowledge, project_id=first, origin_suffix="a", reviewed=True
    )
    await _same_title_note(
        knowledge, project_id=first, origin_suffix="b", reviewed=False
    )
    await _same_title_note(
        knowledge, project_id=second, origin_suffix="b", reviewed=False
    )
    await _same_title_note(
        knowledge, project_id=second, origin_suffix="a", reviewed=True
    )
    assert (await seal_snapshot(knowledge, graph, first)).snapshot_hash == (
        await seal_snapshot(knowledge, graph, second)
    ).snapshot_hash


async def test_content_or_conversion_change_moves_snapshot(tmp_path: Path) -> None:
    projects, knowledge, graph = await _services(tmp_path)
    project_id = await projects.create(name="Changed")
    old_id = await _source(
        knowledge, project_id=project_id, path="repo/app.py", body=b"print('old')"
    )
    before = await seal_snapshot(knowledge, graph, project_id)
    new_id = await _source(
        knowledge, project_id=project_id, path="repo/app.py", body=b"print('new')"
    )
    await knowledge.supersede_source(old_id, new_id)
    after = await seal_snapshot(knowledge, graph, project_id)
    assert after.snapshot_hash != before.snapshot_hash
    assert [source.source_id for source in after.sources] == [new_id]


async def test_graph_watermark_is_recorded_but_not_part_of_content_identity(
    tmp_path: Path,
) -> None:
    projects, knowledge, graph = await _services(tmp_path)
    project_id = await projects.create(name="Graph")
    source_id = await _source(
        knowledge, project_id=project_id, path="repo/app.py", body=b"print('stable')"
    )
    await graph.upsert_edge(
        src_kind="project",
        src_id=str(project_id),
        dst_kind="source",
        dst_id=str(source_id),
        edge_kind="contains",
        origin="derived",
        project_id=project_id,
        trust_class="reviewed",
        created_by="system",
        created_at="2026-07-14T00:00:00+00:00",
    )
    before = await seal_snapshot(knowledge, graph, project_id)
    await graph.upsert_edge(
        src_kind="source",
        src_id=str(source_id),
        dst_kind="folder",
        dst_id=f"{project_id}:repo",
        edge_kind="in_folder",
        origin="derived",
        project_id=project_id,
        trust_class="reviewed",
        created_by="system",
        created_at="2026-07-14T00:00:00+00:00",
    )
    after = await seal_snapshot(knowledge, graph, project_id)
    assert after.snapshot_hash == before.snapshot_hash
    assert after.graph_watermark > before.graph_watermark
    assert after.coverage["graph_edges"] == 2


async def test_empty_and_over_cap_projects_refuse_to_seal(tmp_path: Path) -> None:
    projects, knowledge, graph = await _services(tmp_path)
    project_id = await projects.create(name="Empty")
    with pytest.raises(SnapshotError, match="no live"):
        await seal_snapshot(knowledge, graph, project_id)
    await _source(knowledge, project_id=project_id, path="repo/a.py", body=b"a")
    await _source(knowledge, project_id=project_id, path="repo/b.py", body=b"b")
    with pytest.raises(SnapshotError, match="analysis cap"):
        await seal_snapshot(knowledge, graph, project_id, max_sources=1)
