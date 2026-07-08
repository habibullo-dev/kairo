"""projects.pinned plumbing (schema v9): default off, set/unset round-trips, unknown id."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from jarvis.persistence.db import connect
from jarvis.projects.store import ProjectStore

_OPEN: list = []


@pytest.fixture(autouse=True)
async def _close():
    yield
    while _OPEN:
        await _OPEN.pop().close()


async def _store(tmp_path: Path) -> ProjectStore:
    db = await connect(tmp_path / "p.db")
    _OPEN.append(db)
    return ProjectStore(db, asyncio.Lock())


async def test_pinned_defaults_false_and_round_trips(tmp_path: Path) -> None:
    store = await _store(tmp_path)
    pid = await store.create(name="Alpha")
    p = await store.get(pid)
    assert p is not None and p.pinned is False

    assert await store.set_pinned(pid, True) is True
    p = await store.get(pid)
    assert p is not None and p.pinned is True

    assert await store.set_pinned(pid, False) is True
    p = await store.get(pid)
    assert p is not None and p.pinned is False


async def test_set_pinned_unknown_project(tmp_path: Path) -> None:
    store = await _store(tmp_path)
    assert await store.set_pinned(9999, True) is False
