"""Read-only, bounded workspace summaries for the allowlisted Telegram companion.

These helpers are deliberately command-only. They never feed Google data to the remote model.
Briefing remains count/timing-only; the explicit inbox command may return a capped metadata and
snippet view to the configured private owner chat.
"""

from __future__ import annotations

import datetime as dt
import html
import re
from dataclasses import dataclass
from email.utils import parseaddr, parsedate_to_datetime
from typing import Any, Literal

from jarvis.connectors.base import ConnectorError, ConnectorRegistry
from jarvis.connectors.google import calendar, gmail

_GOOGLE_NOT_CONNECTED = (
    "Google Workspace is not connected on this Kairo instance. Enable connectors.google, then "
    "run `uv run jarvis connect google` locally."
)
_REMOTE_INBOX_MAX_MESSAGES = 8
_REMOTE_BODY_CHARS_PER_MESSAGE = 6_000
_REMOTE_BODY_CHARS_TOTAL = 30_000
_REMOTE_SUMMARY_CHARS = 220
_URL = re.compile(r"https?://\S+|www\.\S+", re.IGNORECASE)
_QUOTED_HISTORY = re.compile(
    r"(?:\n\s*On .{0,300}? wrote:\s*\n|\n\s*From:\s*.+|\n-{2,}\s*Original Message\s*-{2,})",
    re.IGNORECASE | re.DOTALL,
)


@dataclass(frozen=True)
class InboxWorkspaceResult:
    text: str
    message_ids: tuple[str, ...] = ()


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
    return _one_line(name or address or "Unknown sender", limit=60)


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
    tokens: list[str] = []
    used = 0
    for raw in re.findall(r"[\w@.+-]+", value, re.UNICODE)[:8]:
        token = raw[:40]
        separator = 1 if tokens else 0
        if used + len(token) + separator > 120:
            break
        tokens.append(token)
        used += len(token) + separator
    display = " ".join(tokens)
    gmail_query = " ".join(f'"{token}"' for token in tokens)
    return display, gmail_query


def _safe_extract(value: str, *, fallback: str) -> str:
    """Produce a local extractive summary; content remains inert and never enters a model."""
    text = html.unescape((value or "")[:_REMOTE_BODY_CHARS_PER_MESSAGE])
    text = _QUOTED_HISTORY.split(text, maxsplit=1)[0]
    text = _URL.sub("[link omitted]", text)
    text = re.sub(r"(?i)(?<!\w)/(approve|deny|cancel)\b", r"／\1", text)
    text = _one_line(text, limit=_REMOTE_SUMMARY_CHARS)
    return text or fallback


async def _bound_inbox_followup(
    client: Any,
    *,
    message_ids: tuple[str, ...],
    display_filter: str,
    mode: Literal["summarize_each", "detail"],
    item_index: int | None,
    timezone: dt.tzinfo | None,
) -> InboxWorkspaceResult:
    bound = message_ids[:_REMOTE_INBOX_MAX_MESSAGES]
    if not bound:
        return InboxWorkspaceResult(
            "That inbox selection is empty. Ask Kairo to show the inbox again."
        )
    if mode == "detail":
        if item_index is None or item_index < 1 or item_index > len(bound):
            return InboxWorkspaceResult(
                f"Choose an email number from 1 to {len(bound)}.", message_ids=bound
            )
        selected: list[tuple[int, str]] = [(item_index, bound[item_index - 1])]
    else:
        selected = list(enumerate(bound, start=1))

    loaded: list[tuple[int, gmail.Message]] = []
    remaining = _REMOTE_BODY_CHARS_TOTAL
    try:
        for index, message_id in selected:
            if remaining <= 0:
                break
            message = await gmail.get_message(client, message_id)
            body = message.body[: min(_REMOTE_BODY_CHARS_PER_MESSAGE, remaining)]
            remaining -= len(body)
            loaded.append(
                (
                    index,
                    gmail.Message(
                        id="",
                        thread_id="",
                        sender=message.sender,
                        to="",
                        subject=message.subject,
                        date=message.date,
                        body=body,
                    ),
                )
            )
    except ConnectorError as exc:
        return InboxWorkspaceResult(
            f"Kairo could not read that Gmail selection: {exc.user_message}", message_ids=bound
        )
    except Exception:
        return InboxWorkspaceResult(
            "Kairo could not summarize that Gmail selection right now.", message_ids=bound
        )

    scope = f" matching {display_filter}" if display_filter else ""
    if mode == "detail":
        heading = f"Email {loaded[0][0]} from today's inbox{scope}:"
    else:
        heading = f"Summaries for today's inbox{scope} — {len(loaded)} message(s):"
    lines = [heading]
    for index, message in loaded:
        sender = _sender_name(message.sender)
        subject = _one_line(message.subject, limit=100) or "(no subject)"
        received = _message_time(message.date, timezone=timezone)
        fallback = "No readable body preview was available."
        summary = _safe_extract(message.body, fallback=fallback)
        lines.append(f"{index}. {received} · {sender}\n{subject}\nSummary: {summary}")
    return InboxWorkspaceResult("\n\n".join(lines), message_ids=bound)


async def inbox_today_view(
    connectors: ConnectorRegistry | None,
    *,
    now: dt.datetime | None = None,
    max_messages: int = _REMOTE_INBOX_MAX_MESSAGES,
    filter_terms: str = "",
    mode: Literal["list", "summarize_each", "detail"] = "list",
    item_index: int | None = None,
    message_ids: tuple[str, ...] = (),
) -> InboxWorkspaceResult:
    """Return a bounded inbox view or a follow-up bound to exact in-memory message IDs."""
    client = _google_client(connectors)
    if client is None:
        return InboxWorkspaceResult(_GOOGLE_NOT_CONNECTED)
    current = _local_now(now)
    display_filter, gmail_filter = _search_terms(filter_terms)
    if mode != "list":
        return await _bound_inbox_followup(
            client,
            message_ids=message_ids,
            display_filter=display_filter,
            mode=mode,
            item_index=item_index,
            timezone=current.tzinfo,
        )

    start = current.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + dt.timedelta(days=1)
    cap = max(1, min(max_messages, _REMOTE_INBOX_MAX_MESSAGES))
    query = f"in:inbox after:{int(start.timestamp())} before:{int(end.timestamp())}"
    if gmail_filter:
        query += f" {gmail_filter}"
    try:
        messages = await gmail.search(client, query=query, max_results=cap)
    except ConnectorError as exc:
        return InboxWorkspaceResult(f"Kairo could not check Gmail: {exc.user_message}")
    except Exception:
        return InboxWorkspaceResult(
            "Kairo could not check Gmail right now. Please try again or use local Kairo."
        )
    if not messages:
        if display_filter:
            return InboxWorkspaceResult(f"Today's inbox: no messages matched {display_filter}.")
        return InboxWorkspaceResult("Today's inbox: no messages received since local midnight.")

    qualifier = f"up to {cap} recent" if len(messages) == cap else str(len(messages))
    scope = f" matching {display_filter}" if display_filter else ""
    lines = [f"Today's inbox{scope} — {qualifier} message(s):"]
    for index, message in enumerate(messages, start=1):
        sender = _sender_name(message.sender)
        subject = _one_line(message.subject, limit=100) or "(no subject)"
        snippet = _one_line(message.snippet, limit=150) or "No preview available."
        received = _message_time(message.date, timezone=current.tzinfo)
        lines.append(f"{index}. {received} · {sender}\n{subject} — {snippet}")
    lines.append("Reply 'summarize each' or 'show number 2' while this list is active.")
    return InboxWorkspaceResult(
        "\n\n".join(lines),
        message_ids=tuple(message.id for message in messages if message.id),
    )


async def inbox_today_summary(
    connectors: ConnectorRegistry | None,
    *,
    now: dt.datetime | None = None,
    max_messages: int = _REMOTE_INBOX_MAX_MESSAGES,
    filter_terms: str = "",
) -> str:
    """Return today's capped sender/subject/snippet view without exposing full message bodies."""
    result = await inbox_today_view(
        connectors,
        now=now,
        max_messages=max_messages,
        filter_terms=filter_terms,
    )
    return result.text


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
