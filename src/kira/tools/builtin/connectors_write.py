"""Connector WRITE tools (Phase 12): calendar + Drive/Docs writes as two-phase proposals.

These tools NEVER execute an outward write. Each ``run()`` only PROPOSES: it resolves attendees
(returning a clarification and creating no intent if any are ambiguous), builds a typed request +
a faithful preview, and persists a ``previewed`` WriteIntent for human approval. The actual write
happens ONLY via the human-approved execute route off the approval queue, which runs the STORED
request — so what executes equals what was previewed, and no model/auto/unattended path can write.

Permissions (pinned): ``permission_default = ASK`` + ``egress = True`` (so a private-read-then-
propose turn demotes to a non-persistable ASK), kept OUT of ``PLAN_SAFE`` (Plan denies proposing),
added to Auto's ``AUTO_NEVER`` and the ``UnattendedGate`` hard-deny (no auto/headless proposing in
Phase 12). Tools register only when BOTH a Google client and the intent store are present.

Gmail DRAFTS are handled separately (one-phase, in connectors_google.py): a draft is never sent,
so it does not need the outward-write queue.
"""

from __future__ import annotations

import hashlib
import json

from pydantic import BaseModel, Field
from tzlocal import get_localzone_name

from kira.actions.intents import IntentState
from kira.actions.preview import build_preview
from kira.actions.requests import (
    CalendarCancelRequest,
    CalendarCreateRequest,
    CalendarUpdateRequest,
    DocAppendOp,
    DocCreateRequest,
    DocReplaceOp,
    DocUpdateRequest,
    WriteRequest,
    request_to_dict,
)
from kira.actions.resolve import AttendeesUnresolved, require_resolved
from kira.connectors.base import ConnectorError
from kira.connectors.google import calendar as cal
from kira.tools.base import Permission, Tool, ToolContext, ToolResult

#: Every connector-write PROPOSE tool. Pinned as the exact set added to AUTO_NEVER + the
#: UnattendedGate hard-deny, so a new write tool cannot be forgotten in the permission matrix.
WRITE_TOOL_NAMES: frozenset[str] = frozenset(
    {
        "calendar_create_event",
        "calendar_update_event",
        "calendar_cancel_event",
        "drive_create_doc",
        "drive_update_doc",
    }
)

_SEND_UPDATES = ("none", "all", "externalOnly")


def _writes_available(context: ToolContext) -> bool:
    """A write tool is available only when BOTH the Google client and the intent store exist —
    otherwise proposing a write would have nowhere to land."""
    connectors = getattr(context, "connectors", None)
    return (
        connectors is not None
        and getattr(connectors, "google", None) is not None
        and getattr(context, "intents", None) is not None
    )


def _project_id(context: ToolContext) -> int | None:
    project = getattr(context, "project", None)
    scope = project() if callable(project) else project
    return getattr(scope, "id", None)


def _default_tz() -> str:
    return get_localzone_name()


def _idem_key(data: dict) -> str:
    """A deterministic key for a proposed write, so a retried identical proposal dedupes to the
    same intent instead of queuing twice."""
    digest = hashlib.sha256(json.dumps(data, sort_keys=True).encode("utf-8")).hexdigest()
    return f"{data['kind']}:{digest[:20]}"


async def _propose(
    context: ToolContext, request: WriteRequest, *, remote: dict | None = None
) -> str:
    """Persist a previewed WriteIntent for ``request`` and return the human-facing queue note.
    Idempotent: a re-proposal of an already-handled write is reported, not re-queued."""
    preview = build_preview(request, remote=remote)
    data = request_to_dict(request)
    intents = context.intents
    intent_id = await intents.create_draft(
        idempotency_key=_idem_key(data),
        provider="google",
        kind=request.kind,
        request=data,
        summary=preview.title,
        source="agent",
        project_id=_project_id(context),
    )
    current = await intents.get(intent_id)
    if current is not None and current.state not in (IntentState.DRAFT, IntentState.PREVIEWED):
        return (
            f"This exact write was already handled (intent #{intent_id}, "
            f"state {current.state.value}); not re-queuing."
        )
    await intents.mark_previewed(intent_id, preview=preview.to_dict())
    lines = [f"Queued write intent #{intent_id} for your approval: {preview.title}."]
    if preview.warnings:
        lines.append("Please note: " + " ".join(preview.warnings))
    lines.append(
        "Open Approvals to review the full preview and approve it to send. I will NOT execute "
        "this write myself — nothing is sent until you approve it."
    )
    return "\n".join(lines)


def _normalize_event(raw: dict) -> dict:
    """A Google event resource → the ``remote`` snapshot build_preview diffs an update against."""
    start = raw.get("start", {}) or {}
    end = raw.get("end", {}) or {}
    all_day = "date" in start
    return {
        "summary": raw.get("summary", ""),
        "start": start.get("dateTime") or start.get("date") or "",
        "end": end.get("dateTime") or end.get("date") or "",
        "timezone": start.get("timeZone"),
        "location": raw.get("location", ""),
        "description": raw.get("description", ""),
        "attendees": [a.get("email", "") for a in (raw.get("attendees") or [])],
        "recurrence": list(raw.get("recurrence") or []),
        "all_day": all_day,
        "has_meet": bool(raw.get("conferenceData") or raw.get("hangoutLink")),
    }


# --- Calendar -------------------------------------------------------------


class CalendarCreateParams(BaseModel):
    summary: str = Field(description="Event title.")
    start: str = Field(description="Start (RFC3339 datetime, or YYYY-MM-DD when all_day).")
    end: str = Field(description="End (RFC3339 datetime, or the exclusive end date when all_day).")
    timezone: str | None = Field(default=None, description="IANA zone; defaults to local zone.")
    attendees: list[str] = Field(
        default_factory=list,
        description="Attendee EMAIL addresses. Bare names are refused — resolve to emails first.",
    )
    location: str = Field(default="")
    description: str = Field(default="")
    recurrence: list[str] = Field(default_factory=list, description="RRULE lines (FREQ=WEEKLY…).")
    add_meet: bool = Field(default=False, description="Attach a Google Meet video link.")
    all_day: bool = Field(default=False)
    send_updates: str = Field(default="all", description="none | all | externalOnly")


class CalendarCreateEventTool(Tool):
    name = "calendar_create_event"
    description = (
        "Propose creating a Google Calendar event (optionally with a Meet link). Does NOT create "
        "it — it queues a preview for your approval; you approve to send. Attendees must be email "
        "addresses; if a name is ambiguous, ask the user for the address first."
    )
    Params = CalendarCreateParams
    permission_default = Permission.ASK
    egress = True

    @classmethod
    def is_available(cls, context: ToolContext) -> bool:
        return _writes_available(context)

    async def run(self, params: CalendarCreateParams) -> ToolResult | str:
        try:
            attendees = require_resolved(params.attendees)
        except AttendeesUnresolved as exc:
            return str(exc)  # ask the user; NO intent is created for an ambiguous attendee
        send_updates = params.send_updates if params.send_updates in _SEND_UPDATES else "all"
        request = CalendarCreateRequest(
            summary=params.summary,
            start=params.start,
            end=params.end,
            timezone=params.timezone or _default_tz(),
            attendees=attendees,
            location=params.location,
            description=params.description,
            recurrence=tuple(params.recurrence),
            add_meet=params.add_meet,
            all_day=params.all_day,
            send_updates=send_updates,
        )
        return await _propose(self.context, request)


class CalendarUpdateParams(BaseModel):
    event_id: str = Field(description="The event id to update (from calendar_list_events).")
    summary: str | None = None
    start: str | None = None
    end: str | None = None
    timezone: str | None = None
    attendees: list[str] | None = Field(
        default=None, description="Replace the attendee list (email addresses)."
    )
    location: str | None = None
    description: str | None = None
    all_day: bool | None = None
    send_updates: str = Field(default="all", description="none | all | externalOnly")


class CalendarUpdateEventTool(Tool):
    name = "calendar_update_event"
    description = (
        "Propose updating an existing Google Calendar event (only the fields you set change). "
        "Does NOT change it — queues a preview with a field-level diff for your approval."
    )
    Params = CalendarUpdateParams
    permission_default = Permission.ASK
    egress = True

    @classmethod
    def is_available(cls, context: ToolContext) -> bool:
        return _writes_available(context)

    async def run(self, params: CalendarUpdateParams) -> ToolResult | str:
        attendees: tuple[str, ...] | None = None
        if params.attendees is not None:
            try:
                attendees = require_resolved(params.attendees)
            except AttendeesUnresolved as exc:
                return str(exc)
        send_updates = params.send_updates if params.send_updates in _SEND_UPDATES else "all"
        request = CalendarUpdateRequest(
            event_id=params.event_id,
            timezone=params.timezone,
            summary=params.summary,
            start=params.start,
            end=params.end,
            attendees=attendees,
            location=params.location,
            description=params.description,
            all_day=params.all_day,
            send_updates=send_updates,
        )
        remote: dict | None = None
        try:
            raw = await cal.get_event(self.context.connectors.google, params.event_id)
            remote = _normalize_event(raw)
        except ConnectorError:
            remote = None  # preview notes it couldn't load the current event
        return await _propose(self.context, request, remote=remote)


class CalendarCancelParams(BaseModel):
    event_id: str = Field(description="The event id to cancel (from calendar_list_events).")
    summary: str = Field(default="", description="A label for the event, for the preview only.")
    send_updates: str = Field(default="all", description="none | all | externalOnly")


class CalendarCancelEventTool(Tool):
    name = "calendar_cancel_event"
    description = (
        "Propose cancelling (deleting) a Google Calendar event. Does NOT cancel it — queues a "
        "preview for your approval; attendees are notified when you approve (undo re-creates it)."
    )
    Params = CalendarCancelParams
    permission_default = Permission.ASK
    egress = True

    @classmethod
    def is_available(cls, context: ToolContext) -> bool:
        return _writes_available(context)

    async def run(self, params: CalendarCancelParams) -> ToolResult | str:
        send_updates = params.send_updates if params.send_updates in _SEND_UPDATES else "all"
        request = CalendarCancelRequest(
            event_id=params.event_id, summary=params.summary, send_updates=send_updates
        )
        return await _propose(self.context, request)


# --- Drive / Docs ---------------------------------------------------------


class DocCreateParams(BaseModel):
    title: str = Field(description="Document title.")
    body: str = Field(default="", description="Initial plain-text content.")


class DriveCreateDocTool(Tool):
    name = "drive_create_doc"
    description = (
        "Propose creating a Google Doc (under the narrow drive.file scope). Does NOT create it — "
        "queues a preview for your approval; you approve to create it (undo trashes it)."
    )
    Params = DocCreateParams
    permission_default = Permission.ASK
    egress = True

    @classmethod
    def is_available(cls, context: ToolContext) -> bool:
        return _writes_available(context)

    async def run(self, params: DocCreateParams) -> ToolResult | str:
        request = DocCreateRequest(title=params.title, body=params.body)
        return await _propose(self.context, request)


class DocReplaceParam(BaseModel):
    find: str
    replace: str


class DocUpdateParams(BaseModel):
    document_id: str = Field(description="The Google Doc id to edit.")
    title: str = Field(default="", description="A label for the doc, for the preview only.")
    append: str | None = Field(default=None, description="Text to append at the end of the doc.")
    replacements: list[DocReplaceParam] = Field(
        default_factory=list, description="Find/replace edits applied to the whole document."
    )


class DriveUpdateDocTool(Tool):
    name = "drive_update_doc"
    description = (
        "Propose editing an existing Google Doc (append text and/or find-replace, under "
        "drive.file). Does NOT edit it — queues a preview of the edits for your approval."
    )
    Params = DocUpdateParams
    permission_default = Permission.ASK
    egress = True

    @classmethod
    def is_available(cls, context: ToolContext) -> bool:
        return _writes_available(context)

    async def run(self, params: DocUpdateParams) -> ToolResult | str:
        ops: list = [DocReplaceOp(find=r.find, replace=r.replace) for r in params.replacements]
        if params.append:
            ops.append(DocAppendOp(text=params.append))
        if not ops:
            return ToolResult(
                content="No edits specified — provide `append` text and/or `replacements`.",
                is_error=True,
            )
        request = DocUpdateRequest(
            document_id=params.document_id, title=params.title, operations=tuple(ops)
        )
        return await _propose(self.context, request)
