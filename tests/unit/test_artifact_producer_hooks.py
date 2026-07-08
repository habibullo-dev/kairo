"""Artifact producer hook (Phase 11 T3): KnowledgeService.write_page registers a confined
local-file artifact through a REAL producer (exercises the wiki managed-root confinement +
identity-by-path dedupe end to end). The other three hooks (digest/orchestration/meeting) share
this guarded, fail-soft register() pattern; register() correctness itself is covered store-side
in test_artifacts_store.py. Keyless."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from jarvis.config import KnowledgeConfig
from jarvis.knowledge.service import KnowledgeService
from jarvis.knowledge.store import KnowledgeStore
from jarvis.memory.embeddings import FakeEmbedder
from jarvis.persistence.artifacts import ArtifactStore
from jarvis.persistence.db import connect

_OPEN: list = []


@pytest.fixture(autouse=True)
async def _close():
    yield
    while _OPEN:
        await _OPEN.pop().close()


async def _service(tmp_path: Path):
    db = await connect(tmp_path / "kb.db")
    _OPEN.append(db)
    lock = asyncio.Lock()
    knowledge_dir = tmp_path / "knowledge"
    artifacts = ArtifactStore(
        db, lock, data_dir=tmp_path, managed_roots={"wiki": knowledge_dir / "wiki"}
    )
    svc = KnowledgeService(
        KnowledgeStore(db, lock),
        FakeEmbedder(),
        KnowledgeConfig(),
        knowledge_dir=knowledge_dir,
        root=tmp_path,
        artifacts=artifacts,
    )
    svc.ensure_dirs()
    return svc, artifacts, knowledge_dir


async def test_write_page_registers_confined_wiki_artifact(tmp_path: Path) -> None:
    svc, artifacts, knowledge_dir = await _service(tmp_path)
    await svc.write_page("topics/rust.md", "# Rust\n\nTokio is a runtime.", created_by="agent")

    arts = await artifacts.list()
    assert len(arts) == 1
    art = arts[0]
    assert art.origin_type == "wiki" and art.kind == "wiki_page"
    assert art.origin_id == "topics/rust.md"  # relative wiki path = stable identity
    assert art.local_path == "knowledge/wiki/topics/rust.md"  # stored relative to the data dir
    assert art.project_id is None  # wiki is global
    # content_path re-confines and resolves back to the real on-disk file.
    assert artifacts.content_path(art) == (knowledge_dir / "wiki" / "topics" / "rust.md").resolve()


async def test_write_page_reedit_updates_same_artifact(tmp_path: Path) -> None:
    svc, artifacts, _ = await _service(tmp_path)
    await svc.write_page("p.md", "# Page\n\nv1 body")
    await svc.write_page("p.md", "# Page v2\n\nv2 body")
    # Same wiki path → same origin identity → one artifact, updated in place (not duplicated).
    assert len(await artifacts.list()) == 1
