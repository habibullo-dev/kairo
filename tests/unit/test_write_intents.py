"""WriteIntent store + state machine (Phase 12 Task 1). Keyless, synthetic, no network.

Pins the safety properties: only legal transitions, faithful (immutable) request payload,
idempotent create + idempotent execute (no double-write), and project scoping.
"""

from __future__ import annotations

from pathlib import Path

import aiosqlite
import pytest

from jarvis.actions.intents import (
    ALLOWED_TRANSITIONS,
    IntentKind,
    IntentState,
    IntentStore,
    InvalidTransition,
)
from jarvis.persistence.db import connect

_NOW = "2026-01-01T00:00:00+00:00"

_REQUEST = {
    "summary": "Standup",
    "start": "2026-02-01T10:00:00",
    "end": "2026-02-01T10:15:00",
    "attendees": ["a@example.com"],
}


async def _store(tmp_path: Path) -> tuple[IntentStore, aiosqlite.Connection]:
    db = await connect(tmp_path / "intents.db")
    return IntentStore(db), db


async def _project(db: aiosqlite.Connection, name: str = "P") -> int:
    cur = await db.execute(
        "INSERT INTO projects (name, slug, repos_json, settings_json, created_at, updated_at) "
        "VALUES (?, ?, '[]', '{}', ?, ?)",
        (name, name.lower(), _NOW, _NOW),
    )
    await db.commit()
    assert cur.lastrowid is not None
    return cur.lastrowid


async def _draft(store: IntentStore, **overrides: object) -> int:
    kwargs: dict = {
        "idempotency_key": "k1",
        "provider": "google",
        "kind": IntentKind.CALENDAR_CREATE,
        "request": _REQUEST,
        "summary": "Create event: Standup",
        "source": "agent",
    }
    kwargs.update(overrides)
    return await store.create_draft(**kwargs)


async def test_full_happy_path_draft_preview_approve_execute(tmp_path: Path) -> None:
    store, db = await _store(tmp_path)
    try:
        iid = await _draft(store)
        intent = await store.get(iid)
        assert intent is not None
        assert intent.state is IntentState.DRAFT
        assert intent.request == _REQUEST  # round-trips
        assert intent.preview is None and intent.result is None

        preview = {"when": "Feb 1, 10:00–10:15", "attendees": ["a@example.com"]}
        intent = await store.mark_previewed(iid, preview=preview)
        assert intent.state is IntentState.PREVIEWED
        assert intent.preview == preview and intent.previewed_at is not None

        intent = await store.approve(iid)
        assert intent.state is IntentState.APPROVED and intent.decided_at is not None

        intent = await store.mark_executed(iid, result={"remote_id": "evt-1"})
        assert intent.state is IntentState.EXECUTED
        assert intent.result == {"remote_id": "evt-1"} and intent.executed_at is not None
    finally:
        await db.close()


async def test_cannot_execute_before_approve(tmp_path: Path) -> None:
    store, db = await _store(tmp_path)
    try:
        iid = await _draft(store)
        await store.mark_previewed(iid, preview={})
        # PREVIEWED → EXECUTED is not a legal edge (must go through APPROVED).
        with pytest.raises(InvalidTransition):
            await store.mark_executed(iid, result={"remote_id": "x"})
    finally:
        await db.close()


async def test_cannot_approve_a_draft_without_preview(tmp_path: Path) -> None:
    store, db = await _store(tmp_path)
    try:
        iid = await _draft(store)
        # No write without a faithful preview: DRAFT → APPROVED is refused.
        with pytest.raises(InvalidTransition):
            await store.approve(iid)
    finally:
        await db.close()


async def test_rejected_is_terminal(tmp_path: Path) -> None:
    store, db = await _store(tmp_path)
    try:
        iid = await _draft(store)
        await store.mark_previewed(iid, preview={})
        rejected = await store.reject(iid)
        assert rejected.state is IntentState.REJECTED
        for move in (store.approve(iid), store.mark_previewed(iid, preview={})):
            with pytest.raises(InvalidTransition):
                await move
    finally:
        await db.close()


async def test_create_draft_is_idempotent_by_key(tmp_path: Path) -> None:
    store, db = await _store(tmp_path)
    try:
        first = await _draft(store, idempotency_key="dup")
        # A retry with the same key returns the SAME row — no second intent, no double-write.
        second = await _draft(store, idempotency_key="dup", summary="different label")
        assert first == second
        rows = await store.list()
        assert len(rows) == 1
    finally:
        await db.close()


async def test_retry_after_key_advances_returns_same_intent(tmp_path: Path) -> None:
    store, db = await _store(tmp_path)
    try:
        iid = await _draft(store, idempotency_key="dup")
        await store.mark_previewed(iid, preview={})
        await store.approve(iid)
        # Even once the intent has moved on, a same-key retry returns the existing id (dedup),
        # it does not resurrect a fresh draft.
        again = await _draft(store, idempotency_key="dup")
        assert again == iid
        assert (await store.get(iid)).state is IntentState.APPROVED
    finally:
        await db.close()


async def test_execute_is_idempotent_no_double_write(tmp_path: Path) -> None:
    store, db = await _store(tmp_path)
    try:
        iid = await _draft(store)
        await store.mark_previewed(iid, preview={})
        await store.approve(iid)
        once = await store.mark_executed(iid, result={"remote_id": "evt-1"})
        # A replayed execute returns the recorded result unchanged — it does not re-fire, and
        # (crucially) does not raise on the already-terminal state.
        twice = await store.mark_executed(iid, result={"remote_id": "SHOULD-NOT-REPLACE"})
        assert twice == once
        assert (await store.get(iid)).result == {"remote_id": "evt-1"}
    finally:
        await db.close()


async def test_concurrent_execute_fires_exactly_once(tmp_path: Path) -> None:
    import asyncio

    store, db = await _store(tmp_path)
    try:
        iid = await _draft(store)
        await store.mark_previewed(iid, preview={})
        await store.approve(iid)
        # Two racing executes: exactly one transition to EXECUTED happens (atomic under the lock),
        # the other is an idempotent no-op returning the same recorded result — neither raises,
        # and the DB ends with a single executed result.
        a, b = await asyncio.gather(
            store.mark_executed(iid, result={"remote_id": "A"}),
            store.mark_executed(iid, result={"remote_id": "B"}),
        )
        assert a.state is IntentState.EXECUTED and b.state is IntentState.EXECUTED
        assert a.result == b.result  # both observers see the single winning result
        assert (await store.get(iid)).result in ({"remote_id": "A"}, {"remote_id": "B"})
    finally:
        await db.close()


async def test_failed_execution_is_terminal_and_error_is_recorded(tmp_path: Path) -> None:
    store, db = await _store(tmp_path)
    try:
        iid = await _draft(store)
        await store.mark_previewed(iid, preview={})
        await store.approve(iid)
        failed = await store.mark_failed(iid, error="Google API request failed (HTTP 500).")
        assert failed.state is IntentState.FAILED
        assert "HTTP 500" in failed.error
        with pytest.raises(InvalidTransition):
            await store.mark_executed(iid, result={"remote_id": "x"})
    finally:
        await db.close()


async def test_undo_only_after_execute(tmp_path: Path) -> None:
    store, db = await _store(tmp_path)
    try:
        iid = await _draft(store)
        await store.mark_previewed(iid, preview={})
        await store.approve(iid)
        with pytest.raises(InvalidTransition):
            await store.mark_undone(iid)  # not executed yet
        await store.mark_executed(iid, result={"remote_id": "evt-1"})
        undone = await store.mark_undone(iid)
        assert undone.state is IntentState.UNDONE and undone.undone_at is not None
    finally:
        await db.close()


async def test_transition_on_missing_intent_raises_keyerror(tmp_path: Path) -> None:
    store, db = await _store(tmp_path)
    try:
        with pytest.raises(KeyError):
            await store.mark_previewed(9999, preview={})
    finally:
        await db.close()


async def test_list_filters_by_state_and_project(tmp_path: Path) -> None:
    store, db = await _store(tmp_path)
    try:
        pid = await _project(db)
        a = await _draft(store, idempotency_key="a", project_id=pid)
        b = await _draft(store, idempotency_key="b", project_id=pid)
        await _draft(store, idempotency_key="c")  # global (no project)
        await store.mark_previewed(a, preview={})  # a → previewed
        _ = b

        previewed = await store.list(state=IntentState.PREVIEWED)
        assert [i.id for i in previewed] == [a]

        in_project = await store.list(project_id=pid)
        assert {i.id for i in in_project} == {a, b}

        # newest-first ordering
        all_rows = await store.list()
        assert [r.id for r in all_rows] == sorted([r.id for r in all_rows], reverse=True)
    finally:
        await db.close()


async def test_project_fk_is_enforced(tmp_path: Path) -> None:
    store, db = await _store(tmp_path)
    try:
        # A dangling project_id is rejected while FKs are enforced (no silent orphan intent).
        with pytest.raises(aiosqlite.IntegrityError):
            await _draft(store, idempotency_key="orphan", project_id=424242)
    finally:
        await db.close()


async def test_every_state_has_a_transition_entry() -> None:
    # Completeness pin: the transition table covers every IntentState (a new state without an
    # entry would KeyError at runtime instead of failing closed here).
    assert set(ALLOWED_TRANSITIONS) == set(IntentState)
    # Terminal states really are terminal.
    for terminal in (IntentState.FAILED, IntentState.REJECTED, IntentState.UNDONE):
        assert ALLOWED_TRANSITIONS[terminal] == frozenset()
