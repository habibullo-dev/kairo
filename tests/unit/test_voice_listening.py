"""Listening layer (Phase 7, Task 5): push-to-talk, one-utterance scope, observable
state, no unattended mic, and wake-activation-deferred. Keyless (FakeCapture)."""

from __future__ import annotations

from pathlib import Path

import pytest

from jarvis.config import VoiceConfig, load_config
from jarvis.core import AgentLoop, FakeClient, build_system, text_message, tool_use_message
from jarvis.core.client import ToolCall
from jarvis.permissions import PermissionGate, Policy
from jarvis.tools import ToolContext, ToolExecutor, ToolRegistry
from jarvis.voice import (
    FakeCapture,
    FakeTranscriber,
    PushToTalkListener,
    VoiceApprover,
    VoiceSession,
    wake_active,
)


class _Output:
    def on_heard(self, text: str) -> None: ...
    def __call__(self, event) -> None: ...
    async def on_result(self, result) -> None: ...


def _session(tmp_path: Path, *, transcripts, client, approver=None) -> VoiceSession:
    cfg = load_config(root=tmp_path, env_file=None)
    reg = ToolRegistry()
    reg.discover("jarvis.tools.builtin", ToolContext(config=cfg))
    loop = AgentLoop(
        client=client,
        registry=reg,
        executor=ToolExecutor(),
        gate=PermissionGate(Policy(), tmp_path),
        config=cfg,
        approver=approver or VoiceApprover(None),
        system=build_system(voice=True),
    )
    return VoiceSession(
        loop=loop, stt=FakeTranscriber(scripted=list(transcripts)), output=_Output()
    )


# --- one-utterance scope + observable state ---------------------------------


async def test_listen_once_is_one_utterance_and_returns_to_idle(tmp_path: Path) -> None:
    capture = FakeCapture(scripted=[b"audio"])
    session = _session(
        tmp_path, transcripts=["what time is it"], client=FakeClient([text_message("Noon.")])
    )
    states: list[str] = []
    listener = PushToTalkListener(capture, session, on_state=states.append)
    result = await listener.listen_once()
    assert result is not None and result.text == "Noon."
    assert capture.calls == 1  # exactly one utterance captured
    assert listener.state == "idle"  # returned to idle (no indefinite window)
    assert states[0] == "listening" and states[-1] == "idle"  # observable transitions


async def test_silence_captures_no_turn(tmp_path: Path) -> None:
    capture = FakeCapture(scripted=[b""])  # silence
    client = FakeClient([text_message("unused")])
    listener = PushToTalkListener(capture, _session(tmp_path, transcripts=["x"], client=client))
    assert await listener.listen_once() is None
    assert client.responses  # no turn ran


# --- no unattended mic ------------------------------------------------------


async def test_unattended_capture_is_refused(tmp_path: Path) -> None:
    capture = FakeCapture(scripted=[b"audio"])
    listener = PushToTalkListener(
        capture,
        _session(tmp_path, transcripts=["x"], client=FakeClient([text_message("y")])),
        attended=False,
    )
    with pytest.raises(RuntimeError, match="attended"):
        await listener.listen_once()
    assert capture.calls == 0  # the mic was never opened


# --- wake activation deferred -----------------------------------------------


def test_wake_activation_is_deferred() -> None:
    # Even with a wake word configured, activation is off in the MVP (D6/ADR-0007).
    assert wake_active(VoiceConfig()) is False
    assert wake_active(VoiceConfig(wake_word="kira")) is False


async def test_spurious_wake_commits_nothing(tmp_path: Path) -> None:
    # If a trigger (a wake, once activated, or a mis-press) fires one activation whose
    # audio transcribes to a risky command, it still commits nothing: one utterance, and
    # the risky action escalates + is denied (no screen). This is the T8 safety property.
    approver = VoiceApprover(None)
    capture = FakeCapture(scripted=[b"ambient-audio"])
    client = FakeClient(
        [
            tool_use_message([ToolCall("t1", "run_shell", {"command": "rm -rf /"})]),
            text_message("That needs on-screen confirmation."),
        ]
    )
    session = _session(
        tmp_path, transcripts=["delete everything"], client=client, approver=approver
    )
    listener = PushToTalkListener(capture, session)
    await listener.listen_once()
    assert capture.calls == 1  # exactly one utterance — no indefinite window
    assert approver.escalations == 1 and approver.denied == 1  # risky action denied, not run
    assert listener.state == "idle"
