"""Visible voice round-trip in the UI (Phase 8) — captions never bypass the privacy rules.

The UI mirrors the voice turn to the browser (heard transcript + a safe caption), but the
caption is *exactly* the base renderer's post-privacy output — so it can't leak a raw answer,
a command, a payload, or the particulars of a risky action. The heard transcript is masked,
the escalation caption is category-only, mid-turn events aren't mirrored (calm), and the
transcript still enters the model framed as untrusted input.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from jarvis.config import load_config
from jarvis.core import AgentLoop, FakeClient, build_system, text_message
from jarvis.core.client import ToolCall
from jarvis.core.events import ToolStarted
from jarvis.permissions import PermissionGate, Policy
from jarvis.permissions.gate import Decision
from jarvis.tools import Permission, ToolContext, ToolExecutor, ToolRegistry
from jarvis.ui.voice import UiVoiceRenderer
from jarvis.voice import FakeSynthesizer, FakeTranscriber, VoiceApprover, VoiceSession

ASK = Decision(Permission.ASK, "needs approval")


class _Conns:
    """Duck-typed ConnectionManager: records what would be pushed to the browser."""

    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def broadcast(self, message: dict) -> None:
        self.sent.append(message)


def _renderer() -> tuple[UiVoiceRenderer, _Conns]:
    conns = _Conns()
    return UiVoiceRenderer(FakeSynthesizer(), conns), conns


def _voice(msgs: list[dict], role: str) -> list[dict]:
    return [m for m in msgs if m.get("kind") == "voice" and m.get("role") == role]


# --- captions go through the renderer's privacy rules -----------------------


async def test_heard_transcript_is_mirrored_and_masked() -> None:
    r, conns = _renderer()
    await r.on_heard("my api key is sk-abcdef1234567890 keep it safe")
    heard = _voice(conns.sent, "heard")
    assert len(heard) == 1
    assert "sk-abcdef1234567890" not in heard[0]["text"]  # secret masked even on the screen echo
    assert "[redacted]" in heard[0]["text"]
    assert r.spoken[-1].startswith("I heard:")  # base still speaks the audio echo


async def test_reply_caption_equals_the_safe_spoken_text() -> None:
    r, conns = _renderer()
    await r.on_result(SimpleNamespace(text="The token is sk-DEADBEEF12345678 — noted."))
    reply = _voice(conns.sent, "reply")
    assert len(reply) == 1
    # the caption IS the post-mask/cap text that reached TTS — not the raw answer
    assert reply[0]["text"] == r.spoken[-1]
    assert "sk-DEADBEEF12345678" not in reply[0]["text"] and "[redacted]" in reply[0]["text"]


async def test_long_answer_caption_is_capped_like_speech() -> None:
    r, conns = _renderer()
    await r.on_result(SimpleNamespace(text="x" * 5000))
    reply = _voice(conns.sent, "reply")[0]["text"]
    assert (
        reply.endswith("the rest is on screen.") and len(reply) < 1000
    )  # capped, not the full 5000


async def test_escalation_caption_is_category_only_no_payload() -> None:
    r, conns = _renderer()
    call = ToolCall("c1", "write_file", {"path": "/etc/passwd", "content": "TOPSECRET-PAYLOAD"})
    await r.announce_escalation(call, ASK)
    reply = _voice(conns.sent, "reply")[0]["text"]
    assert "write a file" in reply and "on screen" in reply  # category + where
    assert "TOPSECRET-PAYLOAD" not in reply and "/etc/passwd" not in reply  # never the input


async def test_mid_turn_events_are_not_mirrored() -> None:
    # Calm: the tool/sub-agent firehose is NOT pushed to the UI — only heard + safe caption.
    r, conns = _renderer()
    r(ToolStarted("t1", "read_file", {"path": "secret.txt"}))  # the no-op event sink
    assert conns.sent == []


# --- the transcript still enters the model framed untrusted -----------------


def _loop(tmp_path: Path, client) -> AgentLoop:
    cfg = load_config(root=tmp_path, env_file=None)
    reg = ToolRegistry()
    reg.discover("jarvis.tools.builtin", ToolContext(config=cfg))
    return AgentLoop(
        client=client,
        registry=reg,
        executor=ToolExecutor(),
        gate=PermissionGate(Policy(), tmp_path),
        config=cfg,
        approver=VoiceApprover(None),
        system=build_system(voice=True),
    )


async def test_ui_voice_frames_transcript_untrusted_and_mirrors_roundtrip(tmp_path: Path) -> None:
    client = FakeClient([text_message("The answer is 42.")])
    renderer, conns = _renderer()
    session = VoiceSession(
        loop=_loop(tmp_path, client),
        stt=FakeTranscriber(scripted=["what is the answer"]),
        output=renderer,
    )
    result = await session.handle_audio(b"audio")
    assert result is not None and result.text == "The answer is 42."
    # 1) the model received the transcript wrapped as UNTRUSTED content (not raw)
    sent = client.calls[0]["messages"][0]["content"]
    assert "begin transcript (untrusted)" in sent and "what is the answer" in sent
    # 2) the browser saw the heard transcript + the safe reply caption (the round-trip)
    assert _voice(conns.sent, "heard")[0]["text"] == "what is the answer"
    assert _voice(conns.sent, "reply")[0]["text"] == "The answer is 42."
