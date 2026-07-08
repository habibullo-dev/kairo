"""WriteExecutor (Phase 12 Task 8): execute an approved intent, journal it, undo it. Keyless.

A fake Google client records calls and returns canned responses — NO network, NO live account.
Pins: execute runs the STORED request (executed == previewed; a tampered preview is inert), the
journal is metadata-only, execute is idempotent (no double-write), a missing client fails closed
without a live write, a connector error marks failed, and undo reverses via the adapter.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from jarvis.actions.executor import WriteExecutor
from jarvis.actions.intents import IntentKind, IntentState, IntentStore
from jarvis.actions.journal import ConnectorWriteJournal
from jarvis.actions.requests import (
    CalendarCreateRequest,
    DocCreateRequest,
    request_to_dict,
)
from jarvis.connectors.base import ConnectorError
from jarvis.persistence.db import connect

_OPEN: list = []


@pytest.fixture(autouse=True)
async def _close():
    yield
    while _OPEN:
        await _OPEN.pop().close()


class _FakeClient:
    """Records every call; returns canned responses. Raises on demand to test the failure path."""

    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[tuple] = []
        self.fail = fail

    async def post_json(self, url: str, *, json_body: dict, params: dict | None = None) -> dict:
        self.calls.append(("POST", url, json_body, params))
        if self.fail:
            raise ConnectorError("google", user_message="Google API request failed (HTTP 500).")
        if url.endswith(":batchUpdate"):
            return {"documentId": "d1", "replies": []}
        if url.endswith("/documents"):
            return {"documentId": "d1", "title": "Spec"}
        return {"id": "evt-1", "htmlLink": "https://cal/evt-1", "hangoutLink": "https://meet/x"}

    async def get_json(self, url: str, *, params: dict | None = None) -> dict:
        self.calls.append(("GET", url, None, params))
        return {"id": "evt-1", "summary": "Old", "start": {"dateTime": "t", "timeZone": "UTC"}}

    async def patch_json(self, url: str, *, json_body: dict, params: dict | None = None) -> dict:
        self.calls.append(("PATCH", url, json_body, params))
        return {"id": "evt-1"}

    async def delete(self, url: str, *, params: dict | None = None) -> None:
        self.calls.append(("DELETE", url, None, params))


async def _setup(tmp_path: Path) -> tuple[IntentStore, ConnectorWriteJournal]:
    db = await connect(tmp_path / "ex.db")
    _OPEN.append(db)
    return IntentStore(db), ConnectorWriteJournal(db)


async def _approved_calendar_create(store: IntentStore, *, summary: str = "Standup") -> int:
    req = request_to_dict(
        CalendarCreateRequest(
            summary=summary, start="2026-02-01T10:00:00", end="2026-02-01T10:15:00",
            timezone="America/New_York", attendees=("alice@example.com",), add_meet=True,
        )
    )
    iid = await store.create_draft(
        idempotency_key="k1", provider="google", kind=IntentKind.CALENDAR_CREATE,
        request=req, summary="Create event: " + summary, source="agent",
    )
    await store.mark_previewed(iid, preview={"title": "Create event: " + summary})
    await store.approve(iid)
    return iid


async def test_execute_runs_the_stored_request_and_journals(tmp_path: Path) -> None:
    store, journal = await _setup(tmp_path)
    client = _FakeClient()
    iid = await _approved_calendar_create(store)
    result = await WriteExecutor(client, store, journal).execute(iid)

    assert result.state is IntentState.EXECUTED
    posts = [c for c in client.calls if c[0] == "POST"]
    assert posts and posts[0][1].endswith("/calendars/primary/events")
    assert posts[0][2]["summary"] == "Standup"  # the request body, from the stored intent
    assert posts[0][2]["conferenceData"]["createRequest"]["requestId"] == "k1"  # Meet = intent key

    rows = await journal.list()
    assert len(rows) == 1
    assert rows[0].status == "executed" and rows[0].verb == "calendar_create"
    assert rows[0].remote_id == "evt-1" and rows[0].rollback_kind == "cancel_event"


async def test_execute_uses_request_not_a_tampered_preview(tmp_path: Path) -> None:
    store, journal = await _setup(tmp_path)
    client = _FakeClient()
    iid = await _approved_calendar_create(store, summary="REAL")
    # Tamper the stored preview to a different summary; execute must ignore it and use the request.
    await store.db.execute(
        "UPDATE write_intents SET preview_json = ? WHERE id = ?",
        ('{"title": "FORGED"}', iid),
    )
    await store.db.commit()
    await WriteExecutor(client, store, journal).execute(iid)
    posts = [c for c in client.calls if c[0] == "POST"]
    assert posts[0][2]["summary"] == "REAL"  # executed == the stored request, NOT the preview


async def test_execute_is_idempotent(tmp_path: Path) -> None:
    store, journal = await _setup(tmp_path)
    client = _FakeClient()
    iid = await _approved_calendar_create(store)
    execu = WriteExecutor(client, store, journal)
    await execu.execute(iid)
    await execu.execute(iid)  # replay
    assert len([c for c in client.calls if c[0] == "POST"]) == 1  # fired exactly once
    assert len(await journal.list()) == 1


async def test_execute_without_client_fails_closed_no_write(tmp_path: Path) -> None:
    store, journal = await _setup(tmp_path)
    iid = await _approved_calendar_create(store)
    result = await WriteExecutor(None, store, journal).execute(iid)  # not connected
    assert result.state is IntentState.FAILED
    assert "connect" in (result.error or "").lower()
    assert await journal.list() == []  # nothing left the box, nothing journalled as executed


async def test_execute_connector_error_marks_failed(tmp_path: Path) -> None:
    store, journal = await _setup(tmp_path)
    client = _FakeClient(fail=True)
    iid = await _approved_calendar_create(store)
    result = await WriteExecutor(client, store, journal).execute(iid)
    assert result.state is IntentState.FAILED and "HTTP 500" in result.error
    rows = await journal.list()
    assert len(rows) == 1 and rows[0].status == "failed"


async def test_execute_requires_approved_state(tmp_path: Path) -> None:
    store, journal = await _setup(tmp_path)
    req = request_to_dict(DocCreateRequest(title="Spec", body="hi"))
    iid = await store.create_draft(
        idempotency_key="d", provider="google", kind=IntentKind.DOC_CREATE,
        request=req, summary="Create doc: Spec", source="agent",
    )
    await store.mark_previewed(iid, preview={})  # PREVIEWED, not APPROVED
    with pytest.raises(ValueError, match="not approved"):
        await WriteExecutor(_FakeClient(), store, journal).execute(iid)


async def test_undo_calendar_create_cancels_the_event(tmp_path: Path) -> None:
    store, journal = await _setup(tmp_path)
    client = _FakeClient()
    execu = WriteExecutor(client, store, journal)
    iid = await _approved_calendar_create(store)
    await execu.execute(iid)
    undone = await execu.undo(iid)
    assert undone.state is IntentState.UNDONE
    assert any(c[0] == "DELETE" and c[1].endswith("/events/evt-1") for c in client.calls)
    assert [r.status for r in await journal.list()][0] == "undone"  # newest-first
