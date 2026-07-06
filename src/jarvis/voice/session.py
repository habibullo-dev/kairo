"""VoiceSession — the realtime voice loop (Phase 7).

The interface peer of the REPL: it turns one captured utterance into one user turn,
drives the same ``AgentLoop`` through the same seams (events out via the injected
``VoiceOutput``, the ``VoiceApprover`` in), and speaks a safe summary. It owns *when*
things happen (the state machine, the turn lock, cancel/barge-in) and *nothing* about
capture engines or the model — those are injected, so the whole loop is unit-testable
against fakes with no audio and no network.

Two safety properties live here:

* **Finalized-only.** Only an endpointed (``is_final``), non-empty transcript drives a
  turn — a partial never touches a tool (checkpoint §1.2 / plan D3).
* **Untrusted framing.** The transcript becomes a user turn only after
  :func:`~jarvis.voice.framing.frame_transcript` wraps it as untrusted content.

Approval is not decided here — it's the ``AgentLoop``'s injected approver
(``VoiceApprover``), the one and only approval path. A cancel (barge-in) resets state and
re-raises; because nothing risky commits without the screen, a cancel never leaves a
half-committed action (the Phase-1 turn-cancel invariant).
"""

from __future__ import annotations

import asyncio
import inspect
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from jarvis.observability import get_logger
from jarvis.voice.framing import frame_transcript

if TYPE_CHECKING:
    from jarvis.core.agent import AgentLoop, TurnResult
    from jarvis.core.events import Event
    from jarvis.voice.protocols import STTProvider

# The one-utterance state machine. Wake/capture (LISTENING/CAPTURING) is the listening
# layer's concern (Task 5); the session owns transcribe -> think -> speak -> idle.
IDLE = "idle"
TRANSCRIBING = "transcribing"
THINKING = "thinking"
SPEAKING = "speaking"


@runtime_checkable
class VoiceOutput(Protocol):
    """What the session needs from the voice renderer (implemented in Task 4). It is an
    ``EventSink`` (mid-turn events) plus ``on_heard`` (echo the transcript) and
    ``on_result`` (speak the *safe* summary — the renderer, not the session, decides what
    is safe to voice)."""

    def on_heard(self, text: str) -> None: ...

    def __call__(self, event: Event) -> None: ...

    async def on_result(self, result: TurnResult) -> None: ...


class VoiceSession:
    """Runs one voice turn per :meth:`handle_audio`. A peer of the REPL, not a new
    authority — every gate/floor applies beneath ``loop.run_turn`` as usual."""

    def __init__(
        self,
        *,
        loop: AgentLoop,
        stt: STTProvider,
        output: VoiceOutput,
        turn_lock: asyncio.Lock | None = None,
        log=None,
    ) -> None:
        self.loop = loop
        self.stt = stt
        self.output = output
        # Shared with the REPL/background runner when composed (Task 8): a voice turn is
        # an interactive turn and must not interleave with a background job.
        self.turn_lock = turn_lock or asyncio.Lock()
        self.log = log or get_logger("jarvis.voice")
        self.messages: list[dict] = []  # the voice conversation (accumulates across turns)
        self.state = IDLE

    async def handle_audio(self, audio: bytes) -> TurnResult | None:
        """Process one utterance's audio as one voice turn. Returns the ``TurnResult``, or
        ``None`` if the utterance was non-final or empty (no turn ran)."""
        self.state = TRANSCRIBING
        try:
            transcript = await self.stt.transcribe(audio)
            # Finalized-only: a partial or empty utterance never drives a turn or a tool.
            if not transcript.is_final or not transcript.text.strip():
                self.log.info("voice_utterance_skipped", reason="partial_or_empty")
                return None
            # Echo what was heard, before acting (a mishear is caught early). The renderer's
            # echo may be async (it speaks); a plain sink is sync — tolerate both.
            maybe = self.output.on_heard(transcript.text)
            if inspect.isawaitable(maybe):
                await maybe
            self.messages.append({"role": "user", "content": frame_transcript(transcript.text)})
            self.state = THINKING
            async with self.turn_lock:
                result = await self.loop.run_turn(self.messages, on_event=self.output)
            self.messages = result.messages
            self.state = SPEAKING
            await self.output.on_result(result)  # speak the safe summary
            return result
        except asyncio.CancelledError:
            # Barge-in / cancel: reset and re-raise (never swallow a cancel). Nothing risky
            # commits without the screen, so no half-committed action can be left behind.
            self.log.info("voice_turn_cancelled", state=self.state)
            raise
        finally:
            self.state = IDLE
