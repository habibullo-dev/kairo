"""Typed write-request shapes: Kira's internal representation of one proposed outward write.

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

import dataclasses
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

_REQUEST_CLASSES: dict[IntentKind, type] = {
    IntentKind.CALENDAR_CREATE: CalendarCreateRequest,
    IntentKind.CALENDAR_UPDATE: CalendarUpdateRequest,
    IntentKind.CALENDAR_CANCEL: CalendarCancelRequest,
    IntentKind.DOC_CREATE: DocCreateRequest,
    IntentKind.DOC_UPDATE: DocUpdateRequest,
    IntentKind.GMAIL_DRAFT_CREATE: DraftCreateRequest,
    IntentKind.GMAIL_DRAFT_UPDATE: DraftUpdateRequest,
}
_TUPLE_FIELDS = ("attendees", "recurrence", "operations")


def request_to_dict(request: WriteRequest) -> dict:
    """Serialize a request for storage on a WriteIntent. ``kind`` (a property) is added
    explicitly; the rest is the dataclass field dict (tuples serialize as lists via JSON)."""
    return {"kind": request.kind.value, **dataclasses.asdict(request)}


def _op_from_dict(op: dict) -> DocOp:
    return DocReplaceOp(**op) if "find" in op else DocAppendOp(**op)


def request_from_dict(data: dict) -> WriteRequest:
    """Reconstruct the exact request stored by :func:`request_to_dict` — the executor runs THIS,
    so what executes equals what was previewed. Lists round-trip back to the tuple fields."""
    cls = _REQUEST_CLASSES[IntentKind(data["kind"])]
    fields = {k: v for k, v in data.items() if k != "kind"}
    for name in _TUPLE_FIELDS:
        value = fields.get(name)
        if isinstance(value, list):
            fields[name] = (
                tuple(_op_from_dict(o) for o in value)
                if name == "operations"
                else tuple(value)
            )
    return cls(**fields)

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
    "request_from_dict",
    "request_to_dict",
]
