"""Google Calendar adapter (read-only). Returns frozen dataclasses, never raw dicts.

Endpoints are constants; the caller supplies an RFC3339 ``time_min``/``time_max`` window
(computed in the user's timezone by the tool/collector). ``singleEvents=true`` expands
recurring events so "today" means occurrences, not series.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import quote

from jarvis.connectors.google.client import GoogleClient

_CAL_API = "https://www.googleapis.com/calendar/v3"
_MAX_RESULTS = 50


@dataclass(frozen=True)
class CalendarEvent:
    id: str
    summary: str
    start: str  # RFC3339 datetime, or a bare date for all-day events
    end: str
    location: str
    organizer: str
    all_day: bool


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
