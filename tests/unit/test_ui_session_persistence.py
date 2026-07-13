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
from jarvis.core import (
    AgentLoop,
    FakeClient,
    ToolCall,
    build_system,
    text_message,
    tool_use_message,
)
from jarvis.core.context import ContextManager
from jarvis.permissions import PermissionGate, Policy
from jarvis.persistence import SessionStore
from jarvis.persistence.db import connect
from jarvis.persistence.sessions import INTERACTIVE_ONLY
from jarvis.projects import ProjectStore
from jarvis.tools import Permission, ToolContext, ToolExecutor, ToolRegistry
from jarvis.ui.connections import ConnectionManager
from jarvis.ui.session import UiSession, initial_chat_title

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


def test_initial_chat_title_is_descriptive_and_never_numeric() -> None:
    assert (
        initial_chat_title("Can you make a project release plan?") == "make a project release plan"
    )
    assert initial_chat_title("# Review the architecture for the local agent workstation") == (
        "Review the architecture for the local agent workstation"
    )
    assert initial_chat_title("   ") is None


async def test_first_turn_titles_a_blank_chat_without_overwriting_a_human_title(
    tmp_path: Path,
) -> None:
    store = await _store(tmp_path)
    session = UiSession(
        loop=_loop(tmp_path, FakeClient([text_message("done")])),
        connections=ConnectionManager(clock=lambda: 0.0),
        sessions=store,
    )
    await session.handle_text("Could you plan our release checklist?")
    meta = await store.get_meta(session.session_id)
    assert meta is not None and meta.title == "plan our release checklist"

    assert await store.set_title(session.session_id, "Human chosen title")
    session.loop.client = FakeClient([text_message("done again")])
    await session.handle_text("A later question must not replace the title.")
    assert (await store.get_meta(session.session_id)).title == "Human chosen title"


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


async def test_new_chat_clears_compacted_context_and_archived_chat_cannot_resume(
    tmp_path: Path,
) -> None:
    store = await _store(tmp_path)
    old = await store.create_session()
    await store.save_messages(old, [{"role": "user", "content": "old project summary source"}])
    await store.save_compaction(old, "old compacted summary", 1)
    context = ContextManager()
    session = UiSession(
        loop=_loop(tmp_path, FakeClient([])),
        connections=ConnectionManager(clock=lambda: 0.0),
        sessions=store,
        context_manager=context,
    )
    assert await session.resume(old)
    assert context.state() == ("old compacted summary", 1)
    session.start_new_session(None)
    assert session.messages == []
    assert session.session_id is None
    assert context.state() == (None, 0)  # old summary cannot leak into a fresh chat

    await store.set_archived(old, True)
    assert not await session.resume(old)


async def test_persistence_failure_has_a_safe_visible_state(tmp_path: Path, monkeypatch) -> None:
    store = await _store(tmp_path)
    session = UiSession(
        loop=_loop(tmp_path, FakeClient([text_message("reply")])),
        connections=ConnectionManager(clock=lambda: 0.0),
        sessions=store,
    )

    async def fail_save(*_args, **_kwargs) -> None:
        raise OSError("database path and secret-shaped provider detail")

    monkeypatch.setattr(store, "save_messages", fail_save)
    await session.handle_text("hello")
    assert session.persistence_state == "failed"


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


async def test_cancel_before_response_persists_stopped_turn_and_can_resume(tmp_path: Path) -> None:
    store = await _store(tmp_path)
    entered = asyncio.Event()
    never = asyncio.Event()

    class BlockingClient(FakeClient):
        async def create(self, **kwargs):  # type: ignore[override]
            entered.set()
            await never.wait()
            return await super().create(**kwargs)

    class RecordingConnections(ConnectionManager):
        def __init__(self) -> None:
            super().__init__(clock=lambda: 0.0)
            self.published: list[dict] = []

        async def publish(self, _context, message: dict) -> None:
            self.published.append(message)

    connections = RecordingConnections()
    session = UiSession(
        loop=_loop(tmp_path, BlockingClient([text_message("unreached")])),
        connections=connections,
        sessions=store,
    )
    assert session.submit("draft a plan")
    await entered.wait()
    assert session.cancel()
    with pytest.raises(asyncio.CancelledError):
        await session._current

    assert session.session_id is not None
    saved = await store.load_messages(session.session_id)
    assert saved == [
        {"role": "user", "content": "draft a plan"},
        {"role": "assistant", "content": "(stopped)"},
    ]
    # The durable save lifecycle finishes before the websocket cancellation is delivered.
    assert [message["kind"] for message in connections.published] == [
        "session_persistence",
        "session_persistence",
        "turn_cancelled",
    ]
    assert connections.published[1]["state"] == "saved"

    resumed = UiSession(
        loop=_loop(tmp_path, FakeClient([text_message("continuing safely")])),
        connections=ConnectionManager(clock=lambda: 0.0),
        sessions=store,
    )
    assert await resumed.resume(session.session_id)
    result = await resumed.handle_text("continue")
    assert result.text == "continuing safely"


async def test_failed_turn_persists_redacted_marker_before_browser_notification(
    tmp_path: Path,
) -> None:
    store = await _store(tmp_path)

    class FailingClient(FakeClient):
        async def create(self, **_kwargs):  # type: ignore[override]
            raise RuntimeError("SECRET-PROVIDER-PATH-CANARY")

    class RecordingConnections(ConnectionManager):
        def __init__(self) -> None:
            super().__init__(clock=lambda: 0.0)
            self.published: list[dict] = []

        async def publish(self, _context, message: dict) -> None:
            self.published.append(message)

    connections = RecordingConnections()
    session = UiSession(
        loop=_loop(tmp_path, FailingClient([])),
        connections=connections,
        sessions=store,
    )
    await session.ensure_session()
    assert session.submit("retry this safely")
    await session._current

    assert session.session_id is not None
    assert await store.load_messages(session.session_id) == [
        {"role": "user", "content": "retry this safely"},
        {"role": "assistant", "content": "(unable to complete this turn)"},
    ]
    assert [message["kind"] for message in connections.published] == [
        "session_persistence",
        "session_persistence",
        "turn_error",
    ]
    assert connections.published[-1] == {"kind": "turn_error"}
    assert "SECRET-PROVIDER-PATH-CANARY" not in str(connections.published)
    assert session.last_turn_cost_usd is None

    session.loop.client = FakeClient([text_message("recovered")])
    assert (await session.handle_text("continue")).text == "recovered"


async def test_failed_turn_never_persists_a_partial_assistant_protocol_block(
    tmp_path: Path,
) -> None:
    store = await _store(tmp_path)

    class MutatingFailure:
        chat_limits = None

        async def run_turn(self, messages, *, on_event):
            messages.append(
                {
                    "role": "assistant",
                    "content": [{"type": "tool_use", "id": "partial", "name": "echo"}],
                }
            )
            raise RuntimeError("unavailable")

    session = UiSession(
        loop=MutatingFailure(),  # type: ignore[arg-type] - narrow failure seam, not an AgentLoop
        connections=ConnectionManager(clock=lambda: 0.0),
        sessions=store,
    )
    with pytest.raises(RuntimeError, match="unavailable"):
        await session.handle_text("make a plan")

    assert session.session_id is not None
    saved = await store.load_messages(session.session_id)
    assert saved == [
        {"role": "user", "content": "make a plan"},
        {"role": "assistant", "content": "(unable to complete this turn)"},
    ]


async def test_cancel_during_tool_batch_never_persists_unmatched_tool_use(tmp_path: Path) -> None:
    store = await _store(tmp_path)
    entered = asyncio.Event()
    never = asyncio.Event()
    session = UiSession(
        loop=_loop(
            tmp_path,
            FakeClient([tool_use_message([ToolCall("t1", "read_file", {"path": "notes.txt"})])]),
        ),
        connections=ConnectionManager(clock=lambda: 0.0),
        sessions=store,
    )

    async def block_tools(*_args: object) -> list[dict]:
        entered.set()
        await never.wait()
        return []

    session.loop._handle_tools = block_tools  # type: ignore[method-assign]
    assert session.submit("read the notes")
    await entered.wait()
    assert session.cancel()
    with pytest.raises(asyncio.CancelledError):
        await session._current

    assert session.session_id is not None
    saved = await store.load_messages(session.session_id)
    assert [message["role"] for message in saved] == ["user", "assistant"]
    assert saved[-1]["content"] == "(stopped)"
    assert not any(
        isinstance(message["content"], list)
        and any(block.get("type") == "tool_use" for block in message["content"])
        for message in saved
    )
