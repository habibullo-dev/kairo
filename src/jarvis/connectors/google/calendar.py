"""Google Calendar adapter. Reads return frozen dataclasses; writes take primitive params and
return the raw API resource for the caller (the executor) to journal.

Endpoints are constants; the caller supplies an RFC3339 ``time_min``/``time_max`` window
(computed in the user's timezone by the tool/collector). ``singleEvents=true`` expands
recurring events so "today" means occurrences, not series.

The write functions (Phase 12: ``create_event`` / ``update_event`` / ``cancel_event``) are thin
transport wrappers: they build the Google event body from primitive params and never accept a
model-supplied URL. They require the ``calendar.events`` scope (added at the Milestone-2 tool
wiring — this module is exercised only with a fake transport until then). The request→params
mapping from a stored :class:`~jarvis.actions.requests.CalendarCreateRequest` lives in the
executor (Milestone 2), NOT here, so this connector layer keeps no upward dependency on actions.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from urllib.parse import quote

from jarvis.connectors.google.client import GoogleClient

_CAL_API = "https://www.googleapis.com/calendar/v3"
_MAX_RESULTS = 50
_MEET_SOLUTION = {"type": "hangoutsMeet"}  # conferenceData.createRequest solution for Google Meet


@dataclass(frozen=True)
class CalendarEvent:
    id: str
    summary: str
    start: str  # RFC3339 datetime, or a bare date for all-day events
    end: str
    location: str
    organizer: str
    all_day: bool


@dataclass(frozen=True)
class CalendarWindowSummary:
    """The minimum calendar data needed by a remote status surface.

    It intentionally holds no title, location, attendee, organizer, or event identifier.  The
    underlying API request also uses a partial-response field mask so those fields never cross
    this adapter boundary in the first place.
    """

    event_count: int
    next_start: str | None
    next_all_day: bool
    has_more: bool


async def list_events(
    client: GoogleClient,
    *,
    time_min: str,
    time_max: str,
    calendar_id: str = "primary",
    max_results: int = 25,
) -> list[CalendarEvent]:
    cap = max(1, min(max_results, _MAX_RESULTS))
    data = await client.get_json(
        f"{_CAL_API}/calendars/{quote(calendar_id, safe='')}/events",
        params={
            "timeMin": time_min,
            "timeMax": time_max,
            "singleEvents": "true",
            "orderBy": "startTime",
            "maxResults": cap,
        },
    )
    events: list[CalendarEvent] = []
    for item in data.get("items", [])[:cap]:
        start = item.get("start", {}) or {}
        end = item.get("end", {}) or {}
        events.append(
            CalendarEvent(
                id=item.get("id", ""),
                summary=item.get("summary", "(no title)"),
                start=start.get("dateTime") or start.get("date") or "",
                end=end.get("dateTime") or end.get("date") or "",
                location=item.get("location", ""),
                organizer=(item.get("organizer") or {}).get("email", ""),
                all_day="date" in start,  # all-day events carry `date`, timed carry `dateTime`
            )
        )
    return events


async def upcoming_window_summary(
    client: GoogleClient,
    *,
    time_min: str,
    time_max: str,
    calendar_id: str = "primary",
    max_results: int = _MAX_RESULTS,
) -> CalendarWindowSummary:
    """Return a count/timing-only calendar window without fetching event content.

    The fixed field mask asks Google solely for each event's start and a pagination sentinel.
    A ``has_more`` result means the displayed count is a lower bound rather than a misleading
    complete count.
    """
    cap = max(1, min(max_results, _MAX_RESULTS))
    data = await client.get_json(
        f"{_CAL_API}/calendars/{quote(calendar_id, safe='')}/events",
        params={
            "timeMin": time_min,
            "timeMax": time_max,
            "singleEvents": "true",
            "orderBy": "startTime",
            "maxResults": cap,
            "fields": "items(start),nextPageToken",
        },
    )
    items = data.get("items") or []
    if not isinstance(items, list):
        items = []
    first = items[0] if items and isinstance(items[0], dict) else {}
    start = first.get("start", {}) if isinstance(first, dict) else {}
    if not isinstance(start, dict):
        start = {}
    next_start = start.get("dateTime") or start.get("date")
    return CalendarWindowSummary(
        event_count=len(items),
        next_start=next_start if isinstance(next_start, str) else None,
        next_all_day="date" in start,
        has_more=bool(data.get("nextPageToken")),
    )


# --- writes (Phase 12) -----------------------------------------------------


def _events_url(calendar_id: str) -> str:
    return f"{_CAL_API}/calendars/{quote(calendar_id, safe='')}/events"


def _event_url(calendar_id: str, event_id: str) -> str:
    return f"{_events_url(calendar_id)}/{quote(event_id, safe='')}"


def _time_field(value: str, timezone: str | None, all_day: bool) -> dict:
    """A Google start/end object: ``{date}`` for all-day, else ``{dateTime[, timeZone]}``.
    An omitted timeZone lets Google use the event/calendar default (used on partial updates)."""
    if all_day:
        return {"date": value}
    field: dict = {"dateTime": value}
    if timezone:
        field["timeZone"] = timezone
    return field


def _meet_conference(request_id: str) -> dict:
    # requestId dedupes conference creation on the provider side; the executor passes the intent's
    # idempotency key so a retried create cannot mint a second Meet link.
    return {"createRequest": {"requestId": request_id, "conferenceSolutionKey": _MEET_SOLUTION}}


async def create_event(
    client: GoogleClient,
    *,
    summary: str,
    start: str,
    end: str,
    timezone: str,
    attendees: Sequence[str] = (),
    location: str = "",
    description: str = "",
    recurrence: Sequence[str] = (),
    all_day: bool = False,
    add_meet: bool = False,
    meet_request_id: str | None = None,
    send_updates: str = "none",
    calendar_id: str = "primary",
) -> dict:
    """Create an event; returns the created resource (id, htmlLink, conferenceData, …). When
    ``add_meet`` is set, ``meet_request_id`` is REQUIRED (idempotent Meet creation)."""
    body: dict = {
        "summary": summary,
        "start": _time_field(start, timezone, all_day),
        "end": _time_field(end, timezone, all_day),
    }
    if attendees:
        body["attendees"] = [{"email": a} for a in attendees]
    if location:
        body["location"] = location
    if description:
        body["description"] = description
    if recurrence:
        body["recurrence"] = list(recurrence)
    params: dict = {"sendUpdates": send_updates}
    if add_meet:
        if not meet_request_id:
            raise ValueError("meet_request_id is required when add_meet=True")
        body["conferenceData"] = _meet_conference(meet_request_id)
        params["conferenceDataVersion"] = 1
    return await client.post_json(_events_url(calendar_id), json_body=body, params=params)


async def update_event(
    client: GoogleClient,
    event_id: str,
    *,
    timezone: str | None = None,
    summary: str | None = None,
    start: str | None = None,
    end: str | None = None,
    attendees: Sequence[str] | None = None,
    location: str | None = None,
    description: str | None = None,
    recurrence: Sequence[str] | None = None,
    all_day: bool | None = None,
    add_meet: bool | None = None,
    meet_request_id: str | None = None,
    send_updates: str = "none",
    calendar_id: str = "primary",
) -> dict:
    """PATCH only the provided fields (a partial update); returns the updated resource. Fields
    left None are untouched on the remote event."""
    body: dict = {}
    if summary is not None:
        body["summary"] = summary
    if start is not None:
        body["start"] = _time_field(start, timezone, bool(all_day))
    if end is not None:
        body["end"] = _time_field(end, timezone, bool(all_day))
    if attendees is not None:
        body["attendees"] = [{"email": a} for a in attendees]
    if location is not None:
        body["location"] = location
    if description is not None:
        body["description"] = description
    if recurrence is not None:
        body["recurrence"] = list(recurrence)
    params: dict = {"sendUpdates": send_updates}
    if add_meet:
        if not meet_request_id:
            raise ValueError("meet_request_id is required when add_meet=True")
        body["conferenceData"] = _meet_conference(meet_request_id)
        params["conferenceDataVersion"] = 1
    return await client.patch_json(_event_url(calendar_id, event_id), json_body=body, params=params)


async def cancel_event(
    client: GoogleClient,
    event_id: str,
    *,
    send_updates: str = "all",
    calendar_id: str = "primary",
) -> None:
    """Cancel (DELETE) an event. Defaults to notifying guests (``sendUpdates=all``). Undo is a
    re-insert from the intent's stored request (handled by the executor), where the API allows."""
    await client.delete(_event_url(calendar_id, event_id), params={"sendUpdates": send_updates})


async def get_event(client: GoogleClient, event_id: str, *, calendar_id: str = "primary") -> dict:
    """Fetch one raw event resource — used to build the update preview's diff and to capture
    rollback state before an update."""
    return await client.get_json(_event_url(calendar_id, event_id))
