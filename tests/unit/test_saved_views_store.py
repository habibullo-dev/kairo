"""SavedViewStore (schema v9): create/update, scope+project listing, and delete. Keyless."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from jarvis.persistence.db import connect
from jarvis.persistence.saved_views import SavedViewStore
from jarvis.projects.store import ProjectStore

_OPEN: list = []


@pytest.fixture(autouse=True)
async def _close():
    yield
    while _OPEN:
        await _OPEN.pop().close()


async def _setup(tmp_path: Path):
    db = await connect(tmp_path / "views.db")
    _OPEN.append(db)
    lock = asyncio.Lock()
    return SavedViewStore(db, lock), ProjectStore(db, lock)


async def test_save_create_get(tmp_path: Path) -> None:
    store, _ = await _setup(tmp_path)
    vid = await store.save(name="Recent artifacts", scope="artifacts", query={"pinned": True})
    view = await store.get(vid)
    assert view is not None
    assert view.name == "Recent artifacts"
    assert view.scope == "artifacts"
    assert view.query == {"pinned": True}
    assert view.created_by == "user"


async def test_save_updates_in_place(tmp_path: Path) -> None:
    store, _ = await _setup(tmp_path)
    vid = await store.save(name="X", scope="search", query={"q": "old"})
    same = await store.save(name="X2", scope="search", query={"q": "new"}, view_id=vid)
    assert same == vid
    view = await store.get(vid)
    assert view is not None and view.name == "X2" and view.query == {"q": "new"}


async def test_save_with_missing_view_id_creates_new(tmp_path: Path) -> None:
    store, _ = await _setup(tmp_path)
    new_id = await store.save(name="N", scope="projects", view_id=9999)
    assert new_id != 9999
    assert await store.get(new_id) is not None


async def test_invalid_scope_rejected(tmp_path: Path) -> None:
    store, _ = await _setup(tmp_path)
    with pytest.raises(ValueError, match="unknown saved-view scope"):
        await store.save(name="bad", scope="not-a-scope")


async def test_list_by_scope_and_project(tmp_path: Path) -> None:
    store, projects = await _setup(tmp_path)
    a = await projects.create(name="Alpha")
    await store.save(name="AllArtifacts", scope="artifacts")  # global
    await store.save(name="ProjArtifacts", scope="artifacts", project_id=a)
    await store.save(name="AProjectsView", scope="projects")  # global, other scope

    arts = await store.list(scope="artifacts")
    assert {v.name for v in arts} == {"AllArtifacts", "ProjArtifacts"}

    proj_only = await store.list(scope="artifacts", project_id=a, include_global=False)
    assert {v.name for v in proj_only} == {"ProjArtifacts"}

    with_global = await store.list(scope="artifacts", project_id=a, include_global=True)
    assert {v.name for v in with_global} == {"AllArtifacts", "ProjArtifacts"}


async def test_delete(tmp_path: Path) -> None:
    store, _ = await _setup(tmp_path)
    vid = await store.save(name="Temp", scope="search")
    assert await store.delete(vid) is True
    assert await store.get(vid) is None
    assert await store.delete(vid) is False  # already gone
