"""Speech-to-text / text-to-speech provider boundaries (Phase 7).

The same discipline as the ``LLMClient`` boundary: the voice session talks to *a*
``STTProvider`` / ``TTSProvider``, never a concrete engine, so the whole voice layer —
session, approver, renderer — is unit-testable against the fakes below with no audio and
no network, and a live engine (cloud or local) drops in later behind the protocol.

Two safety-relevant shapes live here:

* ``Transcript.is_final`` — only a *finalized* (endpointed) transcript may drive a turn;
  partials are display-only (checkpoint §1.2 / plan D3). The protocol carries the flag so
  the session can enforce it.
* Cloud engines send data off-device (raw audio to STT; the assistant's spoken text to
  TTS). Selecting one is gated behind ``voice.cloud_providers`` in config, not here —
  these protocols are transport-agnostic.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


@dataclass
class Transcript:
    """A speech-to-text result. ``is_final`` marks an endpointed utterance (may drive a
    turn) versus a partial (display-only, never drives tools)."""

    text: str
    is_final: bool = True
    confidence: float | None = None


@runtime_checkable
class STTProvider(Protocol):
    """Transcribe an utterance's audio into text. MVP is batch (whole utterance in,
    one :class:`Transcript` out); streaming partials are a deferred enhancement."""

    async def transcribe(self, audio: bytes) -> Transcript: ...


@runtime_checkable
class TTSProvider(Protocol):
    """Synthesize speech for ``text``. MVP returns the whole clip; streaming synth is
    deferred. The caller (the renderer) is responsible for *what* text is safe to speak —
    the TTS privacy rule lives in the renderer, not the engine."""

    async def synthesize(self, text: str) -> bytes: ...


# --- keyless test doubles --------------------------------------------------


@dataclass
class FakeTranscriber:
    """Returns queued transcripts in order (finalized by default); '' when exhausted.
    Lets the session/approver/renderer be driven by scripted 'speech' with no audio."""

    scripted: list[str] = field(default_factory=list)
    calls: int = 0

    async def transcribe(self, audio: bytes) -> Transcript:
        self.calls += 1
        text = self.scripted.pop(0) if self.scripted else ""
        return Transcript(text=text, is_final=True)


@dataclass
class FakeSynthesizer:
    """Records the text it was asked to speak (no audio) so a test can assert exactly
    what reached the speaker — the substrate for the TTS-privacy pins."""

    spoken: list[str] = field(default_factory=list)

    async def synthesize(self, text: str) -> bytes:
        self.spoken.append(text)
        return b""
