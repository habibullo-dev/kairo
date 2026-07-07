"""UiSession persistence + resume (Phase 10 Task 2).

Before Phase 10 the workstation never persisted its conversation — chats vanished on
restart and were never reflected. These tests prove: a UI turn lazily creates ONE
interactive session row and saves the transcript; resume restores messages (+ frozen
compaction) into the live loop; the session is project-scoped at creation; and a UI
session becomes reflectable (kind interactive, reflected_at NULL after a save). Keyless
via FakeClient."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from jarvis.config import load_config
from jarvis.core import AgentLoop, FakeClient, build_system, text_message
from jarvis.permissions import PermissionGate, Policy
from jarvis.persistence import SessionStore
from jarvis.persistence.db import connect
from jarvis.persistence.sessions import INTERACTIVE_ONLY
from jarvis.projects import ProjectStore
from jarvis.tools import Permission, ToolContext, ToolExecutor, ToolRegistry
from jarvis.ui.connections import ConnectionManager
from jarvis.ui.session import UiSession

_OPEN: list = []


@pytest.fixture(autouse=True)
async def _close():
    yield
    while _OPEN:
        await _OPEN.pop().close()


async def _deny(_call, _decision) -> Permission:
    return Permission.DENY


def _loop(tmp_path: Path, client) -> AgentLoop:
    cfg = load_config(root=tmp_path, env_file=None)
    reg = ToolRegistry()
    reg.discover("jarvis.tools.builtin", ToolContext(config=cfg))
    return AgentLoop(
        client=client,
        registry=reg,
        executor=ToolExecutor(),
        gate=PermissionGate(Policy(), tmp_path),
        config=cfg,
        approver=_deny,
        system=build_system(),
    )


async def _store(tmp_path: Path) -> SessionStore:
    db = await connect(tmp_path / "s.db")
    _OPEN.append(db)
    return SessionStore(db, asyncio.Lock())


async def test_turn_persists_and_creates_one_session(tmp_path: Path) -> None:
    store = await _store(tmp_path)
    cm = ConnectionManager(clock=lambda: 0.0)
    session = UiSession(
        loop=_loop(tmp_path, FakeClient([text_message("hi there")])),
        connections=cm,
        sessions=store,
    )
    await session.handle_text("hello")
    assert session.session_id is not None
    rows = await store.list_sessions()
    assert len(rows) == 1 and rows[0].id == session.session_id
    saved = await store.load_messages(session.session_id)
    assert saved[0]["content"] == "hello"  # user turn persisted verbatim

    # A second turn reuses the SAME session row (no proliferation).
    session.loop.client = FakeClient([text_message("again")])
    await session.handle_text("more")
    assert len(await store.list_sessions()) == 1


async def test_ui_session_is_reflectable(tmp_path: Path) -> None:
    # A persisted UI turn is an interactive session with reflected_at NULL — so the
    # existing reflection machinery (interactive-only) picks it up, unlike the pre-Phase-10
    # in-memory-only UI conversation that never reflected.
    store = await _store(tmp_path)
    session = UiSession(
        loop=_loop(tmp_path, FakeClient([text_message("noted")])),
        connections=ConnectionManager(clock=lambda: 0.0),
        sessions=store,
    )
    await session.handle_text("remember I like tea")
    assert await store.needs_reflection(session.session_id, kinds=INTERACTIVE_ONLY) is True


async def test_project_scope_at_creation(tmp_path: Path) -> None:
    store = await _store(tmp_path)
    pid = await ProjectStore(store.db, store.lock).create(name="Scoped")  # FK-resolvable id
    session = UiSession(
        loop=_loop(tmp_path, FakeClient([text_message("ok")])),
        connections=ConnectionManager(clock=lambda: 0.0),
        sessions=store,
        project_id=pid,
    )
    await session.handle_text("scoped turn")
    assert (await store.get_meta(session.session_id)).project_id == pid


async def test_resume_restores_messages(tmp_path: Path) -> None:
    store = await _store(tmp_path)
    # Seed a prior chat directly in the store.
    old = await store.create_session()
    await store.save_messages(
        old,
        [
            {"role": "user", "content": "earlier question"},
            {"role": "assistant", "content": "earlier answer"},
        ],
    )
    session = UiSession(
        loop=_loop(tmp_path, FakeClient([])),
        connections=ConnectionManager(clock=lambda: 0.0),
        sessions=store,
    )
    assert await session.resume(old) is True
    assert session.session_id == old
    assert [m["content"] for m in session.messages] == ["earlier question", "earlier answer"]

    # A follow-up turn appends to the resumed session, not a new one.
    session.loop.client = FakeClient([text_message("continuing")])
    await session.handle_text("follow up")
    assert session.session_id == old
    assert len(await store.load_messages(old)) == 4


async def test_resume_unknown_session_is_false(tmp_path: Path) -> None:
    store = await _store(tmp_path)
    session = UiSession(
        loop=_loop(tmp_path, FakeClient([])),
        connections=ConnectionManager(clock=lambda: 0.0),
        sessions=store,
    )
    assert await session.resume(999) is False
    assert session.session_id is None  # unchanged


async def test_no_store_is_ephemeral_and_safe(tmp_path: Path) -> None:
    # Without a store (bare composition / older tests), the session still runs — it just
    # doesn't persist. No crash, no session_id.
    session = UiSession(
        loop=_loop(tmp_path, FakeClient([text_message("ok")])),
        connections=ConnectionManager(clock=lambda: 0.0),
    )
    result = await session.handle_text("hi")
    assert result.text == "ok"
    assert session.session_id is None
    assert await session.resume(1) is False
