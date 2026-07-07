"""Text-to-speech adapters (Phase 7) behind :class:`TTSProvider`.

``OpenAI`` is the Phase-7 MVP cloud voice — one key covers both STT and TTS — and
``ElevenLabs`` is an optional/deferred premium path; both send the assistant's *spoken
text* off-device, so they count/log egress and lazy-import their SDKs. ``Print`` is the
local, dependency-free default: it "speaks" by writing the (already-safe) text to the
console — a subtitle mode until a real local engine is chosen, and enough for a
keyless/offline setup. The renderer (Task 4) is what guarantees only *safe* text ever
reaches any of these — the TTS-privacy rule (never secrets, commands, or risky-action
details) lives there, upstream of every synthesizer.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from jarvis.observability import get_logger

if TYPE_CHECKING:
    from rich.console import Console


class ElevenLabsSynthesizer:
    """Cloud TTS via ElevenLabs. The spoken text leaves the machine — every call adds to
    ``egress_chars`` and logs (the length, never the text). Behind the ``voice`` extra +
    the explicit cloud opt-in (enforced at config load)."""

    def __init__(
        self,
        api_key: str,
        *,
        voice: str | None = None,
        model: str = "eleven_turbo_v2_5",
        client: object | None = None,
        log=None,
    ) -> None:
        self._api_key = api_key
        self._voice = voice or "Rachel"
        self._model = model
        self._client = client  # injectable for tests
        self.log = log or get_logger("jarvis.voice.tts")
        self.egress_chars = 0

    def _get_client(self) -> object:
        if self._client is None:
            try:
                from elevenlabs.client import ElevenLabs
            except ImportError as exc:  # pragma: no cover - only without the extra
                raise RuntimeError(
                    "cloud TTS needs the voice extra: uv sync --extra voice"
                ) from exc
            if not self._api_key:
                raise RuntimeError(
                    "cloud TTS needs ELEVENLABS_API_KEY (or use tts_provider: local)"
                )
            self._client = ElevenLabs(api_key=self._api_key)
        return self._client

    async def synthesize(self, text: str) -> bytes:
        self.egress_chars += len(text)
        self.log.info("text_egress", provider="elevenlabs", chars=len(text))  # visible egress
        client = self._get_client()
        audio = client.text_to_speech.convert(  # type: ignore[attr-defined]
            voice_id=self._voice, model_id=self._model, text=text
        )
        return audio if isinstance(audio, bytes) else b"".join(audio)


class OpenAISynthesizer:
    """Cloud TTS via the OpenAI speech API — the Phase-7 MVP cloud voice (one key covers
    both STT and TTS). The spoken text leaves the machine, so every call adds to
    ``egress_chars`` and logs (the length, never the text). Behind the ``voice`` extra + the
    explicit cloud opt-in (enforced at config load). Lazy-imports its SDK; a test injects a
    fake client."""

    def __init__(
        self,
        api_key: str,
        *,
        voice: str | None = None,
        model: str = "gpt-4o-mini-tts",
        client: object | None = None,
        log=None,
    ) -> None:
        self._api_key = api_key
        self._voice = voice or "alloy"
        self._model = model
        self._client = client  # injectable for tests
        self.log = log or get_logger("jarvis.voice.tts")
        self.egress_chars = 0

    def _get_client(self) -> object:
        if self._client is None:
            try:
                from openai import AsyncOpenAI
            except ImportError as exc:  # pragma: no cover - only without the extra
                raise RuntimeError(
                    "cloud TTS needs the voice extra: uv sync --extra voice"
                ) from exc
            if not self._api_key:
                raise RuntimeError("cloud TTS needs OPENAI_API_KEY (or use tts_provider: local)")
            self._client = AsyncOpenAI(api_key=self._api_key)
        return self._client

    async def synthesize(self, text: str) -> bytes:
        self.egress_chars += len(text)
        self.log.info("text_egress", provider="openai", chars=len(text))  # visible egress
        client = self._get_client()
        # WAV so the bytes are playable via stdlib `wave` + sounddevice (optional playback);
        # the text sent is exactly what the caller passed — the renderer's safe caption.
        resp = await client.audio.speech.create(  # type: ignore[attr-defined]
            model=self._model, voice=self._voice, input=text, response_format="wav"
        )
        # openai returns binary content (resp.content); tolerate a fake that returns bytes.
        audio = getattr(resp, "content", resp)
        return audio if isinstance(audio, bytes) else bytes(audio)


class PrintSynthesizer:
    """Local, dependency-free TTS: writes the safe text to the console (a subtitle mode).
    The default ``tts_provider: local`` — no audio, no egress, works offline. ``spoken``
    records what was 'said' for tests."""

    def __init__(self, console: Console | None = None, *, log=None) -> None:
        self.console = console
        self.log = log or get_logger("jarvis.voice.tts")
        self.spoken: list[str] = []

    async def synthesize(self, text: str) -> bytes:
        self.spoken.append(text)
        if self.console is not None:
            self.console.print(f"[cyan]\N{SPEAKER}  {text}[/]")
        else:
            print(f"\N{SPEAKER}  {text}")
        return b""
