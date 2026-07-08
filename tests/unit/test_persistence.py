"""Persistence tests: migrations, message round-trip, resume, save-per-turn."""

from __future__ import annotations

import asyncio
import io
from pathlib import Path

import aiosqlite
import pytest
from rich.console import Console

from jarvis.cli.repl import Repl
from jarvis.config import load_config
from jarvis.core import FakeClient, text_message
from jarvis.persistence import SessionStore
from jarvis.persistence.db import connect, transaction
from jarvis.persistence.migrations import (
    _SCHEMA_V1,
    _SCHEMA_V2,
    _SCHEMA_V3,
    _SCHEMA_V4,
    migrate,
)
from jarvis.persistence.sessions import REFLECTABLE_KINDS, reflectable_kinds

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
    assert version == 11


async def test_v2_to_v3_migration_preserves_data(tmp_path: Path) -> None:
    db = await aiosqlite.connect(tmp_path / "m.db")
    try:
        await db.executescript(_SCHEMA_V1)
        await db.executescript(_SCHEMA_V2)
        await db.execute("PRAGMA user_version = 2")
        now = "2026-01-01T00:00:00+00:00"
        await db.execute(
            "INSERT INTO sessions (created_at, updated_at, title) VALUES (?, ?, ?)",
            (now, now, "kept"),
        )
        await db.execute(
            "INSERT INTO messages (session_id, seq, role, content, created_at) "
            "VALUES (1, 0, 'user', ?, ?)",
            ('"hi"', now),
        )
        await db.execute(
            "INSERT INTO memories (type, content, embedding, embedding_model, source, "
            "created_at, updated_at) VALUES ('fact', 'kept-memory', x'00', 'm', 'user', ?, ?)",
            (now, now),
        )
        await db.commit()

        assert await migrate(db) == 11  # migrate() applies ALL pending (v3..v11) onto a v2 db

        cur = await db.execute("SELECT title, kind FROM sessions WHERE id=1")
        assert await cur.fetchone() == ("kept", "interactive")  # backfilled + survives v5 rebuild
        cur = await db.execute("SELECT content FROM messages WHERE session_id=1")
        assert (await cur.fetchone())[0] == '"hi"'
        cur = await db.execute("SELECT content FROM memories WHERE id=1")
        assert (await cur.fetchone())[0] == "kept-memory"
        cur = await db.execute("SELECT count(*) FROM tasks")
        assert (await cur.fetchone())[0] == 0
        cur = await db.execute("SELECT count(*) FROM task_runs")
        assert (await cur.fetchone())[0] == 0
    finally:
        await db.close()


async def test_v3_to_v4_migration_preserves_data(tmp_path: Path) -> None:
    db = await aiosqlite.connect(tmp_path / "m.db")
    try:
        await db.executescript(_SCHEMA_V1)
        await db.executescript(_SCHEMA_V2)
        await db.executescript(_SCHEMA_V3)
        await db.execute("PRAGMA user_version = 3")
        now = "2026-01-01T00:00:00+00:00"
        await db.execute(
            "INSERT INTO sessions (created_at, updated_at, title, kind) VALUES (?, ?, ?, 'task')",
            (now, now, "kept"),
        )
        await db.execute(
            "INSERT INTO messages (session_id, seq, role, content, created_at) "
            "VALUES (1, 0, 'user', ?, ?)",
            ('"hi"', now),
        )
        await db.execute(
            "INSERT INTO memories (type, content, embedding, embedding_model, source, "
            "created_at, updated_at) VALUES ('fact', 'kept-memory', x'00', 'm', 'user', ?, ?)",
            (now, now),
        )
        await db.execute(
            "INSERT INTO tasks (kind, title, payload, schedule_kind, schedule_spec, timezone, "
            "next_run_at, created_by, created_at, updated_at) "
            "VALUES ('reminder', 't', 'p', 'once', ?, 'UTC', ?, 'user', ?, ?)",
            (now, now, now, now),
        )
        await db.commit()

        assert await migrate(db) == 11  # applies v4..v11 onto a populated v3 db

        cur = await db.execute("SELECT title, kind FROM sessions WHERE id=1")
        assert await cur.fetchone() == ("kept", "task")  # task kind survives v5 rebuild
        cur = await db.execute("SELECT content FROM memories WHERE id=1")
        assert (await cur.fetchone())[0] == "kept-memory"
        cur = await db.execute("SELECT title FROM tasks WHERE id=1")
        assert (await cur.fetchone())[0] == "t"
        for table in ("kb_sources", "kb_chunks", "kb_wiki_links"):
            cur = await db.execute(f"SELECT count(*) FROM {table}")
            assert (await cur.fetchone())[0] == 0
    finally:
        await db.close()


async def test_v4_to_v5_migration_preserves_data_and_widens_kind(tmp_path: Path) -> None:
    # The highest-blast-radius migration: sessions is rebuilt (SQLite can't ALTER a
    # CHECK) with FK children present. Everything must survive byte-identically, the
    # widened CHECK must accept 'subagent', and FK enforcement must be back ON after.
    db = await aiosqlite.connect(tmp_path / "m.db")
    try:
        await db.executescript(_SCHEMA_V1)
        await db.executescript(_SCHEMA_V2)
        await db.executescript(_SCHEMA_V3)
        await db.executescript(_SCHEMA_V4)
        await db.execute("PRAGMA user_version = 4")
        now = "2026-01-01T00:00:00+00:00"
        # An interactive and a task session, both with compaction state + reflected_at.
        await db.execute(
            "INSERT INTO sessions (id, created_at, updated_at, title, reflected_at, "
            "compaction_summary, compaction_cut, kind) "
            "VALUES (1, ?, ?, 'me', ?, 'sum', 7, 'interactive')",
            (now, now, now),
        )
        await db.execute(
            "INSERT INTO sessions (id, created_at, updated_at, title, kind) "
            "VALUES (2, ?, ?, 'job #1', 'task')",
            (now, now),
        )
        # FK children of sessions: messages, memories, tasks (exercise FK preservation).
        await db.execute(
            "INSERT INTO messages (session_id, seq, role, content, created_at) "
            "VALUES (1, 0, 'user', ?, ?)",
            ('"hi"', now),
        )
        await db.execute(
            "INSERT INTO memories (type, content, embedding, embedding_model, source, "
            "source_session_id, created_at, updated_at) "
            "VALUES ('fact', 'kept', x'00', 'm', 'user', 1, ?, ?)",
            (now, now),
        )
        await db.execute(
            "INSERT INTO tasks (kind, title, payload, schedule_kind, schedule_spec, timezone, "
            "next_run_at, created_by, source_session_id, created_at, updated_at) "
            "VALUES ('reminder', 't', 'p', 'once', ?, 'UTC', ?, 'user', 1, ?, ?)",
            (now, now, now, now),
        )
        await db.commit()

        assert await migrate(db) == 11

        # All rows survive, including the widened-column values on the rebuilt table.
        cur = await db.execute(
            "SELECT title, reflected_at, compaction_summary, compaction_cut, kind "
            "FROM sessions WHERE id = 1"
        )
        assert await cur.fetchone() == ("me", now, "sum", 7, "interactive")
        cur = await db.execute("SELECT kind FROM sessions WHERE id = 2")
        assert (await cur.fetchone())[0] == "task"
        cur = await db.execute("SELECT source_session_id FROM memories WHERE id = 1")
        assert (await cur.fetchone())[0] == 1  # FK reference preserved across the rebuild
        cur = await db.execute("SELECT source_session_id FROM tasks WHERE id = 1")
        assert (await cur.fetchone())[0] == 1

        # The widened CHECK now accepts 'subagent'...
        await db.execute(
            "INSERT INTO sessions (created_at, updated_at, kind) VALUES (?, ?, 'subagent')",
            (now, now),
        )
        await db.commit()
        # ...but still rejects an unknown kind.
        with pytest.raises(aiosqlite.IntegrityError):
            await db.execute(
                "INSERT INTO sessions (created_at, updated_at, kind) VALUES (?, ?, 'bogus')",
                (now, now),
            )
        await db.rollback()

        # FK enforcement is back ON: a child pointing at a missing parent is rejected.
        with pytest.raises(aiosqlite.IntegrityError):
            await db.execute(
                "INSERT INTO messages (session_id, seq, role, content, created_at) "
                "VALUES (9999, 0, 'user', ?, ?)",
                ('"orphan"', now),
            )
        await db.rollback()

        # agent_runs exists and is empty.
        cur = await db.execute("SELECT count(*) FROM agent_runs")
        assert (await cur.fetchone())[0] == 0
    finally:
        await db.close()


async def test_schema_v5_agent_runs_check_constraints(tmp_path: Path) -> None:
    db = await connect(tmp_path / "c.db")
    try:
        now = "2026-01-01T00:00:00+00:00"
        # agent_runs.status is constrained to the run-status machine.
        with pytest.raises(aiosqlite.IntegrityError):
            await db.execute(
                "INSERT INTO agent_runs (title, prompt, tools_scope, status, started_at, "
                "created_at) VALUES ('t', 'p', '[]', 'bogus', ?, ?)",
                (now, now),
            )
    finally:
        await db.close()


async def test_schema_v3_check_constraints(tmp_path: Path) -> None:
    db = await connect(tmp_path / "c.db")
    try:
        now = "2026-01-01T00:00:00+00:00"
        # terminal task status must not carry a next_run_at (it would look due)
        with pytest.raises(aiosqlite.IntegrityError):
            await db.execute(
                "INSERT INTO tasks (kind, title, payload, schedule_kind, schedule_spec, "
                "timezone, next_run_at, status, created_by, created_at, updated_at) "
                "VALUES ('job', 't', 'p', 'once', ?, 'UTC', ?, 'done', 'user', ?, ?)",
                (now, now, now, now),
            )
        # sessions.kind is constrained
        with pytest.raises(aiosqlite.IntegrityError):
            await db.execute(
                "INSERT INTO sessions (created_at, updated_at, kind) VALUES (?, ?, 'bogus')",
                (now, now),
            )
    finally:
        await db.close()


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


async def test_unreflected_sessions_and_mark(tmp_path: Path) -> None:
    store = await SessionStore.open(tmp_path / "s.db")
    try:
        empty = await store.create_session()  # no messages -> not "unreflected with content"
        with_msgs = await store.create_session()
        await store.save_messages(with_msgs, [{"role": "user", "content": "hi"}])
        current = await store.create_session()
        await store.save_messages(current, [{"role": "user", "content": "now"}])

        # only sessions that have messages and aren't the current one need catch-up
        stale = await store.unreflected_session_ids(exclude=current)
        assert with_msgs in stale
        assert empty not in stale  # no messages
        assert current not in stale  # excluded

        await store.mark_reflected(with_msgs)
        assert with_msgs not in await store.unreflected_session_ids(exclude=current)
    finally:
        await store.close()


async def test_save_messages_clears_reflected_at(tmp_path: Path) -> None:
    store = await SessionStore.open(tmp_path / "s.db")
    try:
        sid = await store.create_session()
        await store.save_messages(sid, [{"role": "user", "content": "hi"}])
        await store.mark_reflected(sid)
        assert await store.needs_reflection(sid) is False  # reflected, unchanged
        # new content arrives -> reflection is now stale
        await store.save_messages(sid, [{"role": "user", "content": "and another thing"}])
        assert await store.needs_reflection(sid) is True
    finally:
        await store.close()


async def test_needs_reflection_requires_content(tmp_path: Path) -> None:
    store = await SessionStore.open(tmp_path / "s.db")
    try:
        sid = await store.create_session()
        assert await store.needs_reflection(sid) is False  # no messages yet
        await store.save_messages(sid, [{"role": "user", "content": "x"}])
        assert await store.needs_reflection(sid) is True
    finally:
        await store.close()


async def test_resumed_session_stays_catchable_after_new_turns(tmp_path: Path) -> None:
    # The bug: reflect a session, resume it, add turns, crash before clean exit.
    # Startup catch-up must still find it (reflected_at was cleared by the new save).
    store = await SessionStore.open(tmp_path / "s.db")
    try:
        sid = await store.create_session()
        await store.save_messages(sid, [{"role": "user", "content": "first session"}])
        await store.mark_reflected(sid)  # clean exit reflected it
        # ...later: resume, add a turn (save), then the process dies (no clean exit)
        await store.save_messages(
            sid,
            [
                {"role": "user", "content": "first session"},
                {"role": "user", "content": "a new thing added after resume"},
            ],
        )
        # a *different* current session so `sid` isn't excluded from catch-up
        current = await store.create_session()
        assert sid in await store.unreflected_session_ids(exclude=current)
    finally:
        await store.close()


async def test_latest_session_ignores_task_sessions(tmp_path: Path) -> None:
    # --resume must never land the user inside a background job's transcript.
    store = await SessionStore.open(tmp_path / "s.db")
    try:
        interactive = await store.create_session(title="me")
        await store.save_messages(interactive, [{"role": "user", "content": "hi"}])
        task = await store.create_session(title="task #1", kind="task")
        await store.save_messages(task, [{"role": "user", "content": "job payload"}])
        # the task session is newer, but latest still returns the interactive one
        assert await store.latest_session_id() == interactive
    finally:
        await store.close()


async def test_unreflected_skips_task_sessions_by_default(tmp_path: Path) -> None:
    # Unattended transcripts must not feed long-term memory unless opted in.
    store = await SessionStore.open(tmp_path / "s.db")
    try:
        task = await store.create_session(kind="task")
        await store.save_messages(task, [{"role": "user", "content": "job payload"}])
        interactive = await store.create_session()
        await store.save_messages(interactive, [{"role": "user", "content": "hi"}])

        assert task not in await store.unreflected_session_ids()
        assert interactive in await store.unreflected_session_ids()
        # explicit opt-in (scheduler.reflect_job_sessions) includes them
        assert task in await store.unreflected_session_ids(kinds=REFLECTABLE_KINDS)

        assert await store.needs_reflection(task) is False
        assert await store.needs_reflection(task, kinds=REFLECTABLE_KINDS) is True
    finally:
        await store.close()


async def test_reflectable_kinds_never_includes_subagent() -> None:
    # The Phase 6 firewall as pure policy: the ONLY helper callers use to derive a
    # reflection kinds set can never yield 'subagent', for either config value.
    assert reflectable_kinds(reflect_job_sessions=False) == frozenset({"interactive"})
    assert reflectable_kinds(reflect_job_sessions=True) == frozenset({"interactive", "task"})
    assert "subagent" not in reflectable_kinds(reflect_job_sessions=True)
    assert "subagent" not in REFLECTABLE_KINDS


async def test_subagent_sessions_never_reflected_even_if_kinds_asks(tmp_path: Path) -> None:
    # The firewall as structure: even a (buggy) caller that explicitly requests
    # 'subagent' gets nothing — the query intersects with REFLECTABLE_KINDS, so a
    # delegated child transcript can never launder into long-term memory.
    store = await SessionStore.open(tmp_path / "s.db")
    try:
        sub = await store.create_session(title="child", kind="subagent")
        await store.save_messages(sub, [{"role": "user", "content": "delegated task"}])

        assert sub not in await store.unreflected_session_ids()
        assert sub not in await store.unreflected_session_ids(
            kinds=frozenset({"interactive", "task", "subagent"})
        )
        assert sub not in await store.unreflected_session_ids(kinds=frozenset({"subagent"}))
        assert await store.needs_reflection(sub, kinds=frozenset({"subagent"})) is False
        # and a subagent session never wins --resume either
        interactive = await store.create_session()
        await store.save_messages(interactive, [{"role": "user", "content": "hi"}])
        assert await store.latest_session_id() == interactive
    finally:
        await store.close()


async def test_concurrent_save_messages_both_survive(tmp_path: Path) -> None:
    # Phase 3's first real write concurrency: two interleaved multi-statement saves
    # must not share a transaction (one commit() flushing the other's half-done work).
    store = await SessionStore.open(tmp_path / "s.db")
    try:
        a = await store.create_session()
        b = await store.create_session()
        msgs_a = [{"role": "user", "content": f"a{i}"} for i in range(50)]
        msgs_b = [{"role": "user", "content": f"b{i}"} for i in range(50)]
        await asyncio.gather(
            store.save_messages(a, msgs_a),
            store.save_messages(b, msgs_b),
            store.save_messages(a, msgs_a),  # re-save concurrently too
        )
        assert await store.load_messages(a) == msgs_a
        assert await store.load_messages(b) == msgs_b
    finally:
        await store.close()


async def test_transaction_rolls_back_on_error(tmp_path: Path) -> None:
    store = await SessionStore.open(tmp_path / "s.db")
    try:
        sid = await store.create_session()
        await store.save_messages(sid, [{"role": "user", "content": "keep me"}])
        with pytest.raises(RuntimeError):
            async with transaction(store.db, store.lock):
                await store.db.execute("DELETE FROM messages WHERE session_id = ?", (sid,))
                raise RuntimeError("boom mid-transaction")
        # the DELETE was rolled back, not committed by a later writer
        assert await store.load_messages(sid) == [{"role": "user", "content": "keep me"}]
    finally:
        await store.close()


async def test_compaction_state_roundtrip(tmp_path: Path) -> None:
    store = await SessionStore.open(tmp_path / "s.db")
    try:
        sid = await store.create_session()
        assert await store.load_compaction(sid) == (None, 0)  # default before any save
        await store.save_compaction(sid, "the running summary", 12)
        assert await store.load_compaction(sid) == ("the running summary", 12)
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
