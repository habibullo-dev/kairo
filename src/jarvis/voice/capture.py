"""Microphone capture + endpointing (Phase 7, Task 6) behind :class:`CaptureSource`.

The real recording (``SoundDeviceCapture``) lazy-imports ``sounddevice``/``numpy`` and is
live-only — it needs a mic, so tests skip it. The *logic* it depends on is factored into
two pure, testable helpers: :func:`is_utterance_end` (when has the user stopped talking?)
and :func:`pcm16_to_wav` (wrap raw PCM as a WAV blob the STT engines accept). Push-to-talk
only; no wake activation (D6).
"""

from __future__ import annotations

import io
import wave

from jarvis.observability import get_logger


def is_utterance_end(energies: list[float], *, threshold: float, silence_chunks: int) -> bool:
    """True once the utterance has ended: there was speech (some chunk above ``threshold``)
    and the last ``silence_chunks`` chunks are all below it. Pure, so the endpointing
    decision is unit-tested without a microphone."""
    if len(energies) < silence_chunks:
        return False
    if not any(e >= threshold for e in energies[:-silence_chunks] or energies):
        return False  # no speech yet — don't end on leading silence
    return all(e < threshold for e in energies[-silence_chunks:])


def pcm16_to_wav(pcm: bytes, *, samplerate: int = 16000, channels: int = 1) -> bytes:
    """Wrap raw little-endian PCM16 samples in a WAV container (stdlib ``wave``; no extra
    dependency). Pure — feed synthetic PCM, get a valid WAV back."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)  # 16-bit
        w.setframerate(samplerate)
        w.writeframes(pcm)
    return buf.getvalue()


class SoundDeviceCapture:
    """Record one endpointed utterance from the default input device. Live-only (needs a
    mic); lazy-imports its deps so the module loads on a base install."""

    def __init__(
        self,
        *,
        samplerate: int = 16000,
        chunk_ms: int = 100,
        silence_seconds: float = 0.8,
        max_seconds: float = 30.0,
        threshold: float = 0.01,
        log=None,
    ) -> None:
        self.samplerate = samplerate
        self.chunk_ms = chunk_ms
        self.silence_seconds = silence_seconds
        self.max_seconds = max_seconds
        self.threshold = threshold
        self.log = log or get_logger("jarvis.voice.capture")

    def _deps(self):
        try:
            import numpy
            import sounddevice
        except ImportError as exc:  # pragma: no cover - only without the extra
            raise RuntimeError(
                "microphone capture needs the voice extra: uv sync --extra voice"
            ) from exc
        return sounddevice, numpy

    async def capture_utterance(self) -> bytes:  # pragma: no cover - hardware, live-only
        import asyncio

        return await asyncio.to_thread(self._record)

    def _record(self) -> bytes:  # pragma: no cover - hardware, live-only
        sd, np = self._deps()
        chunk = int(self.samplerate * self.chunk_ms / 1000)
        silence_chunks = max(1, int(self.silence_seconds * 1000 / self.chunk_ms))
        max_chunks = int(self.max_seconds * 1000 / self.chunk_ms)
        energies: list[float] = []
        frames: list[bytes] = []
        with sd.InputStream(samplerate=self.samplerate, channels=1, dtype="int16") as stream:
            for _ in range(max_chunks):
                data, _overflow = stream.read(chunk)
                pcm = data.tobytes()
                frames.append(pcm)
                energies.append(float(np.sqrt(np.mean((data.astype("float32") / 32768.0) ** 2))))
                if is_utterance_end(
                    energies, threshold=self.threshold, silence_chunks=silence_chunks
                ):
                    break
        return pcm16_to_wav(b"".join(frames), samplerate=self.samplerate)
