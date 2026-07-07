"""SessionStore list/search/pin/set_project + project scoping (Phase 10 Task 2).

Keyless, tmp SQLite. Proves the chats list is metadata-only, scoping filters correctly
(a project, global-only, or any), search spans titles + message content, and pinning /
re-scoping round-trip — while REPL --resume (latest_session_id) stays interactive-only."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from jarvis.persistence import SessionStore
from jarvis.persistence.db import connect
from jarvis.projects import ProjectStore

_OPEN: list = []


@pytest.fixture(autouse=True)
async def _close():
    yield
    while _OPEN:
        await _OPEN.pop().close()


async def _store(tmp_path: Path) -> SessionStore:
    db = await connect(tmp_path / "s.db")
    _OPEN.append(db)
    lock = asyncio.Lock()
    # Two real projects (ids 1 and 2) so session.project_id FKs resolve — the FK is enforced,
    # which is the point: a session can't reference a project that doesn't exist.
    projects = ProjectStore(db, lock)
    await projects.create(name="Project One")
    await projects.create(name="Project Two")
    return SessionStore(db, lock)


async def _chat(
    store: SessionStore, text: str, *, project_id: int | None = None, kind: str = "interactive"
) -> int:
    sid = await store.create_session(kind=kind, project_id=project_id)
    await store.save_messages(sid, [{"role": "user", "content": text}])
    return sid


async def test_list_is_metadata_only_and_counts_messages(tmp_path: Path) -> None:
    store = await _store(tmp_path)
    sid = await store.create_session()
    await store.save_messages(
        sid,
        [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
        ],
    )
    rows = await store.list_sessions()
    assert len(rows) == 1
    assert rows[0].id == sid and rows[0].message_count == 2 and rows[0].pinned is False


async def test_empty_sessions_are_hidden(tmp_path: Path) -> None:
    store = await _store(tmp_path)
    await store.create_session()  # lazily created but never got a turn
    assert await store.list_sessions() == []  # no messages ⇒ not listed


async def test_scope_filters(tmp_path: Path) -> None:
    store = await _store(tmp_path)
    g = await _chat(store, "global chat")
    a = await _chat(store, "project A chat", project_id=1)
    b = await _chat(store, "project B chat", project_id=2)

    # Default: any project.
    assert {r.id for r in await store.list_sessions()} == {g, a, b}
    # A specific project.
    assert {r.id for r in await store.list_sessions(project_id=1)} == {a}
    # Global only (None) must NOT include project chats.
    assert {r.id for r in await store.list_sessions(project_id=None)} == {g}


async def test_kind_filter_excludes_task_and_subagent(tmp_path: Path) -> None:
    store = await _store(tmp_path)
    inter = await _chat(store, "interactive")
    await _chat(store, "job transcript", kind="task")
    rows = await store.list_sessions()  # default kind='interactive'
    assert {r.id for r in rows} == {inter}


async def test_search_titles_and_content(tmp_path: Path) -> None:
    store = await _store(tmp_path)
    s1 = await store.create_session(title="Rust async notes")
    await store.save_messages(s1, [{"role": "user", "content": "unrelated body"}])
    s2 = await store.create_session(title="grocery list")
    await store.save_messages(s2, [{"role": "user", "content": "how does tokio scheduling work"}])

    assert {r.id for r in await store.search_sessions("rust")} == {s1}  # title hit
    assert {r.id for r in await store.search_sessions("tokio")} == {s2}  # content hit
    assert await store.search_sessions("nonexistent") == []


async def test_search_respects_scope(tmp_path: Path) -> None:
    store = await _store(tmp_path)
    a = await _chat(store, "shared keyword alpha", project_id=1)
    await _chat(store, "shared keyword alpha", project_id=2)
    assert {r.id for r in await store.search_sessions("alpha", project_id=1)} == {a}


async def test_pin_round_trip_and_ordering(tmp_path: Path) -> None:
    store = await _store(tmp_path)
    first = await _chat(store, "older")
    second = await _chat(store, "newer")
    assert await store.set_pinned(first, True) is True
    rows = await store.list_sessions()
    assert rows[0].id == first and rows[0].pinned is True  # pinned floats to top
    assert {r.id for r in await store.list_sessions(pinned=True)} == {first}
    assert {r.id for r in await store.list_sessions(pinned=False)} == {second}
    await store.set_pinned(first, False)
    assert (await store.get_meta(first)).pinned is False


async def test_set_project_rescopes(tmp_path: Path) -> None:
    store = await _store(tmp_path)
    sid = await _chat(store, "was global")
    assert (await store.get_meta(sid)).project_id is None
    assert await store.set_project(sid, 2) is True  # project id 2 exists (created in _store)
    assert (await store.get_meta(sid)).project_id == 2


async def test_latest_session_id_still_interactive_only(tmp_path: Path) -> None:
    # REPL --resume must never land in a task/project chat it shouldn't; the addition of
    # project scoping doesn't change that latest_session_id is interactive-kind only.
    store = await _store(tmp_path)
    inter = await _chat(store, "interactive one")
    await _chat(store, "job", kind="task")
    assert await store.latest_session_id() == inter


async def test_get_meta_missing(tmp_path: Path) -> None:
    store = await _store(tmp_path)
    assert await store.get_meta(999) is None
    assert await store.set_pinned(999, True) is False
