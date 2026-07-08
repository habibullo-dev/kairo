"""ConnectorWriteJournal (Phase 12 Task 1): the metadata-only outward-write outbox.

Pins that the journal records handles + status only, never content, and that its columns cannot
hold a body/secret — the audit/undo surface must be safe to display anywhere.
"""

from __future__ import annotations

from pathlib import Path

import aiosqlite

from jarvis.actions.journal import ConnectorWriteJournal
from jarvis.persistence.db import connect

_NOW = "2026-01-01T00:00:00+00:00"


async def _journal(tmp_path: Path) -> tuple[ConnectorWriteJournal, aiosqlite.Connection]:
    db = await connect(tmp_path / "journal.db")
    return ConnectorWriteJournal(db), db


async def test_record_and_get_roundtrip(tmp_path: Path) -> None:
    journal, db = await _journal(tmp_path)
    try:
        wid = await journal.record(
            provider="google",
            verb="calendar_create",
            status="executed",
            scope="https://www.googleapis.com/auth/calendar.events",
            remote_id="evt-1",
            rollback_kind="delete",
            rollback_ref="evt-1",
            egress_ref="calendar_write",
        )
        row = await journal.get(wid)
        assert row is not None
        assert row.verb == "calendar_create" and row.status == "executed"
        assert row.remote_id == "evt-1" and row.rollback_kind == "delete"
    finally:
        await db.close()


async def test_list_filters_and_newest_first(tmp_path: Path) -> None:
    journal, db = await _journal(tmp_path)
    try:
        # intent_id is a real FK, so a journal row must reference an existing intent.
        cur = await db.execute(
            "INSERT INTO write_intents (idempotency_key, provider, kind, state, source, summary, "
            "request_json, created_at, updated_at) "
            "VALUES ('k', 'google', 'calendar_create', 'executed', 'agent', 's', '{}', ?, ?)",
            (_NOW, _NOW),
        )
        await db.commit()
        intent_id = cur.lastrowid
        w1 = await journal.record(provider="google", verb="doc_create", status="executed")
        w2 = await journal.record(
            provider="google", verb="calendar_create", status="executed", intent_id=intent_id
        )
        rows = await journal.list()
        assert [r.id for r in rows] == [w2, w1]  # newest first
        assert [r.id for r in await journal.list(intent_id=intent_id)] == [w2]
    finally:
        await db.close()


async def test_mark_status_flips_to_undone(tmp_path: Path) -> None:
    journal, db = await _journal(tmp_path)
    try:
        wid = await journal.record(
            provider="google", verb="calendar_create", status="executed", remote_id="evt-1"
        )
        assert await journal.mark_status(wid, "undone") is True
        assert (await journal.get(wid)).status == "undone"
        assert await journal.mark_status(999, "undone") is False
    finally:
        await db.close()


async def test_status_check_constraint(tmp_path: Path) -> None:
    journal, db = await _journal(tmp_path)
    try:
        # The status vocabulary is closed by a table CHECK — a bogus status is rejected.
        try:
            await journal.record(provider="google", verb="calendar_create", status="sent")
            raised = False
        except aiosqlite.IntegrityError:
            raised = True
        assert raised, "connector_writes.status must be constrained to executed|failed|undone"
    finally:
        await db.close()


async def test_journal_table_is_metadata_only(tmp_path: Path) -> None:
    # The invariant as structure: connector_writes has NO column that could hold written
    # content or a secret. It records that a write happened + how to undo it, nothing more.
    _journal_, db = await _journal(tmp_path)
    try:
        cur = await db.execute("PRAGMA table_info(connector_writes)")
        cols = {r[1] for r in await cur.fetchall()}
        assert not (
            cols
            & {
                "body",
                "content",
                "title",
                "summary",
                "attendees",
                "recipient",
                "to",
                "secret",
                "request_json",
                "preview_json",
                "raw",
            }
        )
        # The handles it DOES carry are present.
        assert {"remote_id", "rollback_kind", "rollback_ref", "scope", "status"} <= cols
    finally:
        await db.close()
