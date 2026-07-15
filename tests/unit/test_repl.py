"""REPL tests: approval resolution, renderer, and a full turn via FakeClient."""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from rich.console import Console

from kira.cli.render import ConsoleRenderer
from kira.cli.repl import Repl
from kira.config import load_config
from kira.core import FakeClient, ToolCall, text_message, tool_use_message
from kira.core.events import TextDelta, ToolFinished, ToolStarted, TurnCompleted
from kira.paths import resolve_path
from kira.permissions.gate import Decision
from kira.tools import Permission

ASK = Decision(Permission.ASK, "needs approval")


def _console() -> Console:
    return Console(file=io.StringIO(), force_terminal=False, width=100)


def _repl(tmp_path: Path, responses: list | None = None) -> Repl:
    config = load_config(root=tmp_path, env_file=None)
    return Repl(config, client=FakeClient(responses or []), console=_console())


def _answer(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setattr("builtins.input", lambda *_a: value)


# --- approval --------------------------------------------------------------


async def test_approve_yes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _answer(monkeypatch, "y")
    repl = _repl(tmp_path)
    perm = await repl._approve(ToolCall("t", "run_shell", {"command": "ls"}), ASK)
    assert perm is Permission.ALLOW


async def test_approve_no(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _answer(monkeypatch, "")  # empty -> No (default)
    repl = _repl(tmp_path)
    perm = await repl._approve(ToolCall("t", "run_shell", {"command": "ls"}), ASK)
    assert perm is Permission.DENY


async def test_approve_always_shell_persists_prefix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _answer(monkeypatch, "a")
    repl = _repl(tmp_path)
    perm = await repl._approve(ToolCall("t", "run_shell", {"command": "git status"}), ASK)
    assert perm is Permission.ALLOW
    assert any(r.prefix == "git status" for r in repl.gate.policy.shell.rules)


async def test_approve_always_write_persists_resolved_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _answer(monkeypatch, "always")
    repl = _repl(tmp_path)
    target = tmp_path / "exports" / "out.txt"
    await repl._approve(ToolCall("t", "write_file", {"path": str(target)}), ASK)
    # persisted as the fully-resolved parent dir (what the gate will check against),
    # never a bare relative fragment
    expected = str(resolve_path(str(target), repl.config.root).parent)
    assert expected in repl.gate.policy.filesystem.write_allowlist


async def test_approve_always_write_refuses_overbroad_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _answer(monkeypatch, "a")
    repl = _repl(tmp_path)
    before = list(repl.gate.policy.filesystem.write_allowlist)
    # a file whose parent is the drive/filesystem root — must NOT be persisted
    root_file = Path(tmp_path.anchor) / "at_root.txt"
    perm = await repl._approve(ToolCall("t", "write_file", {"path": str(root_file)}), ASK)
    assert perm is Permission.ALLOW  # the one write is still approved
    assert repl.gate.policy.filesystem.write_allowlist == before  # but nothing broadened


async def test_approve_always_other_tool_persists_tool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _answer(monkeypatch, "a")
    repl = _repl(tmp_path)
    await repl._approve(ToolCall("t", "some_tool", {}), ASK)
    assert repl.gate.policy.tools["some_tool"] is Permission.ALLOW


# --- renderer --------------------------------------------------------------


def test_renderer_handles_all_event_types() -> None:
    console = _console()
    r = ConsoleRenderer(console)
    r(TextDelta("hello "))
    r(TextDelta("world"))
    r(ToolStarted("t", "echo", {"text": "x"}))
    r(ToolFinished("t", "echo", is_error=False, preview="ok"))
    r(ToolFinished("t2", "boom", is_error=True, preview="failed"))
    r(TurnCompleted("done", "end_turn"))
    r(TurnCompleted("", "max_iterations"))
    out = console.file.getvalue()
    assert "hello world" in out
    assert "echo" in out
    assert "max tool-iteration" in out


# --- full turn -------------------------------------------------------------


async def test_run_turn_text_only(tmp_path: Path) -> None:
    repl = _repl(tmp_path, [text_message("hi there")])
    repl.messages.append({"role": "user", "content": "hello"})
    await repl.run_turn()
    assert repl.messages[-1]["role"] == "assistant"
    assert repl.usage.output_tokens == 5  # FakeClient default usage
    assert "hi there" in repl.console.file.getvalue()


async def test_run_turn_with_allowed_tool(tmp_path: Path) -> None:
    # list_dir defaults to ALLOW, so no approval is needed.
    (tmp_path / "a.py").write_text("", encoding="utf-8")
    repl = _repl(
        tmp_path,
        [
            tool_use_message([ToolCall("t1", "list_dir", {"path": str(tmp_path)})]),
            text_message("there is one file"),
        ],
    )
    repl.messages.append({"role": "user", "content": "list the dir"})
    await repl.run_turn()
    roles = [m["role"] for m in repl.messages]
    assert roles == ["user", "assistant", "user", "assistant"]
    tool_result = repl.messages[2]["content"][0]
    assert tool_result["type"] == "tool_result"
    assert "a.py" in tool_result["content"]
    out = repl.console.file.getvalue()
    assert "list_dir" in out and "there is one file" in out
