"""Persistence tests: migrations, message round-trip, resume, save-per-turn."""

from __future__ import annotations

import io
from pathlib import Path

from rich.console import Console

from jarvis.cli.repl import Repl
from jarvis.config import load_config
from jarvis.core import FakeClient, text_message
from jarvis.persistence import SessionStore
from jarvis.persistence.db import connect

MIXED_MESSAGES = [
    {"role": "user", "content": "summarize the file"},
    {
        "role": "assistant",
        "content": [
            {"type": "thinking", "thinking": "", "signature": "sig-abc"},
            {"type": "text", "text": "Reading it now."},
            {"type": "tool_use", "id": "t1", "name": "read_file", "input": {"path": "a.txt"}},
        ],
    },
    {
        "role": "user",
        "content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": "hi", "is_error": False}
        ],
    },
    {"role": "assistant", "content": [{"type": "text", "text": "It says hi."}]},
]


async def test_migrations_set_user_version(tmp_path: Path) -> None:
    db = await connect(tmp_path / "v.db")
    cursor = await db.execute("PRAGMA user_version")
    (version,) = await cursor.fetchone()
    await db.close()
    assert version == 1


async def test_create_and_latest_session(tmp_path: Path) -> None:
    store = await SessionStore.open(tmp_path / "s.db")
    try:
        assert await store.latest_session_id() is None
        sid = await store.create_session(title="first")
        assert await store.latest_session_id() == sid
    finally:
        await store.close()


async def test_message_roundtrip_preserves_structure(tmp_path: Path) -> None:
    store = await SessionStore.open(tmp_path / "s.db")
    try:
        sid = await store.create_session()
        await store.save_messages(sid, MIXED_MESSAGES)
        loaded = await store.load_messages(sid)
        assert loaded == MIXED_MESSAGES
        # the thinking block's signature survives — required for API replay
        assert loaded[1]["content"][0]["signature"] == "sig-abc"
    finally:
        await store.close()


async def test_save_replaces_previous_messages(tmp_path: Path) -> None:
    store = await SessionStore.open(tmp_path / "s.db")
    try:
        sid = await store.create_session()
        await store.save_messages(sid, MIXED_MESSAGES)
        await store.save_messages(sid, [{"role": "user", "content": "only this"}])
        loaded = await store.load_messages(sid)
        assert loaded == [{"role": "user", "content": "only this"}]
    finally:
        await store.close()


async def test_latest_session_tracks_most_recent_update(tmp_path: Path) -> None:
    store = await SessionStore.open(tmp_path / "s.db")
    try:
        first = await store.create_session()
        second = await store.create_session()
        # touching `first` makes it the most recently updated
        await store.save_messages(first, [{"role": "user", "content": "x"}])
        assert await store.latest_session_id() == first
        assert second != first
    finally:
        await store.close()


async def test_persistence_survives_reopen(tmp_path: Path) -> None:
    path = tmp_path / "s.db"
    store = await SessionStore.open(path)
    sid = await store.create_session()
    await store.save_messages(sid, MIXED_MESSAGES)
    await store.close()

    reopened = await SessionStore.open(path)
    try:
        assert await reopened.load_messages(sid) == MIXED_MESSAGES
    finally:
        await reopened.close()


async def test_repl_saves_each_turn(tmp_path: Path) -> None:
    config = load_config(root=tmp_path, env_file=None)
    store = await SessionStore.open(tmp_path / "s.db")
    try:
        sid = await store.create_session()
        console = Console(file=io.StringIO(), force_terminal=False, width=100)
        repl = Repl(
            config,
            client=FakeClient([text_message("done")]),
            console=console,
            store=store,
            session_id=sid,
        )
        repl.messages.append({"role": "user", "content": "hi"})
        await repl.run_turn()

        loaded = await store.load_messages(sid)
        assert loaded == repl.messages
        assert loaded[-1]["role"] == "assistant"
    finally:
        await store.close()
