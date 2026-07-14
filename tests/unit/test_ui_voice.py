"""Voice on the UI (Phase 8, Task 6) — the fail-closed screen + meeting capture.

The non-negotiable: the workstation is voice's *screen*, and "screen available" is a
POSITIVE, live, modal-bound check (ADR-0008 §5). A spoken "yes" has no path to approve — the
VoiceApprover escalates to the UIScreenApprover, which resolves only via an authenticated
click (nonce), denies when no live Gate surface is watching, and fail-closes (DENY) if the
surface vanishes mid-confirmation. Meetings land UNREVIEWED, never an auto-action.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from jarvis.core.client import ToolCall
from jarvis.core.execution import ExecutionContext, bind_execution_context
from jarvis.permissions.gate import Decision
from jarvis.tools import Permission
from jarvis.ui.approver import ApprovalManager, UIScreenApprover
from jarvis.ui.connections import ConnectionManager
from jarvis.ui.voice import UiVoice
from jarvis.voice import FakeCapture, FakeTranscriber, MeetingCapture, VoiceApprover

ASK = Decision(Permission.ASK, "risky")
_CONTEXT = ExecutionContext(session_id=101, project_id=None)


class _FakeWS:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send_json(self, m: dict) -> None:
        self.sent.append(m)


def _live_gate_conn(cm: ConnectionManager):
    """A live connection with the Gate surface mounted — the only 'available' screen."""
    conn = cm.register(_FakeWS(), owner_session="test")
    cm.bind_workspace(
        conn,
        owner_session="test",
        workspace_id="w" * 24,
        context=_CONTEXT,
    )
    cm.set_surface(conn, "gate", mounted=True)
    return conn


def _in_context(awaitable):
    with bind_execution_context(_CONTEXT):
        return asyncio.create_task(awaitable)


def _available(screen: UIScreenApprover) -> bool:
    with bind_execution_context(_CONTEXT):
        return screen.available()


def _call() -> ToolCall:
    return ToolCall("c1", "run_shell", {"command": "dropdb production"})


# --- available(): positive, live, mounted-surface only ----------------------


def test_available_requires_live_and_mounted_gate_surface() -> None:
    cm = ConnectionManager(heartbeat_seconds=15.0, clock=lambda: 0.0)
    screen = UIScreenApprover(ApprovalManager(cm), cm)
    assert _available(screen) is False  # no clients at all
    conn = cm.register(_FakeWS(), owner_session="test")
    cm.bind_workspace(
        conn,
        owner_session="test",
        workspace_id="w" * 24,
        context=_CONTEXT,
    )
    assert (
        _available(screen) is False
    )  # connected but no Gate surface mounted (hello-claim ≠ watching)
    cm.set_surface(conn, "daily", mounted=True)
    assert _available(screen) is False  # a different surface doesn't count
    cm.set_surface(conn, "gate", mounted=True)
    assert _available(screen) is True  # live + Gate mounted ⇒ available


def test_available_false_when_client_heartbeat_stale() -> None:
    now = [0.0]
    cm = ConnectionManager(heartbeat_seconds=10.0, clock=lambda: now[0])
    screen = UIScreenApprover(ApprovalManager(cm), cm)
    _live_gate_conn(cm)
    assert _available(screen) is True
    now[0] = 30.0  # heartbeat goes stale ⇒ not a watching screen
    assert _available(screen) is False


# --- the full VoiceApprover path: no voice-only approval --------------------


async def test_no_screen_means_voice_denies() -> None:
    # VoiceApprover escalates every ASK to the screen; with no live Gate surface the screen
    # is unavailable ⇒ DENY. A spoken "yes" cannot substitute for a screen.
    cm = ConnectionManager(heartbeat_seconds=15.0, clock=lambda: 0.0)
    approver = VoiceApprover(UIScreenApprover(ApprovalManager(cm), cm))
    with bind_execution_context(_CONTEXT):
        assert await approver(_call(), ASK) is Permission.DENY


async def test_spoken_yes_cannot_approve_only_the_screen_click_can() -> None:
    cm = ConnectionManager(heartbeat_seconds=15.0, clock=lambda: 0.0)
    approvals = ApprovalManager(cm)
    conn = _live_gate_conn(cm)
    approver = VoiceApprover(UIScreenApprover(approvals, cm))
    task = _in_context(approver(_call(), ASK))
    await asyncio.sleep(0)
    (pending,) = approvals.pending()  # escalated to the screen; awaiting a click
    assert not task.done()  # NO spoken-yes shortcut — it waits for the screen
    assert pending.kind == "voice"
    # the human at the screen declines → DENY (the screen governs, per-instance)
    nonce = await approvals.mint_nonce(pending.decision_id, conn)
    approvals.resolve(pending.decision_id, nonce, "deny")
    assert await task is Permission.DENY


async def test_screen_click_approves_through_the_full_path() -> None:
    cm = ConnectionManager(heartbeat_seconds=15.0, clock=lambda: 0.0)
    approvals = ApprovalManager(cm)
    conn = _live_gate_conn(cm)
    approver = VoiceApprover(UIScreenApprover(approvals, cm))
    task = _in_context(approver(_call(), ASK))
    await asyncio.sleep(0)
    (pending,) = approvals.pending()
    nonce = await approvals.mint_nonce(pending.decision_id, conn)
    approvals.resolve(pending.decision_id, nonce, "approve")
    assert await task is Permission.ALLOW  # the authenticated click committed it


async def test_fail_closed_when_surface_vanishes_mid_confirm() -> None:
    # The client is watching (available), the voice action escalates, then the surface goes
    # away before a click — the decision must resolve DENY, never hang.
    cm = ConnectionManager(heartbeat_seconds=15.0, clock=lambda: 0.0)
    approvals = ApprovalManager(cm)
    conn = _live_gate_conn(cm)
    approver = VoiceApprover(UIScreenApprover(approvals, cm))
    task = _in_context(approver(_call(), ASK))
    await asyncio.sleep(0)
    assert approvals.pending()  # escalated, awaiting the screen
    cm.drop(conn)  # the watching client disconnects mid-confirmation
    assert await asyncio.wait_for(task, timeout=1.0) is Permission.DENY


async def test_on_escalate_announcement_is_the_safe_line_not_the_input() -> None:
    # The renderer's announcement must never carry the payload (TTS-privacy). Here we just
    # assert the VoiceApprover invokes on_escalate before touching the screen.
    cm = ConnectionManager(heartbeat_seconds=15.0, clock=lambda: 0.0)
    announced: list = []

    def on_escalate(call, decision):
        announced.append((call.name, decision.reason))

    approver = VoiceApprover(UIScreenApprover(ApprovalManager(cm), cm), on_escalate=on_escalate)
    with bind_execution_context(_CONTEXT):
        await approver(_call(), ASK)  # no screen ⇒ denies, but announces first
    assert announced == [("run_shell", "risky")]  # name + reason, NOT the command payload


# --- UiVoice controller: meeting → unreviewed, status, push-to-talk ---------


class _FakeKnowledge:
    def __init__(self) -> None:
        self.bound_unattended = False
        self.ingested: list[dict] = []

    async def ingest(self, **kw):
        self.ingested.append(kw)
        review = "unreviewed" if self.bound_unattended or kw.get("quarantine") else "reviewed"
        return SimpleNamespace(
            action="ingested", source_id=1, chunks=1, review_status=review, title=kw.get("title")
        )


async def test_meeting_capture_lands_unreviewed(tmp_path: Path) -> None:
    knowledge = _FakeKnowledge()
    stt = FakeTranscriber(scripted=["Standup. Action item: grant Bob admin."])
    meeting = MeetingCapture(knowledge, stt)
    voice = UiVoice(meeting=meeting, capture=FakeCapture(scripted=[b"audio"]))
    result = await voice.capture_meeting(title="Standup")
    assert result is not None and result.review_status == "unreviewed"


async def test_meeting_capture_state_and_provenance_match_the_real_lifecycle() -> None:
    knowledge = _FakeKnowledge()
    states: list[str] = []
    meeting = MeetingCapture(
        knowledge,
        FakeTranscriber(scripted=["Standup notes"]),
        on_state=states.append,
    )
    seen_while_microphone_open: list[str] = []

    class _Capture:
        async def capture_utterance(self) -> bytes:
            seen_while_microphone_open.append(meeting.state)
            return b"audio"

    result = await UiVoice(meeting=meeting, capture=_Capture()).capture_meeting(
        title="Standup",
        project_id=7,
        source_session_id=42,
    )

    assert result is not None
    assert seen_while_microphone_open == ["recording"]
    assert states == ["recording", "transcribing", "saving", "idle"]
    assert knowledge.ingested[0]["project_id"] == 7
    assert knowledge.ingested[0]["source_session_id"] == 42
    assert knowledge.ingested[0]["origin_override"].startswith("meeting-capture:")


def test_meeting_availability_is_separate_from_generic_voice() -> None:
    class _Listener:
        pass

    voice = UiVoice(listener=_Listener())
    assert voice.status()["enabled"] is True
    assert voice.status()["meeting_available"] is False
    assert voice.status()["meeting_reason"]


async def test_status_and_listen_delegate() -> None:
    heard: list[bool] = []

    class _Listener:
        async def listen_once(self):
            heard.append(True)
            return object()  # a turn ran

    voice = UiVoice(listener=_Listener())
    assert voice.status()["enabled"] is True
    assert await voice.listen_once() is True
    assert heard == [True]
    assert voice.state == "idle"  # returns to idle after the activation


def test_status_disabled_when_unwired() -> None:
    voice = UiVoice()
    assert voice.status()["enabled"] is False
