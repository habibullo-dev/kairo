"""Preview / diff builder (Phase 12 Task 2). Pure, deterministic golden tests — no network.

The full-dict goldens pin the exact human-approval text (the "what you approve is what you get"
surface); the rest pin the load-bearing behaviors: timezone resolution, recurrence expansion vs
honest decline, field-level update diffs, and the safety notes (Meet, sendUpdates, drafts-only).
"""

from __future__ import annotations

import pytest

from jarvis.actions.preview import build_preview
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
)

_TZ = "America/New_York"
# Feb 1 2026 is a Sunday.
_WHEN_10 = "Sun, Feb 1 2026 · 10:00 AM – 10:15 AM (America/New_York)"
_WHEN_11 = "Sun, Feb 1 2026 · 11:00 AM – 11:30 AM (America/New_York)"


def test_calendar_create_full_golden() -> None:
    req = CalendarCreateRequest(
        summary="Standup",
        start="2026-02-01T10:00:00",
        end="2026-02-01T10:15:00",
        timezone=_TZ,
        attendees=("alice@example.com", "bob@example.com"),
        location="Room 4",
        recurrence=("RRULE:FREQ=WEEKLY",),
        add_meet=True,
        send_updates="all",
    )
    assert build_preview(req).to_dict() == {
        "kind": "calendar_create",
        "title": "Create event: Standup",
        "fields": [
            {"label": "When", "value": _WHEN_10},
            {"label": "Attendees", "value": "alice@example.com, bob@example.com"},
            {"label": "Location", "value": "Room 4"},
            {"label": "Recurrence", "value": "Every week · next: Feb 1, Feb 8, Feb 15"},
        ],
        "diff": [],
        "notes": [
            "A Google Meet video link will be created and attached.",
            "Attendees will be notified by email (sendUpdates=all).",
        ],
        "warnings": [],
    }


def test_calendar_create_warns_on_empty_title_and_backwards_time() -> None:
    req = CalendarCreateRequest(
        summary="  ",
        start="2026-02-01T10:00:00",
        end="2026-02-01T09:00:00",  # before start
        timezone=_TZ,
    )
    prev = build_preview(req)
    assert "The event has no title." in prev.warnings
    assert "The end time is not after the start time." in prev.warnings
    # No attendees ⇒ sendUpdates is moot, so no notification note.
    assert prev.notes == ()


def test_calendar_create_all_day() -> None:
    req = CalendarCreateRequest(
        summary="Holiday",
        start="2026-02-01",
        end="2026-02-02",  # Google's exclusive end
        timezone=_TZ,
        all_day=True,
    )
    when = dict(build_preview(req).fields)["When"]
    assert when == "Sun, Feb 1 2026 (all day)"


def test_recurrence_advanced_rule_is_not_expanded() -> None:
    req = CalendarCreateRequest(
        summary="Team sync",
        start="2026-02-01T10:00:00",
        end="2026-02-01T10:30:00",
        timezone=_TZ,
        recurrence=("RRULE:FREQ=WEEKLY;BYDAY=MO,WE",),
    )
    prev = build_preview(req)
    rec = dict(prev.fields)["Recurrence"]
    assert "BYDAY=MO,WE" in rec  # shows the rule text, not a wrong expansion
    assert any("advanced rules" in w for w in prev.warnings)


def test_recurrence_monthly_interval_and_count() -> None:
    req = CalendarCreateRequest(
        summary="Rent",
        start="2026-01-31T09:00:00",
        end="2026-01-31T09:15:00",
        timezone=_TZ,
        recurrence=("RRULE:FREQ=MONTHLY;INTERVAL=1;COUNT=2",),
    )
    rec = dict(build_preview(req).fields)["Recurrence"]
    # COUNT=2 caps at two occurrences; Jan 31 + 1 month clamps to Feb 28.
    assert rec == "Every month · next: Jan 31, Feb 28"


def test_calendar_update_diff_golden() -> None:
    remote = {
        "summary": "Standup",
        "start": "2026-02-01T10:00:00",
        "end": "2026-02-01T10:15:00",
        "timezone": _TZ,
        "location": "Room 4",
        "attendees": ["alice@example.com"],
        "all_day": False,
    }
    req = CalendarUpdateRequest(
        event_id="evt-1",
        timezone=_TZ,
        start="2026-02-01T11:00:00",
        end="2026-02-01T11:30:00",
        location="Room 7",
        send_updates="all",
    )
    assert build_preview(req, remote=remote).to_dict() == {
        "kind": "calendar_update",
        "title": "Update event: Standup",
        "fields": [],
        "diff": [
            {"field": "When", "old": _WHEN_10, "new": _WHEN_11},
            {"field": "Location", "old": "Room 4", "new": "Room 7"},
        ],
        "notes": ["Attendees will be notified by email (sendUpdates=all)."],
        "warnings": [],
    }


def test_calendar_update_attendee_diff_shows_added_and_removed() -> None:
    remote = {"summary": "Sync", "attendees": ["a@x.com", "b@x.com"]}
    req = CalendarUpdateRequest(
        event_id="e", attendees=("a@x.com", "c@x.com")  # drop b, add c
    )
    diff = dict((f, (o, n)) for f, o, n in build_preview(req, remote=remote).diff)
    assert "Attendees" in diff
    _old, new = diff["Attendees"]
    assert "+ c@x.com" in new and "- b@x.com" in new


def test_calendar_update_without_remote_shows_requested_only() -> None:
    req = CalendarUpdateRequest(event_id="e", location="Room 9")
    prev = build_preview(req, remote=None)
    assert any("Could not load the current event" in w for w in prev.warnings)
    assert ("Location", "Room 9") in prev.fields
    assert prev.diff == ()


def test_calendar_update_noop_is_flagged() -> None:
    remote = {"summary": "Sync", "location": "Room 4"}
    req = CalendarUpdateRequest(event_id="e", location="Room 4")  # same value
    prev = build_preview(req, remote=remote)
    assert prev.diff == ()
    assert "This update does not change anything." in prev.warnings


def test_calendar_cancel_notes_notification() -> None:
    prev = build_preview(CalendarCancelRequest(event_id="evt-9", summary="Old meeting"))
    assert prev.title == "Cancel event: Old meeting"
    assert prev.notes == ("Attendees will be notified by email (sendUpdates=all).",)


def test_doc_create_and_update() -> None:
    create = build_preview(DocCreateRequest(title="Spec", body="Hello world"))
    assert create.title == "Create doc: Spec"
    assert dict(create.fields)["Content"] == "Hello world"

    empty = build_preview(DocCreateRequest(title="  "))
    assert "The document has no title." in empty.warnings

    upd = build_preview(
        DocUpdateRequest(
            document_id="d1",
            title="Spec",
            operations=(DocReplaceOp(find="TODO", replace="Done"), DocAppendOp(text="Appendix")),
        )
    )
    values = [v for _, v in upd.fields]
    assert values == ["Replace 'TODO' with 'Done'", "Append: Appendix"]


def test_draft_create_is_drafts_only_and_warns() -> None:
    prev = build_preview(DraftCreateRequest(to="", subject="", body="hi"))
    assert "This creates a DRAFT only — Kira never sends mail." in prev.notes
    assert "The draft has no recipient." in prev.warnings
    assert "The draft has no subject." in prev.warnings


def test_draft_update_is_drafts_only() -> None:
    prev = build_preview(
        DraftUpdateRequest(draft_id="d1", to="bob@x.com", subject="Re: hi", body="updated")
    )
    assert prev.title == "Update draft: Re: hi"
    assert "This updates a DRAFT only — Kira never sends mail." in prev.notes


def test_timezone_is_resolved_for_offset_input() -> None:
    # An offset-bearing start is converted INTO the request timezone (15:00Z == 10:00 EST).
    req = CalendarCreateRequest(
        summary="Call",
        start="2026-02-01T15:00:00+00:00",
        end="2026-02-01T15:30:00+00:00",
        timezone=_TZ,
    )
    assert dict(build_preview(req).fields)["When"] == (
        "Sun, Feb 1 2026 · 10:00 AM – 10:30 AM (America/New_York)"
    )


def test_build_preview_rejects_unknown_request() -> None:
    with pytest.raises(TypeError):
        build_preview(object())  # type: ignore[arg-type]
