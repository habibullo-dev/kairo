"""The preview / diff builder: turn a :mod:`jarvis.actions.requests` object into the exact,
human-readable summary the person approves BEFORE any write happens (roadmap R5).

Everything here is pure and deterministic — no network, no locale, no clock. Dates and times are
formatted from fixed abbreviation tables (not ``strftime``, whose ``%-I`` / ``%#I`` differ across
platforms and whose month/day names depend on the machine locale), so a golden preview is stable
on any machine. Timezone resolution uses stdlib :mod:`zoneinfo`; recurrence expansion is a small
in-house expander for the common ``FREQ``/``INTERVAL``/``COUNT``/``UNTIL`` rules and honestly
declines (a warning, never a wrong guess) on advanced ``BY*`` rules.

For an update, the builder diffs the request against the ``remote`` snapshot of the live event so
the human sees precisely which fields change (and nothing else). If the remote could not be
loaded, it says so and shows the requested values rather than pretending to diff.
"""

from __future__ import annotations

import calendar as _cal
import datetime as _dt
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from jarvis.actions.requests import (
    CalendarCancelRequest,
    CalendarCreateRequest,
    CalendarUpdateRequest,
    DocAppendOp,
    DocCreateRequest,
    DocReplaceOp,
    DocUpdateRequest,
    DraftCreateRequest,
    DraftUpdateRequest,
    WriteRequest,
)

_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_FREQ_LABEL = {"DAILY": "day", "WEEKLY": "week", "MONTHLY": "month", "YEARLY": "year"}
_SEND_UPDATES_NOTE = {
    "all": "Attendees will be notified by email (sendUpdates=all).",
    "none": "No email notifications will be sent (sendUpdates=none).",
    "externalOnly": (
        "Only guests outside your organization will be notified (sendUpdates=externalOnly)."
    ),
}
_BODY_PREVIEW_CHARS = 200


@dataclass(frozen=True)
class Preview:
    """The rendered preview. ``fields`` are (label, value) display rows; ``diff`` are
    (field, old, new) change rows (updates only); ``notes`` explain side effects (Meet link,
    who gets notified); ``warnings`` flag anything the human should double-check."""

    kind: str
    title: str
    fields: tuple[tuple[str, str], ...] = ()
    diff: tuple[tuple[str, str, str], ...] = ()
    notes: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        """JSON-ready form (stored as ``write_intents.preview_json``)."""
        return {
            "kind": self.kind,
            "title": self.title,
            "fields": [{"label": lbl, "value": v} for lbl, v in self.fields],
            "diff": [{"field": f, "old": o, "new": n} for f, o, n in self.diff],
            "notes": list(self.notes),
            "warnings": list(self.warnings),
        }


# --- time / date formatting (deterministic, locale-independent) ------------------------------


def _parse_dt(value: str, tz: str) -> _dt.datetime:
    """Parse an ISO instant into an aware datetime in ``tz``. A naive value is interpreted as
    local to ``tz``; an offset-bearing value is converted into ``tz``."""
    dt = _dt.datetime.fromisoformat(value)
    zone = ZoneInfo(tz)
    return dt.replace(tzinfo=zone) if dt.tzinfo is None else dt.astimezone(zone)


def _parse_date(value: str) -> _dt.date:
    return _dt.date.fromisoformat(value[:10])


def _fmt_time(dt: _dt.datetime) -> str:
    hour = dt.hour % 12 or 12
    return f"{hour}:{dt.minute:02d} {'AM' if dt.hour < 12 else 'PM'}"


def _fmt_date(dt: _dt.date) -> str:
    return f"{_WEEKDAYS[dt.weekday()]}, {_MONTHS[dt.month - 1]} {dt.day} {dt.year}"


def _fmt_date_short(dt: _dt.date) -> str:
    return f"{_MONTHS[dt.month - 1]} {dt.day}"


def _fmt_range(start: str, end: str, tz: str, all_day: bool) -> str:
    """A human when-string. All-day events show inclusive dates (Google's end is exclusive)."""
    if all_day:
        s = _parse_date(start)
        e = _parse_date(end)
        last = e - _dt.timedelta(days=1)  # exclusive end → inclusive last day
        if last <= s:
            return f"{_fmt_date(s)} (all day)"
        return f"{_fmt_date(s)} – {_fmt_date(last)} (all day)"
    s = _parse_dt(start, tz)
    e = _parse_dt(end, tz)
    if s.date() == e.date():
        return f"{_fmt_date(s)} · {_fmt_time(s)} – {_fmt_time(e)} ({tz})"
    return f"{_fmt_date(s)} {_fmt_time(s)} – {_fmt_date(e)} {_fmt_time(e)} ({tz})"


# --- recurrence ------------------------------------------------------------------------------


def _add_months(dt: _dt.datetime, months: int) -> _dt.datetime:
    total = dt.month - 1 + months
    year = dt.year + total // 12
    month = total % 12 + 1
    day = min(dt.day, _cal.monthrange(year, month)[1])  # clamp (Jan 31 + 1mo → Feb 28/29)
    return dt.replace(year=year, month=month, day=day)


def _parse_rrule(recurrence: tuple[str, ...]) -> dict[str, str] | None:
    """Parse the first RRULE line into a parts dict, or None if there isn't one."""
    for line in recurrence:
        body = line.split("RRULE:", 1)[1] if line.upper().startswith("RRULE:") else line
        parts: dict[str, str] = {}
        for token in body.split(";"):
            if "=" in token:
                key, _, val = token.partition("=")
                parts[key.strip().upper()] = val.strip()
        if parts:
            return parts
    return None


def _expand_rrule(
    start: _dt.datetime, parts: dict[str, str], count: int = 3
) -> list[_dt.datetime] | None:
    """Up to ``count`` occurrence starts for a SIMPLE rule, or None for an advanced one.

    Supported: FREQ ∈ {DAILY,WEEKLY,MONTHLY,YEARLY}, INTERVAL, COUNT, UNTIL. Any BY* / WKST /
    BYSETPOS part ⇒ None (we decline rather than expand it wrong)."""
    if any(key.startswith("BY") or key in {"WKST"} for key in parts):
        return None
    freq = parts.get("FREQ", "").upper()
    if freq not in _FREQ_LABEL:
        return None
    interval = max(1, int(parts["INTERVAL"])) if parts.get("INTERVAL", "").isdigit() else 1
    limit = int(parts["COUNT"]) if parts.get("COUNT", "").isdigit() else None
    until: _dt.datetime | None = None
    if "UNTIL" in parts:
        try:
            until = _parse_dt(parts["UNTIL"].replace("Z", "+00:00"), str(start.tzinfo))
        except ValueError:
            until = None

    def step(dt: _dt.datetime) -> _dt.datetime:
        if freq == "DAILY":
            return dt + _dt.timedelta(days=interval)
        if freq == "WEEKLY":
            return dt + _dt.timedelta(weeks=interval)
        if freq == "MONTHLY":
            return _add_months(dt, interval)
        return _add_months(dt, 12 * interval)  # YEARLY

    out: list[_dt.datetime] = []
    cur = start
    while len(out) < count:
        if limit is not None and len(out) >= limit:
            break
        if until is not None and cur > until:
            break
        out.append(cur)
        cur = step(cur)
    return out


def _recurrence_row(recurrence: tuple[str, ...], start: _dt.datetime) -> tuple[str, list[str]]:
    """Return (display value, warnings) for a recurrence rule."""
    parts = _parse_rrule(recurrence)
    if parts is None:
        return " ; ".join(recurrence), []
    freq = parts.get("FREQ", "").upper()
    interval = int(parts["INTERVAL"]) if parts.get("INTERVAL", "").isdigit() else 1
    unit = _FREQ_LABEL.get(freq, "occurrence")
    label = f"Every {interval} {unit}s" if interval > 1 else f"Every {unit}"
    occ = _expand_rrule(start, parts)
    if not occ:
        return (
            f"{label} — {' ; '.join(recurrence)}",
            ["Recurrence uses advanced rules; showing the rule text, not expanded dates."],
        )
    return f"{label} · next: {', '.join(_fmt_date_short(o) for o in occ)}", []


def _send_updates_note(value: str) -> str:
    return _SEND_UPDATES_NOTE.get(value, f"sendUpdates={value}")


def _truncate(text: str, limit: int = _BODY_PREVIEW_CHARS) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[:limit].rstrip() + "…"


# --- per-verb builders -----------------------------------------------------------------------


def _calendar_create(req: CalendarCreateRequest) -> Preview:
    fields: list[tuple[str, str]] = []
    notes: list[str] = []
    warnings: list[str] = []
    if not req.summary.strip():
        warnings.append("The event has no title.")
    if req.calendar_id != "primary":
        fields.append(("Calendar", req.calendar_id))
    when = _fmt_range(req.start, req.end, req.timezone, req.all_day)
    fields.append(("When", when))
    if not req.all_day:
        try:
            if _parse_dt(req.end, req.timezone) <= _parse_dt(req.start, req.timezone):
                warnings.append("The end time is not after the start time.")
        except ValueError:
            warnings.append("Could not parse the start/end time.")
    fields.append(("Attendees", ", ".join(req.attendees) if req.attendees else "None"))
    if req.location:
        fields.append(("Location", req.location))
    if req.description:
        fields.append(("Description", _truncate(req.description)))
    if req.recurrence:
        value, warns = _recurrence_row(req.recurrence, _parse_dt(req.start, req.timezone))
        fields.append(("Recurrence", value))
        warnings.extend(warns)
    if req.add_meet:
        notes.append("A Google Meet video link will be created and attached.")
    if req.attendees:
        notes.append(_send_updates_note(req.send_updates))
    return Preview(
        kind=req.kind.value,
        title=f"Create event: {req.summary or '(untitled)'}",
        fields=tuple(fields),
        notes=tuple(notes),
        warnings=tuple(warnings),
    )


def _calendar_update(req: CalendarUpdateRequest, remote: dict | None) -> Preview:
    title_name = req.summary or (remote or {}).get("summary") or req.event_id
    notes: list[str] = []
    warnings: list[str] = []
    diff: list[tuple[str, str, str]] = []
    fields: list[tuple[str, str]] = []

    if remote is None:
        warnings.append("Could not load the current event; showing the requested changes only.")
        if req.summary is not None:
            fields.append(("Summary", req.summary))
        if req.location is not None:
            fields.append(("Location", req.location))
        if req.start is not None or req.end is not None:
            fields.append(("Start", req.start or "(unchanged)"))
            fields.append(("End", req.end or "(unchanged)"))
    else:
        _diff_scalar(diff, "Summary", remote.get("summary"), req.summary)
        _diff_time(diff, req, remote, warnings)
        _diff_scalar(diff, "Location", remote.get("location"), req.location)
        _diff_scalar(diff, "Description", remote.get("description"), req.description)
        _diff_attendees(diff, remote.get("attendees") or [], req.attendees)
        _diff_recurrence(diff, req, remote)
        if not diff:
            warnings.append("This update does not change anything.")

    if req.add_meet and not (remote or {}).get("has_meet"):
        notes.append("A Google Meet video link will be added.")
    notes.append(_send_updates_note(req.send_updates))
    return Preview(
        kind=req.kind.value,
        title=f"Update event: {title_name}",
        fields=tuple(fields),
        diff=tuple(diff),
        notes=tuple(notes),
        warnings=tuple(warnings),
    )


def _diff_scalar(diff: list, label: str, old: str | None, new: str | None) -> None:
    if new is not None and (old or "") != new:
        diff.append((label, old or "(none)", new or "(none)"))


def _diff_time(diff: list, req: CalendarUpdateRequest, remote: dict, warnings: list) -> None:
    if req.start is None and req.end is None and req.all_day is None:
        return
    old_tz = remote.get("timezone") or "UTC"
    new_tz = req.timezone or old_tz
    old_all_day = bool(remote.get("all_day"))
    new_all_day = req.all_day if req.all_day is not None else old_all_day
    old_start, old_end = remote.get("start"), remote.get("end")
    new_start = req.start or old_start
    new_end = req.end or old_end
    if not (old_start and old_end and new_start and new_end):
        warnings.append("The event has no start/end to compare against.")
        return
    old_range = _fmt_range(old_start, old_end, old_tz, old_all_day)
    new_range = _fmt_range(new_start, new_end, new_tz, new_all_day)
    if old_range != new_range:
        diff.append(("When", old_range, new_range))


def _diff_attendees(diff: list, old: list[str], new: tuple[str, ...] | None) -> None:
    if new is None:
        return
    old_set, new_set = set(old), set(new)
    if old_set == new_set:
        return
    added = sorted(new_set - old_set)
    removed = sorted(old_set - new_set)
    changes = []
    if added:
        changes.append("+ " + ", ".join(added))
    if removed:
        changes.append("- " + ", ".join(removed))
    diff.append(("Attendees", ", ".join(old) or "None", " ; ".join(changes)))


def _diff_recurrence(diff: list, req: CalendarUpdateRequest, remote: dict) -> None:
    if req.recurrence is None:
        return
    old = " ; ".join(remote.get("recurrence") or []) or "None"
    new = " ; ".join(req.recurrence) or "None (one-off)"
    if old != new:
        diff.append(("Recurrence", old, new))


def _calendar_cancel(req: CalendarCancelRequest) -> Preview:
    fields = [("Event", req.summary or req.event_id)]
    if req.calendar_id != "primary":
        fields.append(("Calendar", req.calendar_id))
    return Preview(
        kind=req.kind.value,
        title=f"Cancel event: {req.summary or req.event_id}",
        fields=tuple(fields),
        notes=(_send_updates_note(req.send_updates),),
    )


def _doc_create(req: DocCreateRequest) -> Preview:
    warnings = [] if req.title.strip() else ["The document has no title."]
    fields = [("Title", req.title or "(untitled)")]
    if req.body.strip():
        fields.append(("Content", _truncate(req.body)))
    return Preview(
        kind=req.kind.value,
        title=f"Create doc: {req.title or '(untitled)'}",
        fields=tuple(fields),
        warnings=tuple(warnings),
    )


def _doc_update(req: DocUpdateRequest) -> Preview:
    fields: list[tuple[str, str]] = []
    for i, op in enumerate(req.operations, 1):
        if isinstance(op, DocReplaceOp):
            find, repl = _truncate(op.find, 60), _truncate(op.replace, 60)
            fields.append((f"Edit {i}", f"Replace '{find}' with '{repl}'"))
        elif isinstance(op, DocAppendOp):
            fields.append((f"Edit {i}", f"Append: {_truncate(op.text)}"))
    warnings = [] if req.operations else ["This edit has no operations."]
    return Preview(
        kind=req.kind.value,
        title=f"Edit doc: {req.title or req.document_id}",
        fields=tuple(fields),
        warnings=tuple(warnings),
    )


def _draft_create(req: DraftCreateRequest) -> Preview:
    warnings: list[str] = []
    if not req.to.strip():
        warnings.append("The draft has no recipient.")
    if not req.subject.strip():
        warnings.append("The draft has no subject.")
    fields = [
        ("To", req.to or "(none)"),
        ("Subject", req.subject or "(none)"),
        ("Body", _truncate(req.body)),
    ]
    if req.reply_to_message_id:
        fields.append(("In reply to", req.reply_to_message_id))
    return Preview(
        kind=req.kind.value,
        title=f"Draft email: {req.subject or '(no subject)'}",
        fields=tuple(fields),
        notes=("This creates a DRAFT only — Kira never sends mail.",),
        warnings=tuple(warnings),
    )


def _draft_update(req: DraftUpdateRequest) -> Preview:
    fields = [
        ("To", req.to or "(none)"),
        ("Subject", req.subject or "(none)"),
        ("Body", _truncate(req.body)),
    ]
    return Preview(
        kind=req.kind.value,
        title=f"Update draft: {req.subject or '(no subject)'}",
        fields=tuple(fields),
        notes=("This updates a DRAFT only — Kira never sends mail.",),
    )


def build_preview(request: WriteRequest, *, remote: dict | None = None) -> Preview:
    """Render ``request`` into the human-approval :class:`Preview`. ``remote`` is the live
    snapshot of the target (calendar update only) so the diff shows exactly what changes."""
    if isinstance(request, CalendarCreateRequest):
        return _calendar_create(request)
    if isinstance(request, CalendarUpdateRequest):
        return _calendar_update(request, remote)
    if isinstance(request, CalendarCancelRequest):
        return _calendar_cancel(request)
    if isinstance(request, DocCreateRequest):
        return _doc_create(request)
    if isinstance(request, DocUpdateRequest):
        return _doc_update(request)
    if isinstance(request, DraftCreateRequest):
        return _draft_create(request)
    if isinstance(request, DraftUpdateRequest):
        return _draft_update(request)
    raise TypeError(f"no preview builder for {type(request).__name__}")
