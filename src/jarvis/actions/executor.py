"""WriteExecutor: run an APPROVED WriteIntent, journal it, register the artifact, support undo.

This is the ONLY place an outward connector write actually happens, and it is reached ONLY from
the human-approved execute route (never a model tool). It runs the request stored on the intent —
``request_from_dict(intent.request)`` — so what executes is exactly what the human previewed; the
model cannot forge a different payload at execute time.

Safety properties (pinned):
* **Executed == previewed.** The stored request is the single source of truth for the write.
* **Idempotent.** ``mark_executed`` is a no-op on an already-executed intent (Task 1), so a
  replayed approve cannot double-write; the Meet requestId (calendar) is the intent key, so a
  provider-side retry cannot mint a second conference.
* **Metadata-only journal.** ``connector_writes`` gets verb / scope / remote-id / rollback handle
  / status — never the event body, attendees, or doc content (those live on the intent).
* **Fail-soft artifacts.** Registering the output as an artifact never raises out of execute.

Undo (where the API allows): a calendar create is undone by cancelling the event; a Doc create by
trashing the file. Update/cancel undo restores the prior event captured before the write (stored on
the intent's result, not the metadata journal). Doc-content undo is not offered (revision restore
is out of scope) — the journal records ``rollback_kind='none'``.
"""

from __future__ import annotations

from typing import Any

from jarvis.actions.intents import IntentState, IntentStore, WriteIntent
from jarvis.actions.journal import ConnectorWriteJournal
from jarvis.actions.requests import (
    CalendarCancelRequest,
    CalendarCreateRequest,
    CalendarUpdateRequest,
    DocAppendOp,
    DocCreateRequest,
    DocReplaceOp,
    DocUpdateRequest,
    request_from_dict,
)
from jarvis.connectors.base import ConnectorError
from jarvis.connectors.google import calendar as cal
from jarvis.connectors.google import docs, drive
from jarvis.observability import get_logger, log_egress

_BASE = "https://www.googleapis.com/auth"
# The OAuth scope each verb exercises (journalled for the audit trail).
_SCOPE = {
    "calendar_create": f"{_BASE}/calendar.events",
    "calendar_update": f"{_BASE}/calendar.events",
    "calendar_cancel": f"{_BASE}/calendar.events",
    "doc_create": f"{_BASE}/drive.file",
    "doc_update": f"{_BASE}/drive.file",
}
_EGRESS = {
    "calendar_create": "calendar_write",
    "calendar_update": "calendar_write",
    "calendar_cancel": "calendar_write",
    "doc_create": "drive_write",
    "doc_update": "drive_write",
}


def _time_value(field: dict) -> str:
    """The dateTime (timed) or date (all-day) out of a Google start/end object."""
    return field.get("dateTime") or field.get("date") or ""


class WriteExecutor:
    """Executes / undoes an approved intent against the Google client. ``client`` may be None
    (not connected) — execute then fails the intent with a friendly reconnect message, never a
    live write."""

    def __init__(
        self,
        client: Any,
        intents: IntentStore,
        journal: ConnectorWriteJournal,
        *,
        artifacts: Any = None,
        log: Any = None,
    ) -> None:
        self.client = client
        self.intents = intents
        self.journal = journal
        self.artifacts = artifacts
        self.log = log or get_logger("jarvis.actions.executor")

    # --- execute -----------------------------------------------------------

    async def execute(self, intent_id: int) -> WriteIntent:
        """Run an APPROVED intent. Idempotent (an already-executed intent returns unchanged).
        Records a metadata-only journal row + registers an artifact on success; marks the intent
        failed (no journal 'executed') on a connector error."""
        intent = await self.intents.get(intent_id)
        if intent is None:
            raise KeyError(f"no write intent with id {intent_id}")
        if intent.state is IntentState.EXECUTED:
            return intent  # idempotent: already done
        if intent.state is not IntentState.APPROVED:
            raise ValueError(f"intent {intent_id} is {intent.state.value}, not approved")
        if self.client is None:
            return await self.intents.mark_failed(
                intent_id, error="Google is not connected — use `uv run kira connect google`."
            )
        request = request_from_dict(intent.request)
        try:
            result, rollback = await self._run(request, intent)
        except ConnectorError as exc:
            await self.journal.record(
                provider="google", verb=intent.kind, status="failed",
                intent_id=intent_id, project_id=intent.project_id, scope=_SCOPE.get(intent.kind),
            )
            self.log.warning("connector_write_failed", verb=intent.kind, intent_id=intent_id)
            return await self.intents.mark_failed(intent_id, error=exc.user_message)
        log_egress(category=_EGRESS.get(intent.kind, "calendar_write"), destination_type="google")
        await self.journal.record(
            provider="google", verb=intent.kind, status="executed", intent_id=intent_id,
            project_id=intent.project_id, scope=_SCOPE.get(intent.kind),
            remote_id=result.get("remote_id"), rollback_kind=rollback[0], rollback_ref=rollback[1],
            egress_ref=_EGRESS.get(intent.kind),
        )
        await self._register_artifact(intent, result)
        return await self.intents.mark_executed(intent_id, result=result)

    async def _run(self, request: Any, intent: WriteIntent) -> tuple[dict, tuple[str, str | None]]:
        """Dispatch to the adapter; return (result_meta, (rollback_kind, rollback_ref)). The
        result stores enough to journal + undo — including, for update/cancel, the PRIOR event
        (content lives on the intent's result, never the metadata journal)."""
        c = self.client
        if isinstance(request, CalendarCreateRequest):
            created = await cal.create_event(
                c, summary=request.summary, start=request.start, end=request.end,
                timezone=request.timezone, attendees=request.attendees, location=request.location,
                description=request.description, recurrence=request.recurrence,
                all_day=request.all_day, add_meet=request.add_meet,
                meet_request_id=intent.idempotency_key, send_updates=request.send_updates,
                calendar_id=request.calendar_id,
            )
            rid = created.get("id", "")
            return (
                {"remote_id": rid, "link": created.get("htmlLink", ""),
                 "meet": created.get("hangoutLink", "")},
                ("cancel_event", rid),
            )
        if isinstance(request, CalendarUpdateRequest):
            prior = await cal.get_event(c, request.event_id, calendar_id=request.calendar_id)
            updated = await cal.update_event(
                c, request.event_id, timezone=request.timezone, summary=request.summary,
                start=request.start, end=request.end, attendees=request.attendees,
                location=request.location, description=request.description,
                all_day=request.all_day, send_updates=request.send_updates,
                calendar_id=request.calendar_id,
            )
            return (
                {
                    "remote_id": request.event_id,
                    "link": updated.get("htmlLink", ""),
                    "prior": prior,
                },
                ("restore_event", request.event_id),
            )
        if isinstance(request, CalendarCancelRequest):
            prior = await cal.get_event(c, request.event_id, calendar_id=request.calendar_id)
            await cal.cancel_event(
                c, request.event_id, send_updates=request.send_updates,
                calendar_id=request.calendar_id,
            )
            return (
                {"remote_id": request.event_id, "prior": prior},
                ("reinsert_event", request.event_id),
            )
        if isinstance(request, DocCreateRequest):
            created = await docs.create_document(c, title=request.title)
            doc_id = created.get("documentId", "")
            if request.body:
                await docs.batch_update(c, doc_id, [docs.append_text_request(request.body)])
            return (
                {"remote_id": doc_id, "link": f"https://docs.google.com/document/d/{doc_id}/edit"},
                ("trash_file", doc_id),
            )
        if isinstance(request, DocUpdateRequest):
            reqs: list[dict] = []
            for op in request.operations:
                if isinstance(op, DocReplaceOp):
                    reqs.append(docs.replace_all_text_request(op.find, op.replace))
                elif isinstance(op, DocAppendOp):
                    reqs.append(docs.append_text_request(op.text))
                else:  # pragma: no cover - DocUpdateRequest's typed tuple closes this union.
                    raise TypeError(f"unsupported document operation {type(op).__name__}")
            await docs.batch_update(c, request.document_id, reqs)
            return ({"remote_id": request.document_id}, ("none", None))
        raise TypeError(f"no executor for {type(request).__name__}")

    # --- undo --------------------------------------------------------------

    async def undo(self, intent_id: int) -> WriteIntent:
        """Reverse an executed write where the API allows. Marks the intent undone and journals
        the reversal; a not-undoable write raises ValueError (surfaced as a friendly error)."""
        intent = await self.intents.get(intent_id)
        if intent is None:
            raise KeyError(f"no write intent with id {intent_id}")
        if intent.state is not IntentState.EXECUTED:
            raise ValueError(f"intent {intent_id} is {intent.state.value}, not executed")
        if self.client is None:
            raise ValueError("Google is not connected.")
        result = intent.result or {}
        kind = request_from_dict(intent.request).kind.value
        await self._reverse(request_from_dict(intent.request), result)
        await self.journal.record(
            provider="google", verb=intent.kind, status="undone", intent_id=intent_id,
            project_id=intent.project_id, scope=_SCOPE.get(kind), remote_id=result.get("remote_id"),
            egress_ref=_EGRESS.get(kind),
        )
        return await self.intents.mark_undone(intent_id)

    async def _reverse(self, request: Any, result: dict) -> None:
        c = self.client
        rid = result.get("remote_id")
        if isinstance(request, CalendarCreateRequest) and rid:
            await cal.cancel_event(c, rid, send_updates=request.send_updates)
        elif isinstance(request, DocCreateRequest) and rid:
            await drive.trash_file(c, rid)
        elif isinstance(request, CalendarCancelRequest):
            prior = result.get("prior") or {}
            start = prior.get("start") or {}
            await cal.create_event(
                c,
                summary=prior.get("summary", ""),
                start=_time_value(start),
                end=_time_value(prior.get("end") or {}),
                timezone=start.get("timeZone") or "UTC",
                all_day="date" in start,
            )
        else:
            raise ValueError("Undo is not available for this write type.")

    async def _register_artifact(self, intent: WriteIntent, result: dict) -> None:
        """Register the write's output as an artifact (fail-soft — never breaks the write)."""
        if self.artifacts is None:
            return
        kinds = {"calendar_create": "calendar_event", "calendar_update": "calendar_event",
                 "calendar_cancel": "calendar_event", "doc_create": "doc", "doc_update": "doc"}
        title = (intent.preview or {}).get("title") or intent.summary
        link = result.get("link") or f"kira://write/{intent.id}"
        try:
            await self.artifacts.register(
                origin_type="connector_write",
                origin_id=str(intent.id),
                kind=kinds.get(intent.kind, "connector_write"),
                title=title,
                created_by="agent",
                external_uri=link,
                project_id=intent.project_id,
            )
        except Exception as exc:  # noqa: BLE001 — bookkeeping must never fail the write
            self.log.warning("connector_write_artifact_failed", intent_id=intent.id, error=str(exc))
