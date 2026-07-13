"""Telegram workspace summaries disclose only their documented bounded fields."""

from __future__ import annotations

import datetime as dt

from jarvis.connectors.base import ConnectorRegistry
from jarvis.remote.workspace import calendar_status, inbox_status, inbox_today_summary


class _Google:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict | None]] = []

    async def get_json(self, url: str, *, params: dict | None = None) -> dict:
        self.calls.append((url, params))
        if url.endswith("/messages"):
            return {"resultSizeEstimate": 3, "messages": [{"id": "private-id"}]}
        return {
            "items": [
                {
                    "id": "private-event-id",
                    "summary": "Sensitive board meeting",
                    "location": "Private office",
                    "organizer": {"email": "owner@example.com"},
                    "start": {"dateTime": "2026-07-13T10:30:00+00:00"},
                    "end": {"dateTime": "2026-07-13T11:00:00+00:00"},
                }
            ]
        }


class _InboxGoogle:
    def __init__(self, *, messages: bool = True) -> None:
        self.messages = messages
        self.calls: list[tuple[str, dict | None]] = []

    async def get_json(self, url: str, *, params: dict | None = None) -> dict:
        self.calls.append((url, params))
        if url.endswith("/messages"):
            return {"messages": [{"id": "m1"}, {"id": "m2"}]} if self.messages else {}
        if url.endswith("/messages/m1"):
            return {
                "id": "m1",
                "threadId": "thread-private-1",
                "snippet": "Review &amp; approve the attached plan.",
                "payload": {
                    "headers": [
                        {"name": "From", "value": "Alice Example <alice@example.com>"},
                        {"name": "Subject", "value": "Project update"},
                        {"name": "Date", "value": "Mon, 13 Jul 2026 09:30:00 +0000"},
                    ]
                },
            }
        return {
            "id": "m2",
            "threadId": "thread-private-2",
            "snippet": "The receipt is ready.\nOpen the portal for details.",
            "payload": {
                "headers": [
                    {"name": "From", "value": "billing@example.com"},
                    {"name": "Subject", "value": "Your receipt"},
                    {"name": "Date", "value": "not-a-date"},
                ]
            },
        }


async def test_inbox_status_uses_one_count_only_listing_without_message_fetches() -> None:
    google = _Google()
    reply = await inbox_status(ConnectorRegistry(google=google))
    assert reply == "Inbox: about 3 unread message(s)."
    assert len(google.calls) == 1
    url, params = google.calls[0]
    assert url.endswith("/messages")
    assert params == {"q": "in:inbox is:unread", "maxResults": 1}


async def test_today_inbox_summary_returns_bounded_metadata_without_bodies() -> None:
    google = _InboxGoogle()
    kst = dt.timezone(dt.timedelta(hours=9))
    now = dt.datetime(2026, 7, 13, 18, 0, tzinfo=kst)
    reply = await inbox_today_summary(ConnectorRegistry(google=google), now=now)

    start = int(dt.datetime(2026, 7, 13, 0, 0, tzinfo=kst).timestamp())
    end = int(dt.datetime(2026, 7, 14, 0, 0, tzinfo=kst).timestamp())
    assert google.calls[0] == (
        "https://www.googleapis.com/gmail/v1/users/me/messages",
        {"q": f"in:inbox after:{start} before:{end}", "maxResults": 8},
    )
    assert all(call[1] == {"format": "metadata", "metadataHeaders": ["From", "Subject", "Date"]}
               for call in google.calls[1:])
    assert reply == (
        "Today's inbox — 2 message(s):\n\n"
        "1. 18:30 · Alice Example\n"
        "Project update — Review & approve the attached plan.\n\n"
        "2. time unknown · billing@example.com\n"
        "Your receipt — The receipt is ready. Open the portal for details."
    )
    assert "thread-private" not in reply and "alice@example.com" not in reply


async def test_today_inbox_summary_reports_an_empty_day() -> None:
    reply = await inbox_today_summary(
        ConnectorRegistry(google=_InboxGoogle(messages=False)),
        now=dt.datetime(2026, 7, 13, 18, 0, tzinfo=dt.UTC),
    )
    assert reply == "Today's inbox: no messages received since local midnight."


async def test_calendar_status_excludes_event_content_from_telegram_reply() -> None:
    google = _Google()
    reply = await calendar_status(
        ConnectorRegistry(google=google),
        calendar_id="primary",
        now=dt.datetime(2026, 7, 13, 9, 0, tzinfo=dt.UTC),
    )
    assert reply == "Calendar: 1 event(s) in the next 24 hours. Next starts today at 10:30."
    assert "Sensitive" not in reply and "Private" not in reply and "owner@example.com" not in reply
    assert len(google.calls) == 1
