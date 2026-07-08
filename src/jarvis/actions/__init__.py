"""Action Connectors (Phase 12): the two-phase outward-write substrate.

Every outward write (Calendar / Drive-Docs create/update/cancel, Gmail draft) is a
:class:`WriteIntent` that moves draft → previewed → approved → executed, with a faithful
preview in between and a metadata-only journal row after. The model only ever *proposes* an
intent and *approves/rejects* a stored one; it can never forge the payload that actually
executes (the executor reads ``request`` off the stored intent, which is immutable after the
draft). This package is the mechanism; the Gate/turn routes and tools (Milestone 2) are the
policy that reaches it.

Milestone 1 ships the keyless, inert core: the intent state machine + store
(:mod:`jarvis.actions.intents`) and the journal (:mod:`jarvis.actions.journal`). No tool, no
HTTP route, no OAuth scope change — nothing here can touch a live account yet.
"""

from __future__ import annotations

from jarvis.actions.intents import (
    ALLOWED_TRANSITIONS,
    IntentKind,
    IntentState,
    IntentStore,
    InvalidTransition,
    WriteIntent,
)
from jarvis.actions.journal import ConnectorWrite, ConnectorWriteJournal
from jarvis.actions.preview import Preview, build_preview
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
from jarvis.actions.resolve import (
    AttendeeResolution,
    AttendeesUnresolved,
    require_resolved,
    resolve_attendees,
)

__all__ = [
    "ALLOWED_TRANSITIONS",
    "AttendeeResolution",
    "AttendeesUnresolved",
    "CalendarCancelRequest",
    "CalendarCreateRequest",
    "CalendarUpdateRequest",
    "ConnectorWrite",
    "ConnectorWriteJournal",
    "DocAppendOp",
    "DocCreateRequest",
    "DocReplaceOp",
    "DocUpdateRequest",
    "DraftCreateRequest",
    "DraftUpdateRequest",
    "IntentKind",
    "IntentState",
    "IntentStore",
    "InvalidTransition",
    "Preview",
    "WriteIntent",
    "WriteRequest",
    "build_preview",
    "require_resolved",
    "resolve_attendees",
]
