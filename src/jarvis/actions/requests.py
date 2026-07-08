"""Typed write-request shapes: Kairo's internal representation of one proposed outward write.

These are deliberately provider-agnostic value objects. The preview builder
(:mod:`jarvis.actions.preview`) renders them for human approval; the executor (Milestone 2)
translates them into the Google API payload. Keeping the request separate from the API payload is
what lets the preview be rendered — and pinned — without any network, and is half of the
"executed == previewed" guarantee (both derive from this one stored object).

Update requests use ``None`` to mean "leave this field unchanged" (partial/PATCH semantics), so a
"move the meeting an hour later" request carries only the new ``start``/``end`` and the diff shows
exactly those two fields changing.

``send_updates`` mirrors the Google Calendar ``sendUpdates`` parameter (none | all | externalOnly)
— surfaced in every calendar preview so the human knows whether guests get an email.
"""

from __future__ import annotations

from dataclasses import dataclass

from jarvis.actions.intents import IntentKind

SendUpdates = str  # "none" | "all" | "externalOnly"


@dataclass(frozen=True)
class CalendarCreateRequest:
    summary: str
    start: str  # RFC3339 datetime (naive → interpreted in `timezone`) or a date for all-day
    end: str
    timezone: str  # IANA name, e.g. "America/New_York"
    attendees: tuple[str, ...] = ()  # resolved email addresses (see actions.resolve)
    location: str = ""
    description: str = ""
    recurrence: tuple[str, ...] = ()  # RRULE lines, e.g. ("RRULE:FREQ=WEEKLY",)
    add_meet: bool = False  # attach a Google Meet link (conferenceData.createRequest)
    send_updates: SendUpdates = "none"
    all_day: bool = False
    calendar_id: str = "primary"

    @property
    def kind(self) -> IntentKind:
        return IntentKind.CALENDAR_CREATE


@dataclass(frozen=True)
class CalendarUpdateRequest:
    event_id: str
    # Context for interpreting a changed start/end, not itself a change; falls back to the
    # event's current zone when a time is being changed. None ⇒ "not changing the time".
    timezone: str | None = None
    summary: str | None = None
    start: str | None = None
    end: str | None = None
    attendees: tuple[str, ...] | None = None
    location: str | None = None
    description: str | None = None
    recurrence: tuple[str, ...] | None = None
    add_meet: bool | None = None
    all_day: bool | None = None
    send_updates: SendUpdates = "none"
    calendar_id: str = "primary"

    @property
    def kind(self) -> IntentKind:
        return IntentKind.CALENDAR_UPDATE


@dataclass(frozen=True)
class CalendarCancelRequest:
    event_id: str
    # A short human title for the event being cancelled (for the preview label only — the
    # execute path targets the event by id, never by title).
    summary: str = ""
    send_updates: SendUpdates = "all"  # cancels default to notifying guests
    calendar_id: str = "primary"

    @property
    def kind(self) -> IntentKind:
        return IntentKind.CALENDAR_CANCEL


@dataclass(frozen=True)
class DocCreateRequest:
    title: str
    body: str = ""  # initial plain-text content

    @property
    def kind(self) -> IntentKind:
        return IntentKind.DOC_CREATE


@dataclass(frozen=True)
class DocReplaceOp:
    """Replace every occurrence of ``find`` with ``replace`` (Docs replaceAllText)."""

    find: str
    replace: str


@dataclass(frozen=True)
class DocAppendOp:
    """Append ``text`` to the end of the document (Docs insertText at end-of-body)."""

    text: str


DocOp = DocReplaceOp | DocAppendOp


@dataclass(frozen=True)
class DocUpdateRequest:
    document_id: str
    title: str = ""  # for the preview label only
    operations: tuple[DocOp, ...] = ()

    @property
    def kind(self) -> IntentKind:
        return IntentKind.DOC_UPDATE


@dataclass(frozen=True)
class DraftCreateRequest:
    to: str
    subject: str
    body: str
    reply_to_message_id: str | None = None

    @property
    def kind(self) -> IntentKind:
        return IntentKind.GMAIL_DRAFT_CREATE


@dataclass(frozen=True)
class DraftUpdateRequest:
    draft_id: str
    to: str
    subject: str
    body: str

    @property
    def kind(self) -> IntentKind:
        return IntentKind.GMAIL_DRAFT_UPDATE


WriteRequest = (
    CalendarCreateRequest
    | CalendarUpdateRequest
    | CalendarCancelRequest
    | DocCreateRequest
    | DocUpdateRequest
    | DraftCreateRequest
    | DraftUpdateRequest
)

__all__ = [
    "CalendarCancelRequest",
    "CalendarCreateRequest",
    "CalendarUpdateRequest",
    "DocAppendOp",
    "DocCreateRequest",
    "DocOp",
    "DocReplaceOp",
    "DocUpdateRequest",
    "DraftCreateRequest",
    "DraftUpdateRequest",
    "SendUpdates",
    "WriteRequest",
]
