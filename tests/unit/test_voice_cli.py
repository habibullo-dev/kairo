"""CLI wiring for the voice interface (Phase 7, Task 8).

The point of these tests is the *composition contract*, not I/O: `jarvis --voice` builds
a voice turn from the REPL's own collaborators, but with the safety-critical seams swapped
in — the injected approver is a ``VoiceApprover`` that escalates to an on-screen
``TerminalScreenApprover`` (never voice), the output is the calm ``VoiceRenderer``, and the
model gets the voice framing in its system prompt. A voice turn shares the REPL's turn lock
so it can't interleave a background job. Keyless: a ``FakeClient`` REPL, no mic, no DB.
"""

from __future__ import annotations

import io
from pathlib import Path

from rich.console import Console

from jarvis.cli.repl import Repl, build_voice_session, run_voice
from jarvis.config import VoiceConfig, load_config
from jarvis.core import FakeClient
from jarvis.core.prompts import VOICE_GUIDANCE
from jarvis.voice import (
    LocalTranscriber,
    OpenAITranscriber,
    PrintSynthesizer,
    PushToTalkListener,
    TerminalScreenApprover,
    VoiceApprover,
    VoiceRenderer,
    VoiceSession,
)


def _console() -> Console:
    return Console(file=io.StringIO(), force_terminal=False, width=100)


def _voice(tmp_path: Path, config=None) -> tuple[Repl, VoiceSession, PushToTalkListener]:
    config = config or load_config(root=tmp_path, env_file=None)
    repl = Repl(config, client=FakeClient([]), console=_console())
    session, listener = build_voice_session(config, repl=repl, console=repl.console)
    return repl, session, listener


# --- composition: the safety-critical seams --------------------------------


def test_approver_is_voice_approver_escalating_to_the_screen(tmp_path: Path) -> None:
    _repl, session, _listener = _voice(tmp_path)
    approver = session.loop.approver
    # The injected Approver is the voice one: every ASK escalates to a screen, never voice.
    assert isinstance(approver, VoiceApprover)
    # The screen is the terminal — the same TTY the user sees, confirmed by keystroke.
    assert isinstance(approver.screen, TerminalScreenApprover)
    # The escalation announcement is the renderer's SAFE line (never the input preview):
    # this is the only thing spoken on an ASK, and the renderer enforces TTS privacy.
    assert approver.on_escalate == session.output.announce_escalation


def test_output_is_the_calm_renderer_over_local_tts(tmp_path: Path) -> None:
    _repl, session, _listener = _voice(tmp_path)
    assert isinstance(session.output, VoiceRenderer)
    # Default tts_provider is local → the dependency-free PrintSynthesizer (offline, no egress).
    assert isinstance(session.output.tts, PrintSynthesizer)


def test_stt_defaults_to_local_on_device(tmp_path: Path) -> None:
    _repl, session, _listener = _voice(tmp_path)
    # Default stt_provider is local: audio stays on-device, no cloud egress.
    assert isinstance(session.stt, LocalTranscriber)


def test_voice_turn_shares_the_repl_turn_lock(tmp_path: Path) -> None:
    repl, session, _listener = _voice(tmp_path)
    # A voice turn is an interactive turn: it must serialize against background jobs.
    assert session.turn_lock is repl.turn_lock


def test_listener_wraps_the_session(tmp_path: Path) -> None:
    _repl, session, listener = _voice(tmp_path)
    assert isinstance(listener, PushToTalkListener)
    assert listener.session is session
    assert listener.attended is True  # no unattended mic


def test_system_prompt_carries_the_voice_framing(tmp_path: Path) -> None:
    _repl, session, _listener = _voice(tmp_path)
    # The untrusted-audio + no-voice-approval framing reaches the model, not just the code.
    assert VOICE_GUIDANCE in session.loop.system


# --- cloud opt-in flows the key through composition ------------------------


def test_cloud_stt_selected_and_key_threaded(tmp_path: Path) -> None:
    config = load_config(root=tmp_path, env_file=None)
    config.voice = VoiceConfig(enabled=True, cloud_providers=True, stt_provider="openai")
    config.secrets = config.secrets.model_copy(update={"openai_api_key": "sk-test"})
    _repl, session, _listener = _voice(tmp_path, config=config)
    assert isinstance(session.stt, OpenAITranscriber)
    assert session.stt._api_key == "sk-test"  # the Secrets key reaches the adapter


# --- disabled path: no voice surface, nothing opened -----------------------


async def test_run_voice_disabled_prints_and_returns(tmp_path: Path) -> None:
    config = load_config(root=tmp_path, env_file=None)  # voice.enabled defaults False
    out = io.StringIO()
    console = Console(file=out, force_terminal=False, width=100)
    # Returns immediately: never opens the DB, never needs a mic or a key.
    await run_voice(config, console=console)
    assert "not enabled" in out.getvalue().lower()
