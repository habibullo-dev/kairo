"""Voice controller for the workstation (Phase 8, Task 6).

A thin controller over the Phase-7 voice pieces — the push-to-talk listener and meeting
capture — exposing status + two actions to the UI. It adds no authority: risky actions from
a voice turn still escalate through the unchanged ``VoiceApprover`` to the ``UIScreenApprover``
(voice prepares, screen commits), and a meeting still lands as an **unreviewed** KB source
(never an auto-action). Injectable so it's testable with the Phase-7 fakes — no mic, no keys.
"""

from __future__ import annotations

import asyncio
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
    """Holds the voice surface for the UI: a push-to-talk ``listener`` and/or a ``meeting``
    capture (+ a ``capture`` source). Any may be None. ``connections`` is wired so the
    read-only voice *state* can stream to the browser status bar."""

    #: TTS caption cap — the safe caption is short by construction; this is a belt so a browser
    #: can't push a long string off-device via the playback route.
    TTS_MAX_CHARS = 600

    def __init__(
        self,
        *,
        listener: PushToTalkListener | None = None,
        meeting: MeetingCapture | None = None,
        capture: CaptureSource | None = None,
        connections: ConnectionManager | None = None,
        tts: object | None = None,
        stt_name: str = "local",
        tts_name: str = "local",
        log=None,
    ) -> None:
        self.listener = listener
        self.meeting = meeting
        self.capture = capture
        self.connections = connections
        # Phase 15.5: the TTS provider (for browser playback of the SAFE caption) + the provider
        # NAMES (presence/label only, never a key). ``tts`` is set post-construction by the host.
        self.tts = tts
        self.stt_name = stt_name
        self.tts_name = tts_name
        self.log = log or get_logger("jarvis.ui.voice")
        self.state = IDLE
        self._pushes: set = set()

    def note_state(self, state: str) -> None:
        """Read-only voice-state hook — wired to the listener/session/meeting ``on_state``.
        Records the latest state and streams a status pill to the browser. It carries ONLY
        the state name (idle/listening/transcribing/thinking/speaking); it can never derive
        from a transcript, an answer, or a tool payload (there is no such argument)."""
        self.state = state
        if self.connections is None:
            return
        try:
            task = asyncio.create_task(
                self.connections.broadcast({"kind": "voice_state", "state": state})
            )
            self._pushes.add(task)
            task.add_done_callback(self._pushes.discard)
        except RuntimeError:
            pass  # no running loop (sync context) — status() poll still reflects self.state

    def status(self) -> dict:
        """Calm, honest voice status: availability, the live listening state, the meeting
        recording state, WHY it's off if it is, the STT/TTS provider names (presence only, never
        a key), and whether TTS playback can produce audio (cloud) vs subtitle-only (local)."""
        enabled = self.listener is not None or self.meeting is not None
        return {
            "enabled": enabled,
            "listening": self.state,
            "meeting": getattr(self.meeting, "state", IDLE) if self.meeting is not None else IDLE,
            "reason": "" if enabled else "Voice is wired but no listener is available.",
            "stt": self.stt_name,
            "tts": self.tts_name,
            "playback": self.tts is not None and self.tts_name != "local",  # cloud TTS → audio
        }

    async def listen_once(self) -> bool:
        """One SERVER-mic push-to-talk activation (kept as a fallback; the browser mic is the
        primary path in the workstation). A single utterance → one turn. Returns whether a turn
        ran; state transitions stream via the listener's ``on_state`` → :meth:`note_state`."""
        if self.listener is None:
            return False
        result = await self.listener.listen_once()
        return result is not None

    async def handle_utterance(self, audio: bytes) -> bool:
        """Run one BROWSER-captured utterance as a voice turn: feed the audio to the SAME voice
        session the server-mic path uses (STT → framed untrusted turn → safe caption), through the
        unchanged VoiceApprover — so the screen stays the only approval surface. Returns whether a
        turn ran (False on no listener / empty audio / a non-final transcript)."""
        if self.listener is None or not audio:
            return False
        result = await self.listener.session.handle_audio(audio)
        return result is not None

    async def synthesize_caption(self, text: str) -> bytes | None:
        """Synthesize ONLY a safe caption for browser playback: mask secret shapes + cap length,
        then hand it to the existing TTS provider. Because the input is masked + capped here, a raw
        answer, tool payload, or secret can never reach TTS. Returns None when no cloud TTS is
        wired (local/subtitle mode ⇒ the browser keeps captions as text)."""
        if self.tts is None:
            return None
        safe = _mask_secrets(text or "")[: self.TTS_MAX_CHARS]
        if not safe.strip():
            return None
        return await self.tts.synthesize(safe)

    async def capture_meeting(self, *, title: str | None = None) -> object | None:
        """Capture one consented meeting recording → an unreviewed KB source. Requires both a
        capture source and a meeting mode; returns the ingest result (or None if unwired)."""
        if self.meeting is None or self.capture is None:
            return None
        audio = await self.capture.capture_utterance()
        return await self.meeting.capture(audio, title=title)
