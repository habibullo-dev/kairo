"""ProjectStore CRUD (Phase 10 Task 1): create/get/list/update/archive, slug uniqueness,
JSON round-trip, and the never-DELETE archive contract. Keyless, tmp SQLite."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from kira.persistence.db import connect
from kira.projects import Project, ProjectStore, slugify

_OPEN: list = []


@pytest.fixture(autouse=True)
async def _close():
    """Close every connection opened via _store after each test (aiosqlite spawns a worker
    thread; leaving it open GCs after the loop closes → a spurious ResourceWarning)."""
    yield
    while _OPEN:
        await _OPEN.pop().close()


async def _store(tmp_path: Path) -> ProjectStore:
    db = await connect(tmp_path / "p.db")
    _OPEN.append(db)
    return ProjectStore(db, asyncio.Lock())


def test_slugify() -> None:
    assert slugify("My Project!") == "my-project"
    assert slugify("  Spaces  &  Symbols  ") == "spaces-symbols"
    assert slugify("café_2026") == "caf-2026"  # non-alnum collapses
    assert slugify("!!!") == "project"  # empty base falls back


async def test_create_and_get(tmp_path: Path) -> None:
    store = await _store(tmp_path)
    pid = await store.create(
        name="Kira Web",
        description="the UI",
        color="#3b82f6",
        icon="globe",
        repos=["/repo/a"],
        settings={"model_routes": {"planner": "x"}},
    )
    p = await store.get(pid)
    assert isinstance(p, Project)
    assert p.name == "Kira Web" and p.slug == "kira-web" and p.status == "active"
    assert p.description == "the UI" and p.color == "#3b82f6" and p.icon == "globe"
    assert p.repos == ("/repo/a",)
    assert p.settings == {"model_routes": {"planner": "x"}}
    assert p.archived_at is None
    assert await store.get_by_slug("kira-web") == p


async def test_get_missing_returns_none(tmp_path: Path) -> None:
    store = await _store(tmp_path)
    assert await store.get(999) is None
    assert await store.get_by_slug("nope") is None


async def test_slug_uniqueness(tmp_path: Path) -> None:
    store = await _store(tmp_path)
    a = await store.create(name="Same Name")
    b = await store.create(name="Same Name")
    c = await store.create(name="Same Name")
    slugs = {(await store.get(a)).slug, (await store.get(b)).slug, (await store.get(c)).slug}
    assert slugs == {"same-name", "same-name-2", "same-name-3"}


async def test_defaults_json_columns(tmp_path: Path) -> None:
    store = await _store(tmp_path)
    p = await store.get(await store.create(name="Bare"))
    assert p.repos == () and p.settings == {}  # DEFAULT '[]' / '{}' decode cleanly


async def test_list_orders_and_filters(tmp_path: Path) -> None:
    store = await _store(tmp_path)
    a = await store.create(name="A")
    b = await store.create(name="B")
    await store.archive(a)
    active = await store.list(status="active")
    assert [p.id for p in active] == [b]  # a is archived, filtered out
    everything = await store.list()
    assert {p.id for p in everything} == {a, b}


async def test_update_fields_and_json(tmp_path: Path) -> None:
    store = await _store(tmp_path)
    pid = await store.create(name="Proj")
    assert await store.update(
        pid,
        description="new",
        color="#000",
        repos=["/x"],
        settings={"budgets": {"project_monthly_usd": 50}},
    )
    p = await store.get(pid)
    assert p.description == "new" and p.color == "#000"
    assert p.repos == ("/x",) and p.settings == {"budgets": {"project_monthly_usd": 50}}
    assert p.slug == "proj"  # slug is stable across updates


async def test_update_rejects_unknown_field(tmp_path: Path) -> None:
    store = await _store(tmp_path)
    pid = await store.create(name="Proj")
    with pytest.raises(ValueError, match="unknown project field"):
        await store.update(pid, slug="hijack")  # slug is not user-updatable


async def test_update_missing_returns_false(tmp_path: Path) -> None:
    store = await _store(tmp_path)
    assert await store.update(999, description="x") is False


async def test_archive_is_status_flip_not_delete(tmp_path: Path) -> None:
    store = await _store(tmp_path)
    pid = await store.create(name="Proj")
    assert await store.archive(pid) is True
    p = await store.get(pid)
    assert p is not None  # row kept, never DELETEd
    assert p.status == "archived" and p.archived_at is not None
    # archiving again on a missing id is False
    assert await store.archive(999) is False


async def test_set_status_validates(tmp_path: Path) -> None:
    store = await _store(tmp_path)
    pid = await store.create(name="Proj")
    assert await store.set_status(pid, "paused") is True
    assert (await store.get(pid)).status == "paused"
    with pytest.raises(ValueError, match="unknown project status"):
        await store.set_status(pid, "bogus")
