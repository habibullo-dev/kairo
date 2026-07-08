"""Calendar write adapter (Phase 12 Task 4) — MockTransport, no network.

Pins the built Google request per verb: create/update/cancel bodies + params, Meet via
conferenceData.createRequest (+ conferenceDataVersion=1 + the idempotent requestId), all-day vs
timed shapes, partial PATCH on update, DELETE + 204 handling on cancel, and get_event.
"""

from __future__ import annotations

import datetime as _dt
import json
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest

from jarvis.connectors.base import ConnectorError
from jarvis.connectors.google import calendar
from jarvis.connectors.google.client import GoogleClient
from jarvis.connectors.oauth import OAuthProvider
from jarvis.connectors.tokens import TokenState, TokenStore

FIXED = _dt.datetime(2026, 1, 1, 12, 0, 0, tzinfo=_dt.UTC)
_P = OAuthProvider(
    name="google", auth_url="", token_url="https://oauth2.googleapis.com/token", scopes=("s",)
)


def _fresh() -> TokenState:
    return TokenState(
        provider="google",
        access_token="tok",
        refresh_token="rt",
        expires_at=(FIXED + _dt.timedelta(hours=1)).isoformat(),
        obtained_at=FIXED.isoformat(),
        scopes=["s"],
    )


@asynccontextmanager
async def _client(handler, tmp_path: Path):
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    store = TokenStore(
        tmp_path / "g.json", provider=_P, client_id="c", client_secret="s", http=http,
        now=lambda: FIXED,
    )
    store.save(_fresh())
    try:
        yield GoogleClient(store, http=http)
    finally:
        await http.aclose()


def _capture(box: dict):
    def handler(req: httpx.Request) -> httpx.Response:
        box["method"] = req.method
        box["path"] = req.url.path
        box["params"] = dict(req.url.params)
        box["body"] = json.loads(req.content) if req.content else None
        if req.method == "DELETE":
            return httpx.Response(204)
        return httpx.Response(200, json={"id": "evt-new", "htmlLink": "https://cal/evt-new"})

    return handler


async def test_create_event_builds_timed_body(tmp_path: Path) -> None:
    box: dict = {}
    async with _client(_capture(box), tmp_path) as gc:
        out = await calendar.create_event(
            gc,
            summary="Standup",
            start="2026-02-01T10:00:00",
            end="2026-02-01T10:15:00",
            timezone="America/New_York",
            attendees=["a@example.com", "b@example.com"],
            location="Room 4",
            recurrence=["RRULE:FREQ=WEEKLY"],
            send_updates="all",
        )
    assert box["method"] == "POST"
    assert box["path"].endswith("/calendars/primary/events")
    assert box["params"]["sendUpdates"] == "all"
    assert box["body"]["start"] == {
        "dateTime": "2026-02-01T10:00:00",
        "timeZone": "America/New_York",
    }
    assert box["body"]["attendees"] == [{"email": "a@example.com"}, {"email": "b@example.com"}]
    assert box["body"]["recurrence"] == ["RRULE:FREQ=WEEKLY"]
    assert "conferenceData" not in box["body"]  # no Meet unless asked
    assert out["id"] == "evt-new"


async def test_create_event_with_meet(tmp_path: Path) -> None:
    box: dict = {}
    async with _client(_capture(box), tmp_path) as gc:
        await calendar.create_event(
            gc,
            summary="Sync",
            start="2026-02-01T10:00:00",
            end="2026-02-01T10:30:00",
            timezone="UTC",
            add_meet=True,
            meet_request_id="intent-key-123",
        )
    assert box["params"]["conferenceDataVersion"] == "1"
    cr = box["body"]["conferenceData"]["createRequest"]
    assert cr["requestId"] == "intent-key-123"  # idempotent: tied to the intent key
    assert cr["conferenceSolutionKey"] == {"type": "hangoutsMeet"}


async def test_create_event_meet_requires_request_id(tmp_path: Path) -> None:
    async with _client(_capture({}), tmp_path) as gc:
        with pytest.raises(ValueError, match="meet_request_id"):
            await calendar.create_event(
                gc, summary="x", start="2026-02-01T10:00:00", end="2026-02-01T10:30:00",
                timezone="UTC", add_meet=True,
            )


async def test_create_all_day_uses_date(tmp_path: Path) -> None:
    box: dict = {}
    async with _client(_capture(box), tmp_path) as gc:
        await calendar.create_event(
            gc, summary="Holiday", start="2026-02-01", end="2026-02-02",
            timezone="UTC", all_day=True,
        )
    assert box["body"]["start"] == {"date": "2026-02-01"}
    assert box["body"]["end"] == {"date": "2026-02-02"}


async def test_update_event_is_partial_patch(tmp_path: Path) -> None:
    box: dict = {}
    async with _client(_capture(box), tmp_path) as gc:
        await calendar.update_event(
            gc, "evt-1", timezone="America/New_York",
            start="2026-02-01T11:00:00", end="2026-02-01T11:30:00", location="Room 7",
            send_updates="all",
        )
    assert box["method"] == "PATCH"
    assert box["path"].endswith("/events/evt-1")
    assert box["params"]["sendUpdates"] == "all"
    # ONLY the changed fields are present — no summary/attendees/recurrence keys.
    assert set(box["body"]) == {"start", "end", "location"}
    assert box["body"]["start"]["dateTime"] == "2026-02-01T11:00:00"


async def test_cancel_event_deletes_and_handles_204(tmp_path: Path) -> None:
    box: dict = {}
    async with _client(_capture(box), tmp_path) as gc:
        result = await calendar.cancel_event(gc, "evt-9")
    assert result is None
    assert box["method"] == "DELETE"
    assert box["path"].endswith("/events/evt-9")
    assert box["params"]["sendUpdates"] == "all"  # cancels notify guests by default


async def test_get_event_fetches_single(tmp_path: Path) -> None:
    box: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        box["method"] = req.method
        box["path"] = req.url.path
        return httpx.Response(200, json={"id": "evt-1", "summary": "Standup"})

    async with _client(handler, tmp_path) as gc:
        event = await calendar.get_event(gc, "evt-1")
    assert box["method"] == "GET" and box["path"].endswith("/events/evt-1")
    assert event["summary"] == "Standup"


async def test_write_4xx_does_not_leak_provider_body(tmp_path: Path) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"error": "forbidden", "detail": "SECRET-LEAK"})

    async with _client(handler, tmp_path) as gc:
        with pytest.raises(ConnectorError) as exc:
            await calendar.create_event(
                gc, summary="x", start="2026-02-01T10:00:00", end="2026-02-01T10:30:00",
                timezone="UTC",
            )
    assert "SECRET-LEAK" not in str(exc.value)
