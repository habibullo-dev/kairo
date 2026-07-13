"""Telegram workspace summaries disclose only their documented minimized fields."""

from __future__ import annotations

import datetime as dt

from jarvis.connectors.base import ConnectorRegistry
from jarvis.remote.workspace import calendar_status, inbox_status


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


async def test_inbox_status_uses_one_count_only_listing_without_message_fetches() -> None:
    google = _Google()
    reply = await inbox_status(ConnectorRegistry(google=google))
    assert reply == "Inbox: about 3 unread message(s)."
    assert len(google.calls) == 1
    url, params = google.calls[0]
    assert url.endswith("/messages")
    assert params == {"q": "in:inbox is:unread", "maxResults": 1}


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
