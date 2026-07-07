"""Google REST adapters (Phase 9 Task 4) — MockTransport fixtures, no network.

Covers the client's 401→refresh→retry-once (and hard-fail on a second 401), typed 4xx that
never leaks the provider body, calendar/gmail/drive parsing + caps, and — load-bearing — that
create_draft posts to users/me/drafts and there is NO send path.
"""

from __future__ import annotations

import base64
import datetime as _dt
import email
import json
from contextlib import asynccontextmanager
from email import policy
from pathlib import Path

import httpx
import pytest

from jarvis.connectors.base import ConnectorAuthError, ConnectorError
from jarvis.connectors.google import calendar, drive, gmail
from jarvis.connectors.google.client import GoogleClient
from jarvis.connectors.oauth import OAuthProvider
from jarvis.connectors.tokens import TokenState, TokenStore

FIXED = _dt.datetime(2026, 1, 1, 12, 0, 0, tzinfo=_dt.UTC)
_P = OAuthProvider(
    name="google", auth_url="", token_url="https://oauth2.googleapis.com/token", scopes=("s",)
)


def _b64url(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode("utf-8")).rstrip(b"=").decode("ascii")


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
        tmp_path / "g.json",
        provider=_P,
        client_id="c",
        client_secret="s",
        http=http,
        now=lambda: FIXED,
    )
    store.save(_fresh())
    try:
        yield GoogleClient(store, http=http)
    finally:
        await http.aclose()


def _is_token(req: httpx.Request) -> bool:
    return "oauth2.googleapis.com/token" in str(req.url)


# --- client auth/retry -----------------------------------------------------


async def test_401_triggers_one_refresh_then_retries(tmp_path: Path) -> None:
    seen = {"api": 0, "token": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        if _is_token(req):
            seen["token"] += 1
            return httpx.Response(200, json={"access_token": "tok2", "expires_in": 3600})
        seen["api"] += 1
        return httpx.Response(401) if seen["api"] == 1 else httpx.Response(200, json={"ok": True})

    async with _client(handler, tmp_path) as gc:
        data = await gc.get_json("https://www.googleapis.com/x")
    assert data == {"ok": True}
    assert seen == {"api": 2, "token": 1}  # one refresh, one retry


async def test_second_401_raises_auth_error_no_loop(tmp_path: Path) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if _is_token(req):
            return httpx.Response(200, json={"access_token": "tok2", "expires_in": 3600})
        return httpx.Response(401)

    async with _client(handler, tmp_path) as gc:
        with pytest.raises(ConnectorAuthError):
            await gc.get_json("https://www.googleapis.com/x")


async def test_4xx_raises_connector_error_without_provider_body(tmp_path: Path) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"error": "forbidden", "detail": "SECRET-LEAK"})

    async with _client(handler, tmp_path) as gc:
        with pytest.raises(ConnectorError) as exc:
            await gc.get_json("https://www.googleapis.com/x")
    assert "SECRET-LEAK" not in str(exc.value)
    assert "403" in exc.value.user_message


# --- calendar --------------------------------------------------------------


async def test_calendar_parses_timed_and_all_day(tmp_path: Path) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "items": [
                    {
                        "id": "e1",
                        "summary": "Standup",
                        "start": {"dateTime": "2026-01-01T10:00:00Z"},
                        "end": {"dateTime": "2026-01-01T10:15:00Z"},
                        "organizer": {"email": "boss@work"},
                        "location": "Zoom",
                    },
                    {
                        "id": "e2",
                        "summary": "Holiday",
                        "start": {"date": "2026-01-02"},
                        "end": {"date": "2026-01-03"},
                    },
                ]
            },
        )

    async with _client(handler, tmp_path) as gc:
        events = await calendar.list_events(gc, time_min="a", time_max="b")
    assert events[0].summary == "Standup" and events[0].all_day is False
    assert events[0].organizer == "boss@work" and events[0].location == "Zoom"
    assert events[1].all_day is True and events[1].start == "2026-01-02"


# --- gmail -----------------------------------------------------------------


async def test_gmail_search_lists_then_fetches_metadata(tmp_path: Path) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if path.endswith("/messages"):
            return httpx.Response(200, json={"messages": [{"id": "m1"}]})
        return httpx.Response(
            200,
            json={
                "id": "m1",
                "threadId": "t1",
                "snippet": "just a snippet",
                "payload": {
                    "headers": [
                        {"name": "From", "value": "alice@example.com"},
                        {"name": "Subject", "value": "Hello"},
                        {"name": "Date", "value": "Mon, 1 Jan 2026"},
                    ]
                },
            },
        )

    async with _client(handler, tmp_path) as gc:
        metas = await gmail.search(gc, query="is:unread")
    assert len(metas) == 1
    assert metas[0].sender == "alice@example.com"
    assert metas[0].subject == "Hello" and metas[0].snippet == "just a snippet"


async def test_gmail_get_message_prefers_plain_and_caps(tmp_path: Path) -> None:
    big = "x" * 30_000

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "m1",
                "threadId": "t1",
                "payload": {
                    "mimeType": "multipart/alternative",
                    "headers": [
                        {"name": "From", "value": "a@b"},
                        {"name": "Subject", "value": "S"},
                    ],
                    "parts": [
                        {"mimeType": "text/plain", "body": {"data": _b64url(big)}},
                        {"mimeType": "text/html", "body": {"data": _b64url("<b>ignored</b>")}},
                    ],
                },
            },
        )

    async with _client(handler, tmp_path) as gc:
        msg = await gmail.get_message(gc, "m1")
    assert msg.subject == "S"
    assert len(msg.body) == 20_000  # capped
    assert set(msg.body) == {"x"}  # the plain part, not the html


async def test_gmail_get_message_falls_back_to_stripped_html(tmp_path: Path) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "m2",
                "threadId": "t",
                "payload": {
                    "mimeType": "text/html",
                    "headers": [],
                    "body": {"data": _b64url("<p>Hi <b>there</b></p>")},
                },
            },
        )

    async with _client(handler, tmp_path) as gc:
        msg = await gmail.get_message(gc, "m2")
    assert "Hi" in msg.body and "there" in msg.body and "<" not in msg.body


async def test_create_draft_posts_to_drafts_and_never_sends(tmp_path: Path) -> None:
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["path"] = req.url.path
        captured["body"] = json.loads(req.content)
        return httpx.Response(200, json={"id": "draft-123"})

    async with _client(handler, tmp_path) as gc:
        draft_id = await gmail.create_draft(
            gc, to="bob@example.com", subject="Re: hi", body="Hello Bob"
        )

    assert draft_id == "draft-123"
    assert captured["path"].endswith("/drafts")  # drafts.create, NOT /drafts/send
    assert "send" not in captured["path"]
    raw = captured["body"]["message"]["raw"]
    decoded = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
    parsed = email.message_from_bytes(decoded, policy=policy.default)
    assert parsed["To"] == "bob@example.com" and parsed["Subject"] == "Re: hi"
    assert "Hello Bob" in parsed.get_content()


# --- drive -----------------------------------------------------------------


async def test_drive_search_parses_files(tmp_path: Path) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "files": [
                    {
                        "id": "f1",
                        "name": "Notes",
                        "mimeType": "application/vnd.google-apps.document",
                        "modifiedTime": "2026-01-01T00:00:00Z",
                        "webViewLink": "https://drive/f1",
                    }
                ]
            },
        )

    async with _client(handler, tmp_path) as gc:
        files = await drive.search(gc, query="name contains 'Notes'")
    assert files[0].name == "Notes" and files[0].web_view_link == "https://drive/f1"


async def test_drive_fetch_text_exports_gdoc(tmp_path: Path) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/export"):
            return httpx.Response(200, text="exported doc text")
        return httpx.Response(
            200,
            json={"id": "f1", "name": "Doc", "mimeType": "application/vnd.google-apps.document"},
        )

    async with _client(handler, tmp_path) as gc:
        text = await drive.fetch_text(gc, "f1")
    assert text == "exported doc text"


async def test_drive_fetch_text_returns_note_for_binary(tmp_path: Path) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"id": "f1", "name": "pic.png", "mimeType": "image/png"})

    async with _client(handler, tmp_path) as gc:
        text = await drive.fetch_text(gc, "f1")
    assert "binary file 'pic.png'" in text and "image/png" in text
