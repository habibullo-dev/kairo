"""Optional speaker playback for voice output (Phase 8).

This module plays **bytes**, not text. The only bytes it ever receives are what the renderer
synthesized from its *safe caption* (post mask / length-cap / category-only escalation) — so
by construction there is no path for raw model output or a tool payload to reach the speaker.
It is off by default (``voice.play_audio``); the renderer's ``play`` hook stays ``None`` unless
opted in. Live audio I/O is lazy-imported and not unit-covered.
"""

from __future__ import annotations

import asyncio
import io
import wave

from kira.observability import get_logger

_log = get_logger("kira.voice.playback")


async def play_wav(audio: bytes) -> None:
    """Play WAV bytes through the default output device. A no-op on empty audio (e.g. the
    dependency-free local synthesizer). Best-effort: a playback failure is logged, never
    raised, so it can't break a turn."""
    if not audio:
        return
    try:  # pragma: no cover - live audio device I/O
        import numpy as np
        import sounddevice as sd

        with wave.open(io.BytesIO(audio), "rb") as w:
            rate, channels = w.getframerate(), w.getnchannels()
            frames = w.readframes(w.getnframes())
        data = np.frombuffer(frames, dtype=np.int16)
        if channels > 1:
            data = data.reshape(-1, channels)

        def _blocking_play() -> None:
            sd.play(data, rate)
            sd.wait()

        await asyncio.to_thread(_blocking_play)
    except Exception as exc:  # noqa: BLE001 - playback is best-effort; never break a turn
        _log.warning("voice_playback_failed", error=repr(exc))
