"""VoiceSession — the headless voice loop (Phase 7, Task 3), driven by fakes.

Pins the session's contract without any audio/network: finalized-only transcripts drive
a turn, the transcript is framed untrusted, read-only holds (no escalation) while a risky
action escalates and (no screen) is denied, and a cancel resets state + re-raises.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from jarvis.config import load_config
from jarvis.core import AgentLoop, FakeClient, build_system, text_message, tool_use_message
from jarvis.core.client import ToolCall
from jarvis.permissions import PermissionGate, Policy
from jarvis.tools import ToolContext, ToolExecutor, ToolRegistry
from jarvis.voice import FakeTranscriber, Transcript, VoiceApprover, VoiceSession


class _Output:
    """A VoiceOutput double: records the heard text, mid-turn events, and results."""

    def __init__(self) -> None:
        self.heard: list[str] = []
        self.events: list = []
        self.results: list = []

    def on_heard(self, text: str) -> None:
        self.heard.append(text)

    def __call__(self, event) -> None:
        self.events.append(event)

    async def on_result(self, result) -> None:
        self.results.append(result)


def _loop(tmp_path: Path, client, approver, project=None) -> AgentLoop:
    cfg = load_config(root=tmp_path, env_file=None)
    reg = ToolRegistry()
    reg.discover("jarvis.tools.builtin", ToolContext(config=cfg))
    return AgentLoop(
        client=client,
        registry=reg,
        executor=ToolExecutor(),
        gate=PermissionGate(Policy(), tmp_path),
        config=cfg,
        approver=approver,
        system=build_system(voice=True),
        project=project,
    )


def _session(tmp_path: Path, *, transcripts, client, approver=None, output=None) -> VoiceSession:
    approver = approver or VoiceApprover(None)
    return VoiceSession(
        loop=_loop(tmp_path, client, approver),
        stt=FakeTranscriber(scripted=list(transcripts)),
        output=output or _Output(),
    )


# --- finalized-only + framing -----------------------------------------------


async def test_finalized_utterance_drives_one_turn(tmp_path: Path) -> None:
    out = _Output()
    session = _session(
        tmp_path,
        transcripts=["what is 2 plus 2"],
        client=FakeClient([text_message("Four.")]),
        output=out,
    )
    result = await session.handle_audio(b"audio")
    assert result is not None and result.text == "Four."
    assert out.heard == ["what is 2 plus 2"]  # echoed before acting
    assert len(out.results) == 1  # spoke the summary once
    assert session.state == "idle"
    # the transcript entered as untrusted content, not a bare instruction
    assert "untrusted" in session.messages[0]["content"]
    assert "what is 2 plus 2" in session.messages[0]["content"]


async def test_empty_utterance_skips_no_turn(tmp_path: Path) -> None:
    client = FakeClient([text_message("unused")])
    session = _session(tmp_path, transcripts=[""], client=client)
    assert await session.handle_audio(b"silence") is None
    assert session.state == "idle"
    assert client.responses  # run_turn never ran — the scripted response is untouched


async def test_partial_transcript_never_drives_a_turn(tmp_path: Path) -> None:
    class _Partial:
        async def transcribe(self, audio: bytes) -> Transcript:
            return Transcript(text="delete everything", is_final=False)  # not endpointed

    client = FakeClient([text_message("unused")])
    session = VoiceSession(
        loop=_loop(tmp_path, client, VoiceApprover(None)), stt=_Partial(), output=_Output()
    )
    assert await session.handle_audio(b"partial") is None
    assert client.responses  # a partial never reaches a tool or the model


# --- read-only holds vs risky escalates -------------------------------------


async def test_read_only_holds_no_escalation(tmp_path: Path) -> None:
    (tmp_path / "notes.txt").write_text("the meeting is at noon", encoding="utf-8")
    approver = VoiceApprover(None)
    client = FakeClient(
        [
            tool_use_message([ToolCall("t1", "read_file", {"path": "notes.txt"})]),
            text_message("The meeting is at noon."),
        ]
    )
    session = _session(
        tmp_path, transcripts=["when is the meeting"], client=client, approver=approver
    )
    result = await session.handle_audio(b"audio")
    assert result is not None and "noon" in result.text
    assert approver.escalations == 0  # a read-only tool never escalates


async def test_risky_action_escalates_and_denies_without_screen(tmp_path: Path) -> None:
    approver = VoiceApprover(None)  # no screen ⇒ fail-closed deny
    client = FakeClient(
        [
            tool_use_message([ToolCall("t1", "run_shell", {"command": "rm -rf /tmp/x"})]),
            text_message("I couldn't run that — it needs on-screen confirmation."),
        ]
    )
    session = _session(
        tmp_path, transcripts=["delete the temp files"], client=client, approver=approver
    )
    result = await session.handle_audio(b"audio")
    assert result is not None
    assert approver.escalations == 1  # the risky action escalated
    assert approver.denied == 1  # ...and was denied (no screen) — never committed by voice
    # the run_shell call became an is_error (denied) result — it never executed
    tool_results = [
        b
        for m in result.messages
        if isinstance(m.get("content"), list)
        for b in m["content"]
        if isinstance(b, dict) and b.get("type") == "tool_result"
    ]
    assert tool_results and all(b["is_error"] for b in tool_results)


# --- cancel / barge-in ------------------------------------------------------


async def test_cancel_resets_state_and_reraises(tmp_path: Path) -> None:
    class _Hang:
        async def create(self, **_kw):
            await asyncio.Event().wait()
            return text_message("unreachable")

    session = _session(tmp_path, transcripts=["think hard about this"], client=_Hang())
    task = asyncio.create_task(session.handle_audio(b"audio"))
    await asyncio.sleep(0.05)  # let it reach the hanging model call
    task.cancel()
    try:
        await task
        raise AssertionError("expected CancelledError")
    except asyncio.CancelledError:
        pass
    assert session.state == "idle"  # state reset even on barge-in


# --- project binding (Phase 10 A3) ------------------------------------------


async def test_voice_announces_active_project_at_turn_start(tmp_path: Path) -> None:
    # A3: a voice turn announces its scope (project name, or None for global) before acting,
    # carrying ONLY the name — never content.
    from jarvis.projects import GLOBAL, ProjectContext

    announced: list = []
    ctx = ProjectContext(project_id=5, name="Beacon", repos=(), system_extra="x")
    session = VoiceSession(
        loop=_loop(
            tmp_path, FakeClient([text_message("done"), text_message("done")]), VoiceApprover(None)
        ),
        stt=FakeTranscriber(scripted=["do the thing", "do more"]),
        output=_Output(),
        project=lambda: ctx,
        on_project=announced.append,
    )
    await session.handle_audio(b"audio")
    assert announced == ["Beacon"]  # name only

    # Global fallback: no project set ⇒ announce None (never crashes, never leaks).
    session.project = lambda: GLOBAL
    await session.handle_audio(b"audio")
    assert announced == ["Beacon", None]


async def test_voice_writes_bind_to_turn_project_not_post_switch(tmp_path: Path) -> None:
    # A3: the loop snapshots the active project per turn, so a switch between turns applies
    # to the NEXT turn — a memory/tool write can't land in a project selected after the turn
    # it ran in. We assert via the system prompt the loop actually used each turn.
    from jarvis.projects import ProjectContext

    systems: list[str] = []

    class _Recording(FakeClient):
        async def create(self, *, system: str, **kw):
            systems.append(system)
            return await super().create(system=system, **kw)

    scope = {"ctx": ProjectContext(project_id=1, name="First", repos=(), system_extra="proj:First")}
    provider = lambda: scope["ctx"]  # noqa: E731 - the shared provider (loop + session), as in prod
    session = VoiceSession(
        loop=_loop(
            tmp_path,
            _Recording([text_message("a"), text_message("b")]),
            VoiceApprover(None),
            project=provider,
        ),
        stt=FakeTranscriber(scripted=["turn one", "turn two"]),
        output=_Output(),
        project=provider,
    )
    await session.handle_audio(b"a")
    assert "proj:First" in systems[-1]
    # Switch AFTER the first turn — the second turn must reflect the new scope, the first
    # turn's write stays attributed to First.
    scope["ctx"] = ProjectContext(project_id=2, name="Second", repos=(), system_extra="proj:Second")
    await session.handle_audio(b"b")
    assert "proj:Second" in systems[-1]
    assert "proj:First" in systems[0]  # first turn was First, unchanged
