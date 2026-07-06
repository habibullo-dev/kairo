"""Build voice providers from config (Phase 7, Task 6).

Maps ``voice.stt_provider`` / ``voice.tts_provider`` to a concrete adapter. The cloud
opt-in is already enforced at config load (``VoiceConfig`` refuses a cloud provider without
``cloud_providers``), so a cloud choice here implies the opt-in was set — the factory just
passes the key through. Keys come from ``Secrets``; the caller supplies them, never this
module reading the environment.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from jarvis.voice.capture import SoundDeviceCapture
from jarvis.voice.stt import LocalTranscriber, OpenAITranscriber
from jarvis.voice.tts import ElevenLabsSynthesizer, OpenAISynthesizer, PrintSynthesizer

if TYPE_CHECKING:
    from rich.console import Console

    from jarvis.config import VoiceConfig
    from jarvis.voice.listening import CaptureSource
    from jarvis.voice.protocols import STTProvider, TTSProvider


def build_stt(config: VoiceConfig, *, openai_key: str = "", log=None) -> STTProvider:
    if config.stt_provider == "openai":
        return OpenAITranscriber(api_key=openai_key, log=log)
    if config.stt_provider == "local":
        return LocalTranscriber(log=log)
    raise ValueError(f"unknown voice.stt_provider: {config.stt_provider!r}")


def build_tts(
    config: VoiceConfig,
    *,
    openai_key: str = "",
    elevenlabs_key: str = "",
    console: Console | None = None,
    log=None,
) -> TTSProvider:
    if config.tts_provider == "openai":  # Phase 7 MVP cloud voice (one key, STT + TTS)
        return OpenAISynthesizer(api_key=openai_key, voice=config.tts_voice, log=log)
    if config.tts_provider == "elevenlabs":  # optional/deferred premium TTS
        return ElevenLabsSynthesizer(api_key=elevenlabs_key, voice=config.tts_voice, log=log)
    if config.tts_provider == "local":
        return PrintSynthesizer(console=console, log=log)
    raise ValueError(f"unknown voice.tts_provider: {config.tts_provider!r}")


def build_capture(config: VoiceConfig, *, log=None) -> CaptureSource:
    return SoundDeviceCapture(silence_seconds=config.endpoint_silence_seconds, log=log)
