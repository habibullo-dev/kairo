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
from jarvis.voice.framing import frame_transcript
from jarvis.voice.protocols import (
    FakeSynthesizer,
    FakeTranscriber,
    STTProvider,
    Transcript,
    TTSProvider,
)

__all__ = [
    "FakeSynthesizer",
    "FakeTranscriber",
    "STTProvider",
    "ScreenApprover",
    "ScriptedScreenApprover",
    "TTSProvider",
    "TerminalScreenApprover",
    "Transcript",
    "VoiceApprover",
    "frame_transcript",
]
