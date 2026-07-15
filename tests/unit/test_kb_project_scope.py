"""Project-scoped KB retrieval: the A1 no-leak suite (Phase 10 Task 4).

The guarantee: text ingested only into project B is never retrievable from project A or
from the global scope via query_knowledge_base. A project sees its own sources + global;
global sees only global. Pre-Phase-10 sources (project_id NULL) behave as global. Curated
wiki pages stay visible in every scope (they have no source — documented).

Keyless: FakeEmbedder + a temp KnowledgeStore; min_similarity=0 so scope, not similarity,
is what's under test."""

from __future__ import annotations

from pathlib import Path

import pytest

from kira.config import KnowledgeConfig
from kira.graph import GraphStore
from kira.graph.builder import rebuild
from kira.knowledge.service import KnowledgeService
from kira.knowledge.store import KnowledgeStore
from kira.memory.embeddings import FakeEmbedder
from kira.persistence.db import connect
from kira.projects import ProjectStore

_OPEN: list = []


@pytest.fixture(autouse=True)
async def _close():
    yield
    while _OPEN:
        await _OPEN.pop().close()


async def _svc(tmp_path: Path) -> KnowledgeService:
    db = await connect(tmp_path / "kb.db")
    _OPEN.append(db)
    projects = ProjectStore(db)
    await projects.create(name="Project A")  # id 1
    await projects.create(name="Project B")  # id 2
    svc = KnowledgeService(
        KnowledgeStore(db),
        FakeEmbedder(),
        KnowledgeConfig(min_similarity=0.0),
        knowledge_dir=tmp_path / "knowledge",
        root=tmp_path,
    )
    svc.ensure_dirs()
    return svc


async def test_project_source_not_retrievable_from_other_project_or_global(tmp_path: Path) -> None:
    svc = await _svc(tmp_path)
    await svc.ingest(text="the beta blueprint canary lives here", title="b", project_id=2)

    q = "beta blueprint canary"
    # Retrievable in project B (its own scope).
    assert "canary" in await svc.query(q, project_id=2)
    # NOT retrievable from project A.
    assert "canary" not in await svc.query(q, project_id=1)
    # NOT retrievable from the global scope.
    assert "canary" not in await svc.query(q, project_id=None)


async def test_global_source_retrievable_everywhere(tmp_path: Path) -> None:
    svc = await _svc(tmp_path)
    await svc.ingest(text="the shared handbook canary is global", title="g")  # project_id=None

    for scope in (1, 2, None):
        assert "canary" in await svc.query("shared handbook canary", project_id=scope), scope


async def test_preexisting_sources_behave_global(tmp_path: Path) -> None:
    # A source ingested without a project (the pre-Phase-10 shape) is global — visible in
    # every project's scope. Proves the additive migration keeps old KB content reachable.
    svc = await _svc(tmp_path)
    await svc.ingest(text="legacy note canary from before projects", title="legacy")
    assert "canary" in await svc.query("legacy note canary", project_id=1)


async def test_unscoped_query_sees_everything(tmp_path: Path) -> None:
    # The default (no project layer / bare loop) is unscoped — byte-identical to Phase 9.
    svc = await _svc(tmp_path)
    await svc.ingest(text="project one canary", title="a", project_id=1)
    await svc.ingest(text="project two canary", title="b", project_id=2)
    out = await svc.query("canary")  # ANY_PROJECT default
    assert "project one canary" in out and "project two canary" in out


async def test_project_query_includes_only_its_local_import_metadata(tmp_path: Path) -> None:
    svc = await _svc(tmp_path)
    await svc.ingest(
        text="alpha unique-query-token\nfrom .core import runner",
        title="repo/src/kira/app.py",
        project_id=1,
    )
    await svc.ingest(
        text="CORE-DEPENDENCY-CANARY\ndef runner(): pass",
        title="repo/src/kira/core.py",
        project_id=1,
    )
    await svc.ingest(
        text="beta unique-query-token\nfrom .core import runner",
        title="other/src/kira/app.py",
        project_id=2,
    )
    await svc.ingest(
        text="PROJECT-B-DEPENDENCY-CANARY\ndef runner(): pass",
        title="other/src/kira/core.py",
        project_id=2,
    )
    await rebuild(GraphStore(svc.store.db, svc.store.lock))

    # One semantic hit finds app.py. GraphRAG then expands its verified project-local import
    # neighbor core.py, without retrieving the equivalent Project B module.
    project_a = await svc.query("alpha unique-query-token", top_k=1, project_id=1)
    assert "Project dependency metadata" in project_a
    assert "repo/src/kira/app.py imports repo/src/kira/core.py" in project_a
    assert "Direct dependency excerpts" in project_a
    assert "CORE-DEPENDENCY-CANARY" in project_a
    assert "other/src/kira/app.py" not in project_a
    assert "PROJECT-B-DEPENDENCY-CANARY" not in project_a


async def test_project_query_never_expands_to_an_unreviewed_import_neighbor(
    tmp_path: Path,
) -> None:
    svc = await _svc(tmp_path)
    await svc.ingest(
        text="reviewed query token\nfrom .core import runner",
        title="repo/src/app.py",
        project_id=1,
    )
    # Folder-style unattended imports remain quarantined until a person reviews them. A derived
    # graph edge must not let this file bypass the normal retrieval review gate as an expansion.
    svc.bound_unattended = True
    await svc.ingest(
        text="UNREVIEWED-DEPENDENCY-CANARY\ndef runner(): pass",
        title="repo/src/core.py",
        project_id=1,
    )
    await rebuild(GraphStore(svc.store.db, svc.store.lock))

    result = await svc.query("reviewed query token", top_k=1, project_id=1)
    assert "reviewed query token" in result
    assert "UNREVIEWED-DEPENDENCY-CANARY" not in result
    assert "Direct dependency excerpts" not in result
