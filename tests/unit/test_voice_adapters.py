"""Live STT/TTS adapters + capture helpers + provider factory (Phase 7, Task 6).

Keyless: the SDK clients are injected fakes (no network), the capture helpers are pure,
and the live recording path is skipped (no mic). Pins: cloud adapters count egress and
build the right result; the local TTS is dependency-free; the factory maps config to the
right adapter and cloud selection needs a key.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from jarvis.config import VoiceConfig
from jarvis.voice import (
    ElevenLabsSynthesizer,
    LocalTranscriber,
    OpenAITranscriber,
    PrintSynthesizer,
    build_stt,
    build_tts,
    is_utterance_end,
    pcm16_to_wav,
)

# --- cloud STT: builds a transcript + counts egress -------------------------


def _fake_openai(text: str) -> object:
    async def _create(**_kw):
        return SimpleNamespace(text=text)

    return SimpleNamespace(audio=SimpleNamespace(transcriptions=SimpleNamespace(create=_create)))


async def test_openai_transcriber_returns_transcript_and_counts_egress() -> None:
    stt = OpenAITranscriber(api_key="k", client=_fake_openai("hello world"))
    t = await stt.transcribe(b"\x00\x01\x02\x03")
    assert t.text == "hello world" and t.is_final is True
    assert stt.egress_bytes == 4  # raw audio left the machine — counted


async def test_openai_transcriber_needs_a_key_without_client() -> None:
    with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
        await OpenAITranscriber(api_key="").transcribe(b"x")


# --- local STT: no egress ---------------------------------------------------


async def test_local_transcriber_joins_segments() -> None:
    model = SimpleNamespace(
        transcribe=lambda audio: (
            [SimpleNamespace(text="local"), SimpleNamespace(text="hello")],
            None,
        )
    )
    stt = LocalTranscriber(model=model)
    t = await stt.transcribe(b"pcm")
    assert t.text == "local hello"
    assert not hasattr(stt, "egress_bytes")  # local => nothing leaves the machine


# --- cloud TTS: bytes out + counts egress -----------------------------------


async def test_elevenlabs_synthesizer_returns_audio_and_counts_egress() -> None:
    fake = SimpleNamespace(text_to_speech=SimpleNamespace(convert=lambda **_kw: b"AUDIO"))
    tts = ElevenLabsSynthesizer(api_key="k", client=fake)
    assert await tts.synthesize("the meeting is at noon") == b"AUDIO"
    assert tts.egress_chars == len("the meeting is at noon")  # spoken text left the machine


async def test_elevenlabs_needs_a_key_without_client() -> None:
    with pytest.raises(RuntimeError, match="ELEVENLABS_API_KEY"):
        await ElevenLabsSynthesizer(api_key="").synthesize("hi")


# --- local TTS: dependency-free, no egress ----------------------------------


async def test_print_synthesizer_speaks_locally() -> None:
    tts = PrintSynthesizer()  # no console, no dep, no network
    audio = await tts.synthesize("done")
    assert audio == b"" and tts.spoken == ["done"]


# --- capture helpers (pure) -------------------------------------------------


def test_is_utterance_end() -> None:
    lo, hi = 0.001, 0.5  # below / above threshold 0.01
    # all silence => not an end (no speech yet)
    assert is_utterance_end([lo, lo, lo], threshold=0.01, silence_chunks=2) is False
    # speech then enough trailing silence => end
    assert is_utterance_end([hi, hi, lo, lo], threshold=0.01, silence_chunks=2) is True
    # speech then too-short silence => not yet
    assert is_utterance_end([hi, hi, lo], threshold=0.01, silence_chunks=2) is False
    # still speaking => not an end
    assert is_utterance_end([hi, hi, hi], threshold=0.01, silence_chunks=2) is False


def test_pcm16_to_wav_produces_a_valid_wav() -> None:
    wav = pcm16_to_wav(b"\x00\x00\x01\x00\x02\x00", samplerate=16000)
    assert wav[:4] == b"RIFF" and b"WAVE" in wav[:16]


# --- factory: config -> adapter ---------------------------------------------


def test_factory_maps_providers() -> None:
    assert isinstance(build_stt(VoiceConfig(stt_provider="local")), LocalTranscriber)
    assert isinstance(build_tts(VoiceConfig(tts_provider="local")), PrintSynthesizer)
    cloud = VoiceConfig(cloud_providers=True, stt_provider="openai", tts_provider="elevenlabs")
    assert isinstance(build_stt(cloud, openai_key="k"), OpenAITranscriber)
    assert isinstance(build_tts(cloud, elevenlabs_key="k"), ElevenLabsSynthesizer)


def test_factory_rejects_unknown_provider() -> None:
    with pytest.raises(ValueError, match="stt_provider"):
        build_stt(VoiceConfig(stt_provider="bogus"))
