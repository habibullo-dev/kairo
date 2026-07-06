"""Listening layer (Phase 7, Task 5): push-to-talk capture, one utterance per activation.

**Push-to-talk is the shipped path.** The wake contract is *designed and tested* here —
one activation captures a single utterance and returns to idle, and a spurious trigger
commits nothing — but wake-word **activation is deferred** (D6 / ADR-0007):
:func:`wake_active` is ``False`` in the MVP, and a configured ``wake_word`` does not turn
it on by itself.

Two safety properties, both structural:

* **Least-listening / one-turn scope.** :meth:`PushToTalkListener.listen_once` captures a
  single endpointed utterance, runs one turn, and returns to idle — never an indefinite
  window.
* **No unattended mic.** Capture requires an attended session; an unattended context is
  refused. Structurally, a background job never constructs a listener at all — the
  microphone analogue of ``spawn_agent`` being in the unattended ``HARD_DENY`` set.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from jarvis.observability import get_logger

if TYPE_CHECKING:
    from jarvis.config import VoiceConfig
    from jarvis.core.agent import TurnResult
    from jarvis.voice.session import VoiceSession

IDLE = "idle"
LISTENING = "listening"
CAPTURING = "capturing"


@runtime_checkable
class CaptureSource(Protocol):
    """Capture one endpointed utterance's audio (push-to-talk). Blocks until the utterance
    ends (VAD silence or an explicit stop) and returns its audio; returns ``b""`` if
    nothing was captured (silence)."""

    async def capture_utterance(self) -> bytes: ...


@dataclass
class FakeCapture:
    """Returns queued audio blobs in order (``b""`` when exhausted) — lets the listening
    loop be driven with no microphone."""

    scripted: list[bytes] = field(default_factory=list)
    calls: int = 0

    async def capture_utterance(self) -> bytes:
        self.calls += 1
        return self.scripted.pop(0) if self.scripted else b""


def wake_active(config: VoiceConfig) -> bool:
    """Whether wake-word *activation* is on. **Always False in the Phase-7 MVP** — the wake
    contract is designed and tested but not wired (D6 / ADR-0007). A configured
    ``wake_word`` does not enable it; turning wake on is an explicit later step."""
    return False


class PushToTalkListener:
    """Drives one push-to-talk activation at a time. ``on_state`` observes the listening
    state (a UI shows it; there is no silent capture). ``attended`` must be True — an
    unattended context is refused (no unattended mic)."""

    def __init__(
        self,
        capture: CaptureSource,
        session: VoiceSession,
        *,
        attended: bool = True,
        on_state: Callable[[str], None] | None = None,
        log=None,
    ) -> None:
        self.capture = capture
        self.session = session
        self.attended = attended
        self.on_state = on_state
        self.log = log or get_logger("jarvis.voice.listening")
        self.state = IDLE

    def _set(self, state: str) -> None:
        self.state = state
        if self.on_state is not None:
            self.on_state(state)  # observable — the UI reflects listening state

    async def listen_once(self) -> TurnResult | None:
        """One activation: capture a single utterance and run one turn, then return to
        idle (least-listening; never an indefinite window)."""
        if not self.attended:
            # No unattended mic — capture requires a present human. A background run never
            # builds a listener; this guard makes the refusal explicit if one is misused.
            raise RuntimeError(
                "voice capture requires an attended session; refusing to open the microphone"
            )
        self._set(LISTENING)
        try:
            audio = await self.capture.capture_utterance()
            self._set(CAPTURING)
            if not audio:
                return None  # silence — no turn
            return await self.session.handle_audio(audio)
        finally:
            self._set(IDLE)  # always return to idle — one utterance per activation
