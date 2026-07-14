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


async def test_failed_resume_compaction_load_preserves_live_session(
    tmp_path: Path, monkeypatch
) -> None:
    store = await _store(tmp_path)
    current = await store.create_session()
    target = await store.create_session()
    await store.save_messages(current, [{"role": "user", "content": "current"}])
    await store.save_messages(target, [{"role": "user", "content": "target"}])
    await store.save_compaction(current, "current summary", 1)
    context = ContextManager()
    session = UiSession(
        loop=_loop(tmp_path, FakeClient([])),
        connections=ConnectionManager(clock=lambda: 0.0),
        sessions=store,
        context_manager=context,
    )
    assert await session.resume(current)
    before = (session.session_id, list(session.messages), session.project_id, context.state())
    original = store.load_compaction

    async def fail_target(session_id: int):
        if session_id == target:
            raise OSError("compaction unavailable")
        return await original(session_id)

    monkeypatch.setattr(store, "load_compaction", fail_target)
    with pytest.raises(OSError, match="compaction unavailable"):
        await session.resume(target)

    assert (session.session_id, session.messages, session.project_id, context.state()) == before


async def test_context_replacement_hook_failure_is_atomic(tmp_path: Path) -> None:
    store = await _store(tmp_path)
    current = await store.create_session()
    target = await store.create_session()
    await store.save_messages(current, [{"role": "user", "content": "current"}])
    await store.save_messages(target, [{"role": "user", "content": "target"}])
    await store.save_compaction(current, "current summary", 1)
    await store.save_compaction(target, "target summary", 1)
    context = ContextManager()
    session = UiSession(
        loop=_loop(tmp_path, FakeClient([])),
        connections=ConnectionManager(clock=lambda: 0.0),
        sessions=store,
        context_manager=context,
    )
    assert await session.resume(current)
    before = (session.session_id, list(session.messages), session.project_id, context.state())

    def fail_hook() -> None:
        raise RuntimeError("invalidation failed")

    with pytest.raises(RuntimeError, match="invalidation failed"):
        await session.resume(target, before_commit=fail_hook)
    assert (session.session_id, session.messages, session.project_id, context.state()) == before

    with pytest.raises(RuntimeError, match="invalidation failed"):
        session.start_new_session(None, session_id=999, before_commit=fail_hook)
    assert (session.session_id, session.messages, session.project_id, context.state()) == before


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


async def test_cancel_immediately_after_submit_still_enters_and_settles(tmp_path: Path) -> None:
    store = await _store(tmp_path)

    class NeverCalledClient(FakeClient):
        async def create(self, **_kwargs):  # type: ignore[override]
            raise AssertionError("pre-start cancellation must not call the model")

    class RecordingConnections(ConnectionManager):
        def __init__(self) -> None:
            super().__init__(clock=lambda: 0.0)
            self.published: list[dict] = []

        async def publish(self, _context, message: dict) -> None:
            self.published.append(message)

    connections = RecordingConnections()
    session = UiSession(
        loop=_loop(tmp_path, NeverCalledClient([])),
        connections=connections,
        sessions=store,
    )
    assert session.submit("cancel with no scheduling yield")
    target = session.current_task
    assert target is not None and session.cancel()  # deliberately no sleep/event-loop yield

    with pytest.raises(asyncio.CancelledError):
        await target
    assert await store.load_messages(session.session_id) == [
        {"role": "user", "content": "cancel with no scheduling yield"},
        {"role": "assistant", "content": "(stopped)"},
    ]
    assert [message.get("state", message["kind"]) for message in connections.published] == [
        "saving",
        "saved",
        "turn_cancelled",
    ]


async def test_cancel_queued_before_turn_lock_persists_before_terminal(tmp_path: Path) -> None:
    store = await _store(tmp_path)

    class ObservedLock(asyncio.Lock):
        def __init__(self) -> None:
            super().__init__()
            self.acquire_started = asyncio.Event()

        async def acquire(self) -> bool:
            self.acquire_started.set()
            return await super().acquire()

    class NeverCalledClient(FakeClient):
        async def create(self, **_kwargs):  # type: ignore[override]
            raise AssertionError("a turn cancelled in the queue must never call the model")

    class RecordingConnections(ConnectionManager):
        def __init__(self) -> None:
            super().__init__(clock=lambda: 0.0)
            self.published: list[dict] = []

        async def publish(self, _context, message: dict) -> None:
            self.published.append(message)

    turn_lock = ObservedLock()
    connections = RecordingConnections()
    session = UiSession(
        loop=_loop(tmp_path, NeverCalledClient([])),
        connections=connections,
        turn_lock=turn_lock,
        sessions=store,
    )
    await session.ensure_session()
    turn_lock.acquire_started.clear()
    await turn_lock.acquire()
    turn_lock.acquire_started.clear()

    assert session.submit("queued user request")
    await turn_lock.acquire_started.wait()
    target = session.current_task
    assert target is not None and session.cancel()
    turn_lock.acquire_started.clear()
    await turn_lock.acquire_started.wait()  # cancellation finalizer is queued for the same lock
    assert not target.done()

    turn_lock.release()
    with pytest.raises(asyncio.CancelledError):
        await target

    assert await store.load_messages(session.session_id) == [
        {"role": "user", "content": "queued user request"},
        {"role": "assistant", "content": "(stopped)"},
    ]
    assert [message["kind"] for message in connections.published] == [
        "session_persistence",
        "session_persistence",
        "turn_cancelled",
    ]
    assert [message.get("state") for message in connections.published[:2]] == ["saving", "saved"]


async def test_prestart_cancel_in_new_chat_never_reuses_same_prompt_snapshot(
    tmp_path: Path,
) -> None:
    store = await _store(tmp_path)

    class NeverCalledClient(FakeClient):
        async def create(self, **_kwargs):  # type: ignore[override]
            raise AssertionError("the pre-start turn must not call the model")

    prompt = "repeat this exact request"
    loop = _loop(tmp_path, NeverCalledClient([]))
    # Model the prior AgentLoop state at the exact admission boundary. The old snapshot starts
    # with the identical prompt, so content-prefix heuristics would leak its partial assistant
    # content into this fresh chat.
    loop._record_cancellation(  # noqa: SLF001 - regression pins the private handoff state
        [
            {"role": "user", "content": prompt},
            {"role": "assistant", "content": [{"type": "text", "text": "old partial"}]},
        ]
    )
    new_session_id = await store.create_session(project_id=None)
    session = UiSession(
        loop=loop,
        connections=ConnectionManager(clock=lambda: 0.0),
        sessions=store,
    )
    session.start_new_session(None, session_id=new_session_id)
    assert session.submit(prompt)
    second = session.current_task
    assert second is not None and session.cancel()
    with pytest.raises(asyncio.CancelledError):
        await second

    assert await store.load_messages(new_session_id) == [
        {"role": "user", "content": prompt},
        {"role": "assistant", "content": "(stopped)"},
    ]


async def test_cancel_during_session_allocation_finishes_admission_and_persists(
    tmp_path: Path, monkeypatch
) -> None:
    store = await _store(tmp_path)
    allocation_started = asyncio.Event()
    never = asyncio.Event()
    calls = 0
    original_create = store.create_session

    async def first_allocation_blocks(*, project_id=None, **kwargs) -> int:
        nonlocal calls
        calls += 1
        if calls == 1:
            allocation_started.set()
            await never.wait()
        return await original_create(project_id=project_id, **kwargs)

    monkeypatch.setattr(store, "create_session", first_allocation_blocks)
    session = UiSession(
        loop=_loop(tmp_path, FakeClient([text_message("unreached")])),
        connections=ConnectionManager(clock=lambda: 0.0),
        sessions=store,
    )
    assert session.submit("retain admission")
    await allocation_started.wait()
    target = session.current_task
    assert target is not None and session.cancel()

    with pytest.raises(asyncio.CancelledError):
        await target
    assert calls == 2
    assert await store.load_messages(session.session_id) == [
        {"role": "user", "content": "retain admission"},
        {"role": "assistant", "content": "(stopped)"},
    ]


async def test_repeated_cancel_cannot_interrupt_save_or_terminal(
    tmp_path: Path, monkeypatch
) -> None:
    store = await _store(tmp_path)
    model_entered = asyncio.Event()
    never = asyncio.Event()
    save_started = asyncio.Event()
    save_release = asyncio.Event()
    terminal_started = asyncio.Event()
    terminal_release = asyncio.Event()

    class BlockingClient(FakeClient):
        async def create(self, **kwargs):  # type: ignore[override]
            model_entered.set()
            await never.wait()
            return await super().create(**kwargs)

    class RecordingConnections(ConnectionManager):
        def __init__(self) -> None:
            super().__init__(clock=lambda: 0.0)
            self.published: list[dict] = []

        async def publish(self, _context, message: dict) -> None:
            self.published.append(message)
            if message.get("kind") == "turn_cancelled":
                terminal_started.set()
                await terminal_release.wait()

    original_save = store.save_messages

    async def gated_save(session_id: int, messages: list[dict]) -> None:
        save_started.set()
        await save_release.wait()
        await original_save(session_id, messages)

    monkeypatch.setattr(store, "save_messages", gated_save)
    connections = RecordingConnections()
    session = UiSession(
        loop=_loop(tmp_path, BlockingClient([text_message("unreached")])),
        connections=connections,
        sessions=store,
    )
    await session.ensure_session()
    assert session.submit("keep this stopped request")
    await model_entered.wait()
    target = session.current_task
    assert target is not None and session.cancel()
    await save_started.wait()

    assert not session.cancel()  # terminal settlement is live but no longer cancellable
    assert target.cancelling() == 1
    save_release.set()
    await terminal_started.wait()
    assert session.persistence_state == "saved"
    assert not session.cancel()
    assert target.cancelling() == 1 and not target.done()
    terminal_release.set()

    with pytest.raises(asyncio.CancelledError):
        await target
    assert await store.load_messages(session.session_id) == [
        {"role": "user", "content": "keep this stopped request"},
        {"role": "assistant", "content": "(stopped)"},
    ]
    assert [message.get("state", message["kind"]) for message in connections.published] == [
        "saving",
        "saved",
        "turn_cancelled",
    ]


async def test_cancel_during_normal_save_acknowledges_and_drains_success(
    tmp_path: Path, monkeypatch
) -> None:
    store = await _store(tmp_path)
    save_started = asyncio.Event()
    save_release = asyncio.Event()

    class RecordingConnections(ConnectionManager):
        def __init__(self) -> None:
            super().__init__(clock=lambda: 0.0)
            self.published: list[dict] = []

        async def publish(self, _context, message: dict) -> None:
            self.published.append(message)

    original_save = store.save_messages

    async def gated_save(session_id: int, messages: list[dict]) -> None:
        save_started.set()
        await save_release.wait()
        await original_save(session_id, messages)

    monkeypatch.setattr(store, "save_messages", gated_save)
    connections = RecordingConnections()
    session = UiSession(
        loop=_loop(tmp_path, FakeClient([text_message("completed reply")])),
        connections=connections,
        sessions=store,
    )
    assert session.submit("finish this request")
    await save_started.wait()
    target = session.current_task
    assert target is not None and not session.cancel()
    assert target.cancelling() == 0
    assert not target.done()

    save_release.set()
    await target
    assert session.persistence_state == "saved"
    assert await store.load_messages(session.session_id) == session.messages
    assert session.messages[-1]["content"] != "(stopped)"
    assert [
        message["state"]
        for message in connections.published
        if message.get("kind") == "session_persistence"
    ] == ["saving", "saved"]
    assert not any(message.get("kind") == "turn_cancelled" for message in connections.published)


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
