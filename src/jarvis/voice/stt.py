"""Speech-to-text adapters (Phase 7, Task 6) behind :class:`STTProvider`.

Both lazy-import their engine, so these modules load on a base install without the ``voice``
extra; the engine is needed only at call time (or a test injects a client). ``OpenAI`` is
the cloud path — it sends **raw audio off-device**, so it counts/logs egress; ``Local`` is
faster-whisper (audio stays on-device, no egress). Cloud selection is gated at config load
(``voice.cloud_providers``), so these adapters don't re-check it.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from jarvis.observability import get_logger
from jarvis.voice.protocols import Transcript


class OpenAITranscriber:
    """Cloud STT via the OpenAI transcription API. Raw audio leaves the machine — every
    call adds to ``egress_bytes`` and emits an ``audio_egress`` log line (the bytes count,
    never the audio)."""

    def __init__(
        self, api_key: str, *, model: str = "whisper-1", client: object | None = None, log=None
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._client = client  # injectable for tests; else lazily built
        self.log = log or get_logger("jarvis.voice.stt")
        self.egress_bytes = 0

    def _get_client(self) -> object:
        if self._client is None:
            try:
                from openai import AsyncOpenAI
            except ImportError as exc:  # pragma: no cover - exercised only without the extra
                raise RuntimeError(
                    "cloud STT needs the voice extra: uv sync --extra voice"
                ) from exc
            if not self._api_key:
                raise RuntimeError("cloud STT needs OPENAI_API_KEY (or use stt_provider: local)")
            self._client = AsyncOpenAI(api_key=self._api_key)
        return self._client

    async def transcribe(self, audio: bytes) -> Transcript:
        self.egress_bytes += len(audio)
        self.log.info("audio_egress", provider="openai", bytes=len(audio))  # visible egress
        client = self._get_client()
        resp = await client.audio.transcriptions.create(  # type: ignore[attr-defined]
            model=self._model, file=("utterance.wav", audio, "audio/wav")
        )
        return Transcript(text=(getattr(resp, "text", "") or "").strip(), is_final=True)


class LocalTranscriber:
    """Local STT via faster-whisper — audio stays on-device (no egress). faster-whisper is
    a heavy, platform-specific dependency, so it is lazy-imported and installed only with
    the ``voice`` extra."""

    def __init__(
        self, *, model_size: str = "large-v3", model: object | None = None, log=None
    ) -> None:
        self._model_size = model_size
        self._model = model  # injectable for tests
        self.log = log or get_logger("jarvis.voice.stt")

    def _get_model(self) -> object:
        if self._model is None:
            try:
                from faster_whisper import WhisperModel
            except ImportError as exc:  # pragma: no cover - exercised only without the engine
                raise RuntimeError(
                    "local STT needs faster-whisper: uv sync --extra voice"
                ) from exc
            self._model = WhisperModel(self._model_size, device="cpu", compute_type="int8")
        return self._model

    async def transcribe(self, audio: bytes) -> Transcript:
        model = self._get_model()
        # faster-whisper is synchronous — run it off the event loop.
        text = await asyncio.to_thread(self._run, model, audio)
        return Transcript(text=text.strip(), is_final=True)

    async def transcribe_file(self, path: Path) -> Transcript:
        """Transcribe a locally staged encoded media file (OGG/MP3/M4A/WAV/…)."""
        model = self._get_model()
        text = await asyncio.to_thread(self._run, model, str(path))
        return Transcript(text=text.strip(), is_final=True)

    @staticmethod
    def _run(model: object, audio: object) -> str:
        segments, _info = model.transcribe(  # type: ignore[attr-defined]
            audio,
            vad_filter=True,
            vad_parameters={"min_silence_duration_ms": 500},
        )
        return " ".join(
            seg.text
            for seg in segments
            if float(getattr(seg, "no_speech_prob", 0.0) or 0.0) < 0.6
        )
