"""Read-only, privacy-minimized workspace summaries for the Telegram companion.

These helpers are deliberately command-only.  They never feed Google data to the remote model
and return counts/timing rather than mail or event content, so Telegram stays a small status
surface instead of an off-box mirror of a private workspace.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from jarvis.connectors.base import ConnectorError, ConnectorRegistry
from jarvis.connectors.google import calendar, gmail

_GOOGLE_NOT_CONNECTED = (
    "Google Workspace is not connected on this Kairo instance. Enable connectors.google, then "
    "run `uv run jarvis connect google` locally."
)


def _google_client(connectors: ConnectorRegistry | None) -> Any | None:
    return connectors.google if connectors is not None else None


async def inbox_status(connectors: ConnectorRegistry | None) -> str:
    """Return only Gmail's unread Inbox count; never query message metadata or bodies."""
    client = _google_client(connectors)
    if client is None:
        return _GOOGLE_NOT_CONNECTED
    try:
        summary = await gmail.unread_inbox_summary(client)
    except ConnectorError as exc:
        return f"Kairo could not check Gmail: {exc.user_message}"
    except Exception:
        return "Kairo could not check Gmail right now. Please try again or use local Kairo."
    if summary.unread_estimate is None:
        return "Inbox: Gmail did not provide an unread count."
    return f"Inbox: about {summary.unread_estimate} unread message(s)."


def _local_now(now: dt.datetime | None) -> dt.datetime:
    if now is not None:
        return now if now.tzinfo is not None else now.astimezone()
    return dt.datetime.now().astimezone()


def _next_event_timing(start: str | None, *, all_day: bool, now: dt.datetime) -> str:
    """Describe a timing-only calendar result without returning event content."""
    if not start:
        return "Next event time is unavailable."
    if all_day:
        return f"Next event: all day on {start}."
    try:
        starts_at = dt.datetime.fromisoformat(start.replace("Z", "+00:00")).astimezone(now.tzinfo)
    except ValueError:
        return "Next event time is unavailable."
    if starts_at.date() == now.date():
        return f"Next starts today at {starts_at:%H:%M}."
    return f"Next starts {starts_at:%a %H:%M}."


async def calendar_status(
    connectors: ConnectorRegistry | None,
    *,
    calendar_id: str,
    now: dt.datetime | None = None,
) -> str:
    """Return a next-24-hours calendar count plus next timing, never event content."""
    client = _google_client(connectors)
    if client is None:
        return _GOOGLE_NOT_CONNECTED
    current = _local_now(now)
    try:
        summary = await calendar.upcoming_window_summary(
            client,
            time_min=current.isoformat(),
            time_max=(current + dt.timedelta(hours=24)).isoformat(),
            calendar_id=calendar_id,
            max_results=50,
        )
    except ConnectorError as exc:
        return f"Kairo could not check Calendar: {exc.user_message}"
    except Exception:
        return "Kairo could not check Calendar right now. Please try again or use local Kairo."
    if summary.event_count == 0:
        return "Calendar: no events in the next 24 hours."
    count = f"{summary.event_count}+" if summary.has_more else str(summary.event_count)
    return (
        f"Calendar: {count} event(s) in the next 24 hours. "
        f"{_next_event_timing(summary.next_start, all_day=summary.next_all_day, now=current)}"
    )
