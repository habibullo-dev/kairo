"""Connector WRITE tools (Phase 12 Task 7): propose-only, gated, no live write. Keyless.

Pins the safety contract for the write PROPOSE tools: exact OAuth scopes (no broad Drive, no Gmail
send); ASK + egress; in AUTO_NEVER + the UnattendedGate hard-deny; out of PLAN_SAFE; registered
only when a Google client AND the intent store are present; run() only PROPOSES a previewed intent
(never executes a write); and an ambiguous attendee blocks before any intent is created.
"""

from __future__ import annotations

from pathlib import Path

from kira.actions.intents import IntentState, IntentStore
from kira.connectors.base import ConnectorRegistry
from kira.connectors.google import GOOGLE_SCOPES
from kira.permissions.modes import AUTO_NEVER, PLAN_SAFE
from kira.permissions.unattended import HARD_DENY
from kira.persistence.db import connect
from kira.tools.base import Permission, ToolContext
from kira.tools.builtin.connectors_write import (
    WRITE_TOOL_NAMES,
    CalendarCreateEventTool,
    CalendarCreateParams,
    CalendarUpdateEventTool,
    CalendarUpdateParams,
    DriveCreateDocTool,
    DriveUpdateDocTool,
)

_BASE = "https://www.googleapis.com/auth"
_ALL_WRITE_TOOLS = (
    CalendarCreateEventTool,
    CalendarUpdateEventTool,
    DriveCreateDocTool,
    DriveUpdateDocTool,
)


class _FakeGoogle:
    """Minimal Google client for propose tests: only a canned get_event read (for the update
    diff). It has NO write method, so a propose that tried to write would AttributeError — the
    structural proof that proposing never writes."""

    def __init__(self, event: dict | None = None) -> None:
        self._event = event or {}

    async def get_json(self, url: str, *, params: dict | None = None) -> dict:
        return self._event


async def _store(tmp_path: Path) -> IntentStore:
    db = await connect(tmp_path / "wt.db")
    return IntentStore(db)


def _ctx(store: IntentStore | None, google: object | None) -> ToolContext:
    connectors = ConnectorRegistry(google=google) if google is not None else None
    return ToolContext(connectors=connectors, intents=store)


# --- scopes ---------------------------------------------------------------


def test_oauth_scopes_are_exactly_the_implemented_set() -> None:
    assert (
        f"{_BASE}/calendar.readonly",
        f"{_BASE}/calendar.events",
        f"{_BASE}/gmail.readonly",
        f"{_BASE}/gmail.compose",
        f"{_BASE}/drive.readonly",
        f"{_BASE}/drive.file",
    ) == GOOGLE_SCOPES
    # No broad Drive scope, no documents scope, no Gmail send/modify.
    assert f"{_BASE}/drive" not in GOOGLE_SCOPES
    assert not any(s.endswith("/documents") for s in GOOGLE_SCOPES)
    assert not any("gmail.send" in s or "gmail.modify" in s for s in GOOGLE_SCOPES)


# --- permission matrix ----------------------------------------------------


def test_write_tools_are_ask_and_egress() -> None:
    for cls in _ALL_WRITE_TOOLS + (CalendarCreateEventTool,):
        assert cls.permission_default is Permission.ASK
        assert cls.egress is True


def test_write_tools_are_auto_never_and_unattended_hard_deny() -> None:
    assert WRITE_TOOL_NAMES <= AUTO_NEVER  # Auto never auto-approves an outward write
    assert WRITE_TOOL_NAMES <= HARD_DENY  # unattended can neither propose nor execute one


def test_write_tools_are_not_plan_safe() -> None:
    assert not (WRITE_TOOL_NAMES & PLAN_SAFE)  # Plan mode denies proposing a write


def test_write_tool_names_match_registered_tools() -> None:
    assert {cls.name for cls in _ALL_WRITE_TOOLS} | {"calendar_cancel_event"} == WRITE_TOOL_NAMES


# --- availability ---------------------------------------------------------


async def test_unavailable_without_client_or_intents(tmp_path: Path) -> None:
    store = await _store(tmp_path)
    assert not CalendarCreateEventTool.is_available(ToolContext())  # nothing
    assert not CalendarCreateEventTool.is_available(_ctx(store, None))  # no google
    assert not CalendarCreateEventTool.is_available(_ctx(None, _FakeGoogle()))  # no intents
    assert CalendarCreateEventTool.is_available(_ctx(store, _FakeGoogle()))  # both


# --- propose (never write) ------------------------------------------------


async def test_calendar_create_proposes_a_previewed_intent(tmp_path: Path) -> None:
    store = await _store(tmp_path)
    tool = CalendarCreateEventTool(_ctx(store, _FakeGoogle()))
    result = await tool.run(
        CalendarCreateParams(
            summary="Standup",
            start="2026-02-01T10:00:00",
            end="2026-02-01T10:15:00",
            timezone="America/New_York",
            attendees=["alice@example.com"],
            add_meet=True,
        )
    )
    assert "Queued write intent" in result and "will NOT execute" in result.replace("\n", " ")
    previewed = await store.list(state=IntentState.PREVIEWED)
    assert len(previewed) == 1
    intent = previewed[0]
    assert intent.kind == "calendar_create"
    assert intent.state is IntentState.PREVIEWED  # proposed, NOT executed — no write happened
    assert intent.request["summary"] == "Standup"
    assert intent.request["attendees"] == ["alice@example.com"]
    assert intent.preview["title"] == "Create event: Standup"


async def test_ambiguous_attendee_blocks_before_any_intent(tmp_path: Path) -> None:
    store = await _store(tmp_path)
    tool = CalendarCreateEventTool(_ctx(store, _FakeGoogle()))
    result = await tool.run(
        CalendarCreateParams(
            summary="Standup",
            start="2026-02-01T10:00:00",
            end="2026-02-01T10:15:00",
            timezone="America/New_York",
            attendees=["alice@example.com", "Bob"],  # Bob is ambiguous
        )
    )
    assert "email address" in result and "Bob" in result  # clarification asked
    assert await store.list() == []  # NO intent created — never reached the preview stage


async def test_propose_is_idempotent(tmp_path: Path) -> None:
    store = await _store(tmp_path)
    tool = CalendarCreateEventTool(_ctx(store, _FakeGoogle()))
    params = CalendarCreateParams(
        summary="Standup", start="2026-02-01T10:00:00", end="2026-02-01T10:15:00",
        timezone="America/New_York",
    )
    await tool.run(params)
    await tool.run(params)  # identical re-proposal
    assert len(await store.list()) == 1  # deduped — one queued intent


async def test_calendar_update_proposes_with_a_diff(tmp_path: Path) -> None:
    store = await _store(tmp_path)
    remote_event = {
        "summary": "Standup",
        "start": {"dateTime": "2026-02-01T10:00:00", "timeZone": "America/New_York"},
        "end": {"dateTime": "2026-02-01T10:15:00", "timeZone": "America/New_York"},
        "location": "Room 4",
    }
    tool = CalendarUpdateEventTool(_ctx(store, _FakeGoogle(remote_event)))
    result = await tool.run(CalendarUpdateParams(event_id="evt-1", location="Room 7"))
    assert "Queued write intent" in result
    intent = (await store.list(state=IntentState.PREVIEWED))[0]
    assert intent.kind == "calendar_update"
    diff_fields = {row["field"] for row in intent.preview["diff"]}
    assert "Location" in diff_fields  # the diff was built against the fetched remote event


async def test_doc_update_requires_operations(tmp_path: Path) -> None:
    store = await _store(tmp_path)
    tool = DriveUpdateDocTool(_ctx(store, _FakeGoogle()))
    from kira.tools.builtin.connectors_write import DocUpdateParams

    result = await tool.run(DocUpdateParams(document_id="d1"))  # no append, no replacements
    assert result.is_error and await store.list() == []
