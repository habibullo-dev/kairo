"""Voice interface (Phase 7): push-to-talk speech in, safe spoken summary out.

Voice is an *interface* — a peer of the REPL that drives the same ``AgentLoop`` through
the same seams (events out, the injected ``Approver`` in) — never a new authority. Its
safety floor is docs/PLAN-7-voice-permissions-checkpoint.md: read-only by default,
transcribed audio is untrusted, risky actions escalate to on-screen confirmation (never
voice-only), no unattended mic. Design in docs/PLAN-7-voice.md and ADR-0007.

This package currently exports the provider boundaries + fakes and the transcript framing;
the approver, session, renderer, and live engines land in later Phase-7 tasks.
"""

from jarvis.voice.approver import (
    ScreenApprover,
    ScriptedScreenApprover,
    TerminalScreenApprover,
    VoiceApprover,
)
from jarvis.voice.capture import SoundDeviceCapture, is_utterance_end, pcm16_to_wav
from jarvis.voice.factory import build_capture, build_stt, build_tts
from jarvis.voice.framing import frame_transcript
from jarvis.voice.listening import (
    CaptureSource,
    FakeCapture,
    PushToTalkListener,
    wake_active,
)
from jarvis.voice.meeting import MeetingCapture
from jarvis.voice.protocols import (
    FakeSynthesizer,
    FakeTranscriber,
    STTProvider,
    Transcript,
    TTSProvider,
)
from jarvis.voice.render import VoiceRenderer
from jarvis.voice.session import VoiceOutput, VoiceSession
from jarvis.voice.stt import LocalTranscriber, OpenAITranscriber
from jarvis.voice.tts import ElevenLabsSynthesizer, PrintSynthesizer

__all__ = [
    "CaptureSource",
    "ElevenLabsSynthesizer",
    "FakeCapture",
    "FakeSynthesizer",
    "FakeTranscriber",
    "LocalTranscriber",
    "MeetingCapture",
    "OpenAITranscriber",
    "PrintSynthesizer",
    "PushToTalkListener",
    "STTProvider",
    "ScreenApprover",
    "ScriptedScreenApprover",
    "SoundDeviceCapture",
    "TTSProvider",
    "TerminalScreenApprover",
    "Transcript",
    "VoiceApprover",
    "VoiceOutput",
    "VoiceRenderer",
    "VoiceSession",
    "build_capture",
    "build_stt",
    "build_tts",
    "frame_transcript",
    "is_utterance_end",
    "pcm16_to_wav",
    "wake_active",
]
