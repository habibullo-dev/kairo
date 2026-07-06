"""REPL delegation wiring (Phase 6, Task 6): service composition, the forwarding
sub-agent approver (labeled, pattern-grant, never-persist, lock-serialized), the
long-prompt pager on the spawn approval, and the `agents` command. Keyless.
"""

from __future__ import annotations

import asyncio
import builtins
import io
import time
from pathlib import Path

import pytest
from rich.console import Console

from jarvis.agents import AgentRunStore
from jarvis.cli.repl import Repl
from jarvis.config import load_config
from jarvis.core import FakeClient, text_message
from jarvis.core.client import ToolCall
from jarvis.core.prompts import DELEGATION_GUIDANCE
from jarvis.permissions import PermissionGate, Policy, SubAgentGate
from jarvis.permissions.gate import Decision
from jarvis.persistence import SessionStore
from jarvis.persistence.db import connect
from jarvis.tools.base import Permission

_OPEN: list = []


@pytest.fixture(autouse=True)
async def _close_dbs():
    yield
    while _OPEN:
        await _OPEN.pop().close()


def _console() -> tuple[Console, io.StringIO]:
    buf = io.StringIO()
    return Console(file=buf, force_terminal=False, width=200), buf


async def _repl(
    tmp_path: Path, *, with_delegation: bool = True
) -> tuple[Repl, io.StringIO, object]:
    config = load_config(root=tmp_path, env_file=None)
    console, buf = _console()
    db = await connect(tmp_path / "db.db")
    _OPEN.append(db)
    lock = asyncio.Lock()
    store = SessionStore(db, lock)
    sid = await store.create_session()
    run_store = AgentRunStore(db, lock) if with_delegation else None
    repl = Repl(
        config,
        client=FakeClient([text_message("done")]),
        console=console,
        store=store,
        session_id=sid,
        run_store=run_store,
    )
    return repl, buf, run_store


# --- composition -------------------------------------------------------------


async def test_delegation_wired_registers_tool_and_guidance(tmp_path: Path) -> None:
    repl, _buf, _rs = await _repl(tmp_path, with_delegation=True)
    assert repl.agents is not None
    assert "spawn_agent" in repl.registry  # the tool is in the model's schema
    assert DELEGATION_GUIDANCE in repl.loop.system  # parent guidance present
    assert repl.agents.bound_session_id == repl.session_id
    # the service's event sink is the REPL's (so child events render + cost accrues)
    assert repl.agents.emit == repl._agent_event


async def test_no_delegation_when_disabled(tmp_path: Path) -> None:
    repl, _buf, _rs = await _repl(tmp_path, with_delegation=False)
    assert repl.agents is None
    assert "spawn_agent" not in repl.registry
    assert DELEGATION_GUIDANCE not in repl.loop.system


# --- the forwarding sub-agent approver ---------------------------------------


def _fake_input(answers: list[str]):
    def _inner(_prompt: str = "") -> str:
        return answers.pop(0)

    return _inner


async def test_subagent_approver_labels_and_grants_pattern(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repl, buf, _rs = await _repl(tmp_path)
    gate = SubAgentGate(
        PermissionGate(Policy(), tmp_path), scope=frozenset({"web_fetch"}), project_root=tmp_path
    )
    approver = repl._make_subagent_approver(gate, "1", "research")
    call = ToolCall("t1", "web_fetch", {"url": "https://docs.py/x"})

    monkeypatch.setattr(builtins, "input", _fake_input(["a"]))
    perm = await approver(call, Decision(Permission.ASK, "network egress"))
    assert perm is Permission.ALLOW
    out = buf.getvalue()
    assert 'sub-agent "research"' in out  # labeled as the sub-agent's ask
    assert "granted" in out and "docs.py" in out  # the pattern grant is shown

    # the grant now covers the same host without re-prompting (no input scripted)
    same = gate.check("web_fetch", {"url": "https://docs.py/other"}, tool_default=Permission.ASK)
    assert same.permission is Permission.ALLOW
    # ...and permissions.yaml was never written (grants are run-scoped, not persisted)
    assert not (tmp_path / "config" / "permissions.yaml").exists()


async def test_subagent_approver_no_grant_option_for_shell(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repl, buf, _rs = await _repl(tmp_path)
    gate = SubAgentGate(
        PermissionGate(Policy(), tmp_path), scope=frozenset({"run_shell"}), project_root=tmp_path
    )
    approver = repl._make_subagent_approver(gate, "1", "worker")
    monkeypatch.setattr(builtins, "input", _fake_input(["y"]))
    perm = await approver(
        ToolCall("t1", "run_shell", {"command": "ls"}), Decision(Permission.ASK, "shell")
    )
    assert perm is Permission.ALLOW
    assert "a-for-this-run" not in buf.getvalue()  # never offered for run_shell


async def test_subagent_approver_denies_on_no(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repl, _buf, _rs = await _repl(tmp_path)
    gate = SubAgentGate(
        PermissionGate(Policy(), tmp_path), scope=frozenset({"web_fetch"}), project_root=tmp_path
    )
    approver = repl._make_subagent_approver(gate, "1", "r")
    monkeypatch.setattr(builtins, "input", _fake_input(["n"]))
    perm = await approver(
        ToolCall("t1", "web_fetch", {"url": "https://x"}), Decision(Permission.ASK, "net")
    )
    assert perm is Permission.DENY


async def test_approval_lock_serializes_parallel_children(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Two children prompting concurrently must not interleave their input() calls.
    repl, _buf, _rs = await _repl(tmp_path)
    state = {"active": 0, "peak": 0}

    def _slow_input(_prompt: str = "") -> str:
        state["active"] += 1
        state["peak"] = max(state["peak"], state["active"])
        time.sleep(0.03)
        state["active"] -= 1
        return "y"

    monkeypatch.setattr(builtins, "input", _slow_input)
    g1 = SubAgentGate(
        PermissionGate(Policy(), tmp_path), scope=frozenset({"web_fetch"}), project_root=tmp_path
    )
    g2 = SubAgentGate(
        PermissionGate(Policy(), tmp_path), scope=frozenset({"web_fetch"}), project_root=tmp_path
    )
    a1 = repl._make_subagent_approver(g1, "1", "one")
    a2 = repl._make_subagent_approver(g2, "2", "two")
    call = ToolCall("t", "web_fetch", {"url": "https://x"})
    dec = Decision(Permission.ASK, "net")
    await asyncio.gather(a1(call, dec), a2(call, dec))
    assert state["peak"] == 1  # the approval lock kept the prompts from overlapping


# --- long-prompt pager on the spawn approval ---------------------------------


async def test_spawn_approval_pages_long_prompt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repl, buf, _rs = await _repl(tmp_path)
    long_prompt = "\n".join(f"step {i}: do the thing" for i in range(40))
    call = ToolCall(
        "s1", "spawn_agent", {"title": "big", "prompt": long_prompt, "tools": ["web_search"]}
    )
    # 'v' pages the full prompt, then 'n' declines — v must never auto-approve.
    monkeypatch.setattr(builtins, "input", _fake_input(["v", "n"]))
    perm = await repl._approve(call, Decision(Permission.ASK, "delegated execution"))
    assert perm is Permission.DENY
    out = buf.getvalue()
    assert "more line" in out  # the inline summary was truncated (long -> paged, not dumped)
    assert "step 39: do the thing" in out  # ...and 'v' revealed the full prompt


async def test_spawn_approval_view_then_yes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repl, _buf, _rs = await _repl(tmp_path)
    long_prompt = "\n".join(f"line {i}" for i in range(40))
    call = ToolCall(
        "s1", "spawn_agent", {"title": "big", "prompt": long_prompt, "tools": ["web_search"]}
    )
    monkeypatch.setattr(builtins, "input", _fake_input(["v", "y"]))
    perm = await repl._approve(call, Decision(Permission.ASK, "delegated"))
    assert perm is Permission.ALLOW


# --- the `agents` command ----------------------------------------------------


async def test_agents_command_lists_and_details(tmp_path: Path) -> None:
    repl, buf, run_store = await _repl(tmp_path)
    run_id = await run_store.begin_run(
        parent_session_id=repl.session_id,
        parent_trace_id="tr-parent",
        title="research task",
        prompt="find the answer to X",
        tools_scope=["web_search", "web_fetch"],
    )
    await run_store.complete_run(
        run_id, status="ok", child_trace_id="tr-child", iterations=3, cost_usd=0.02
    )

    await repl._show_agents("")  # list
    listed = buf.getvalue()
    assert "research task" in listed and "ok" in listed

    buf.truncate(0)
    buf.seek(0)
    await repl._show_agents(str(run_id))  # detail
    detail = buf.getvalue()
    assert "find the answer to X" in detail  # verbatim prompt
    assert "tr-parent" in detail and "tr-child" in detail  # both trace ids


async def test_agents_command_when_disabled(tmp_path: Path) -> None:
    repl, buf, _rs = await _repl(tmp_path, with_delegation=False)
    await repl._show_agents("")
    assert "not enabled" in buf.getvalue()
