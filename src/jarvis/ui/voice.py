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
from jarvis.voice.render import VoiceRenderer, _mask_secrets

if TYPE_CHECKING:
    from jarvis.core.agent import TurnResult
    from jarvis.core.client import ToolCall
    from jarvis.permissions.gate import Decision
    from jarvis.ui.connections import ConnectionManager
    from jarvis.voice import MeetingCapture, PushToTalkListener
    from jarvis.voice.listening import CaptureSource
    from jarvis.voice.protocols import TTSProvider

IDLE = "idle"
LISTENING = "listening"


class UiVoiceRenderer(VoiceRenderer):
    """The calm renderer, made visible in the browser (Phase 8). It mirrors the voice
    round-trip to the UI *without weakening the TTS-privacy rule*:

    * ``on_heard`` shows the heard transcript as an (untrusted) user message — obvious
      secrets masked, since it's echoed to a surface;
    * ``on_result`` / ``announce_escalation`` show the caption that is **exactly the safe
      text the base renderer spoke** (post-mask, post-cap, category-only for escalations) —
      it reuses the base method's return value, so a caption can never carry a raw answer,
      a command, a payload, or the particulars of a risky action.

    Mid-turn tool events stay a no-op (inherited ``__call__``), so the UI shows one heard
    bubble + one safe caption (+ the Gate modal when risky) — one attention surface, not an
    event firehose."""

    def __init__(self, tts: TTSProvider, connections: ConnectionManager, **kw) -> None:
        super().__init__(tts, **kw)
        self._conns = connections

    async def _mirror(self, role: str, text: str) -> None:
        if text:
            await self._conns.broadcast({"kind": "voice", "role": role, "text": text})

    async def on_heard(self, text: str) -> str:
        # Show the transcript as an untrusted user message (masked in case a secret was
        # spoken); the base still speaks the audio "I heard: …" echo.
        await self._mirror("heard", _mask_secrets(text).strip())
        return await super().on_heard(text)

    async def on_result(self, result: TurnResult) -> str:
        safe = await super().on_result(result)  # the SAFE, masked+capped spoken summary
        await self._mirror("reply", safe)
        return safe

    async def announce_escalation(self, call: ToolCall, decision: Decision) -> str:
        safe = await super().announce_escalation(call, decision)  # category + "on screen" only
        await self._mirror("reply", safe)
        return safe


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
