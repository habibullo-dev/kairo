"""Projects grid read model + label/count store helpers (Phase 11 T9). All keyless.

The safety-critical property here is that setting a project's category label MERGES into
settings_json and never clobbers sibling overrides (model routes / budgets / roster).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from jarvis.persistence.db import connect
from jarvis.persistence.sessions import SessionStore
from jarvis.projects.service import ProjectService
from jarvis.projects.store import ProjectStore
from jarvis.ui.readmodels import UiServices, projects_overview, serialize_project

_OPEN: list = []


@pytest.fixture(autouse=True)
async def _close():
    yield
    while _OPEN:
        await _OPEN.pop().close()


async def _db(tmp_path: Path):
    db = await connect(tmp_path / "t.db")
    _OPEN.append(db)
    return db, asyncio.Lock()


async def test_set_label_merges_without_clobbering_settings(tmp_path: Path) -> None:
    db, lock = await _db(tmp_path)
    store = ProjectStore(db, lock)
    pid = await store.create(name="Alpha", settings={"model_route": "x"})
    assert await store.set_label(pid, "Coding") is True
    p = await store.get(pid)
    assert p.settings["label"] == "Coding"
    assert p.settings["model_route"] == "x"  # sibling override preserved — NOT clobbered
    # clearing the label leaves the rest intact
    assert await store.set_label(pid, None) is True
    p = await store.get(pid)
    assert "label" not in p.settings and p.settings["model_route"] == "x"
    assert await store.set_label(9999, "X") is False  # missing project


async def test_serialize_project_exposes_pinned_and_label(tmp_path: Path) -> None:
    db, lock = await _db(tmp_path)
    store = ProjectStore(db, lock)
    pid = await store.create(name="Beta")
    await store.set_pinned(pid, True)
    await store.set_label(pid, "Personal")
    d = serialize_project(await store.get(pid))
    assert d["pinned"] is True and d["label"] == "Personal"


async def test_session_count_since_empty_is_zero(tmp_path: Path) -> None:
    db, lock = await _db(tmp_path)
    sessions = SessionStore(db, lock)
    assert await sessions.count_since("2000-01-01T00:00:00+00:00") == 0


async def test_projects_overview_degrades_without_services() -> None:
    out = await projects_overview(UiServices())
    assert out == {"projects": [], "archived": [], "active_project_id": None}


async def test_projects_overview_lists_active_pinned_first(tmp_path: Path) -> None:
    db, lock = await _db(tmp_path)
    store = ProjectStore(db, lock)
    await store.create(name="Alpha")
    b = await store.create(name="Beta")
    await store.set_pinned(b, True)
    svc = ProjectService(store)
    out = await projects_overview(UiServices(projects=svc, sessions=SessionStore(db, lock)))
    names = [p["name"] for p in out["projects"]]
    assert names[0] == "Beta"  # pinned first
    assert set(names) == {"Alpha", "Beta"}
    # health chips: sessions store present -> 0; the absent stores stay None (degraded, not error)
    h = out["projects"][0]["health"]
    assert h["sessions_week"] == 0 and h["open_tasks"] is None and h["last_run"] is None
    assert out["active_project_id"] is None
