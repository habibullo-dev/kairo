"""Voice controller for the workstation (Phase 8, Task 6).

A thin controller over the Phase-7 voice pieces — the push-to-talk listener and meeting
capture — exposing status + two actions to the UI. It adds no authority: risky actions from
a voice turn still escalate through the unchanged ``VoiceApprover`` to the ``UIScreenApprover``
(voice prepares, screen commits), and a meeting still lands as an **unreviewed** KB source
(never an auto-action). Injectable so it's testable with the Phase-7 fakes — no mic, no keys.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from jarvis.observability import get_logger

if TYPE_CHECKING:
    from jarvis.voice import MeetingCapture, PushToTalkListener
    from jarvis.voice.listening import CaptureSource

IDLE = "idle"
LISTENING = "listening"


class UiVoice:
    """Holds the voice surface for the UI: a push-to-talk ``listener`` and/or a
    ``meeting`` capture (+ a ``capture`` source for the meeting audio). Any may be None."""

    def __init__(
        self,
        *,
        listener: PushToTalkListener | None = None,
        meeting: MeetingCapture | None = None,
        capture: CaptureSource | None = None,
        log=None,
    ) -> None:
        self.listener = listener
        self.meeting = meeting
        self.capture = capture
        self.log = log or get_logger("jarvis.ui.voice")
        self.state = IDLE

    def status(self) -> dict:
        """Simple, calm status for Daily Mode: is voice available, the listening state, and
        the meeting recording state (always visible — no silent capture)."""
        return {
            "enabled": self.listener is not None or self.meeting is not None,
            "listening": self.state,
            "meeting": getattr(self.meeting, "state", IDLE) if self.meeting is not None else IDLE,
        }

    async def listen_once(self) -> bool:
        """One push-to-talk activation (a single utterance → one turn). Returns whether a
        turn ran (False on silence / no listener)."""
        if self.listener is None:
            return False
        self.state = LISTENING
        try:
            result = await self.listener.listen_once()
            return result is not None
        finally:
            self.state = IDLE

    async def capture_meeting(self, *, title: str | None = None) -> object | None:
        """Capture one consented meeting recording → an unreviewed KB source. Requires both a
        capture source and a meeting mode; returns the ingest result (or None if unwired)."""
        if self.meeting is None or self.capture is None:
            return None
        audio = await self.capture.capture_utterance()
        return await self.meeting.capture(audio, title=title)
