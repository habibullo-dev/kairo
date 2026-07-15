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
  :func:`~kira.voice.framing.frame_transcript` wraps it as untrusted content.

Approval is not decided here — it's the ``AgentLoop``'s injected approver
(``VoiceApprover``), the one and only approval path. A cancel (barge-in) resets state and
re-raises; because nothing risky commits without the screen, a cancel never leaves a
half-committed action (the Phase-1 turn-cancel invariant).
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Callable
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from kira.observability import get_logger
from kira.voice.framing import frame_transcript

if TYPE_CHECKING:
    from kira.core.agent import AgentLoop, TurnResult
    from kira.core.events import Event
    from kira.projects.context import ProjectContext
    from kira.voice.protocols import STTProvider

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
        on_state: Callable[[str], None] | None = None,
        project: Callable[[], ProjectContext] | None = None,
        on_project: Callable[[str | None], None] | None = None,
        log=None,
    ) -> None:
        self.loop = loop
        self.stt = stt
        self.output = output
        # Shared with the REPL/background runner when composed (Task 8): a voice turn is
        # an interactive turn and must not interleave with a background job.
        self.turn_lock = turn_lock or asyncio.Lock()
        # Observes turn-lifecycle state (transcribing/thinking/speaking/idle) — a read-only
        # signal for the UI status pill. Carries ONLY the state name, never any content.
        self.on_state = on_state
        # Phase 10 (A3): the active project provider (inherited from the process; GLOBAL when
        # none) and an announce callback fired at turn start. ``on_project`` carries ONLY the
        # project name (or None for global) — never content — so the surface can display
        # "working in <project>" before the turn runs. Voice never *sets* a project.
        self.project = project
        self.on_project = on_project
        self.log = log or get_logger("kira.voice")
        self.messages: list[dict] = []  # the voice conversation (accumulates across turns)
        self.state = IDLE

    def _set(self, state: str) -> None:
        self.state = state
        if self.on_state is not None:
            self.on_state(state)  # observable; a fixed state string, never transcript/answer

    def _announce_project(self) -> None:
        """A3: announce the active project (name, or None for global) at turn start, so a
        voice turn always makes its scope visible. Carries only the name — never content."""
        name = self.project().name if self.project is not None else None
        self.log.info("voice_active_project", project=name)
        if self.on_project is not None:
            self.on_project(name)

    async def handle_audio(self, audio: bytes) -> TurnResult | None:
        """Process one utterance's audio as one voice turn. Returns the ``TurnResult``, or
        ``None`` if the utterance was non-final or empty (no turn ran)."""
        self._set(TRANSCRIBING)
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
            self._announce_project()  # A3: say which project this turn works in, before acting
            self.messages.append({"role": "user", "content": frame_transcript(transcript.text)})
            self._set(THINKING)
            async with self.turn_lock:
                result = await self.loop.run_turn(self.messages, on_event=self.output)
            self.messages = result.messages
            self._set(SPEAKING)
            await self.output.on_result(result)  # speak the safe summary
            return result
        except asyncio.CancelledError:
            # Barge-in / cancel: reset and re-raise (never swallow a cancel). Nothing risky
            # commits without the screen, so no half-committed action can be left behind.
            self.log.info("voice_turn_cancelled", state=self.state)
            raise
        finally:
            self._set(IDLE)
