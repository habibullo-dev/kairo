"""Read-only, bounded workspace summaries for the allowlisted Telegram companion.

These helpers are deliberately command-only. They never feed Google data to the remote model.
Briefing remains count/timing-only; the explicit inbox command may return a capped metadata and
snippet view to the configured private owner chat.
"""

from __future__ import annotations

import datetime as dt
import html
import re
from email.utils import parseaddr, parsedate_to_datetime
from typing import Any

from jarvis.connectors.base import ConnectorError, ConnectorRegistry
from jarvis.connectors.google import calendar, gmail

_GOOGLE_NOT_CONNECTED = (
    "Google Workspace is not connected on this Kairo instance. Enable connectors.google, then "
    "run `uv run jarvis connect google` locally."
)
_REMOTE_INBOX_MAX_MESSAGES = 8


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


def _one_line(value: str, *, limit: int) -> str:
    plain = html.unescape(value or "")
    plain = re.sub(r"[\x00-\x1f\x7f]+", " ", plain)
    plain = " ".join(plain.split())
    if len(plain) <= limit:
        return plain
    return plain[: max(1, limit - 1)].rstrip() + "…"


def _sender_name(value: str) -> str:
    name, address = parseaddr(value or "")
    return _one_line(name or address or "Unknown sender", limit=80)


def _message_time(value: str, *, timezone: dt.tzinfo | None) -> str:
    try:
        parsed = parsedate_to_datetime(value)
        if parsed is None:
            return "time unknown"
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.UTC)
        return parsed.astimezone(timezone).strftime("%H:%M")
    except (TypeError, ValueError, OverflowError):
        return "time unknown"


def _search_terms(value: str) -> tuple[str, str]:
    tokens = [token[:40] for token in re.findall(r"[\w@.+-]+", value, re.UNICODE)[:8]]
    display = " ".join(tokens)
    gmail_query = " ".join(f'"{token}"' for token in tokens)
    return display, gmail_query


async def inbox_today_summary(
    connectors: ConnectorRegistry | None,
    *,
    now: dt.datetime | None = None,
    max_messages: int = _REMOTE_INBOX_MAX_MESSAGES,
    filter_terms: str = "",
) -> str:
    """Return today's capped sender/subject/snippet view without exposing full message bodies."""
    client = _google_client(connectors)
    if client is None:
        return _GOOGLE_NOT_CONNECTED
    current = _local_now(now)
    start = current.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + dt.timedelta(days=1)
    cap = max(1, min(max_messages, _REMOTE_INBOX_MAX_MESSAGES))
    display_filter, gmail_filter = _search_terms(filter_terms)
    query = f"in:inbox after:{int(start.timestamp())} before:{int(end.timestamp())}"
    if gmail_filter:
        query += f" {gmail_filter}"
    try:
        messages = await gmail.search(client, query=query, max_results=cap)
    except ConnectorError as exc:
        return f"Kairo could not check Gmail: {exc.user_message}"
    except Exception:
        return "Kairo could not check Gmail right now. Please try again or use local Kairo."
    if not messages:
        if display_filter:
            return f"Today's inbox: no messages matched {display_filter}."
        return "Today's inbox: no messages received since local midnight."

    qualifier = f"up to {cap} recent" if len(messages) == cap else str(len(messages))
    scope = f" matching {display_filter}" if display_filter else ""
    lines = [f"Today's inbox{scope} — {qualifier} message(s):"]
    for index, message in enumerate(messages, start=1):
        sender = _sender_name(message.sender)
        subject = _one_line(message.subject, limit=120) or "(no subject)"
        snippet = _one_line(message.snippet, limit=220) or "No preview available."
        received = _message_time(message.date, timezone=current.tzinfo)
        lines.append(f"{index}. {received} · {sender}\n{subject} — {snippet}")
    return "\n\n".join(lines)


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
