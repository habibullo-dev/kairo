"""The spawn_agent tool: registration gating, params validation, and the D8 pin —
a compromised child's out-of-scope attempt is denied AND observable in the forwarded
event stream (the load-bearing property for adversarial evals of delegation). Keyless.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from pydantic import ValidationError

from jarvis.agents import AgentRunStore, SubAgentService
from jarvis.cli.repl import Repl, _call_summary
from jarvis.config import load_config
from jarvis.core.client import ToolCall, text_message, tool_use_message
from jarvis.core.events import SubAgentEvent
from jarvis.permissions import PermissionGate, Policy
from jarvis.persistence import SessionStore
from jarvis.persistence.db import connect
from jarvis.tools import ToolContext, ToolExecutor, ToolRegistry
from jarvis.tools.builtin.agents import SpawnAgentParams, SpawnAgentTool


def _cfg(tmp_path: Path):
    return load_config(root=tmp_path, env_file=None)


# --- registration gating -----------------------------------------------------


def test_registers_only_when_a_service_is_present(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    with_service = ToolRegistry()
    with_service.discover("jarvis.tools.builtin", ToolContext(config=cfg, agents=object()))
    assert "spawn_agent" in with_service

    without = ToolRegistry()
    without.discover("jarvis.tools.builtin", ToolContext(config=cfg))
    assert "spawn_agent" not in without  # no delegation surface when disabled


def test_is_available_reflects_the_context() -> None:
    assert SpawnAgentTool.is_available(ToolContext(agents=object())) is True
    assert SpawnAgentTool.is_available(ToolContext()) is False


# --- params validation -------------------------------------------------------


def test_params_reject_empty_scope() -> None:
    with pytest.raises(ValidationError):
        SpawnAgentParams(title="t", prompt="p", tools=[])


def test_params_reject_unspawnable_tool() -> None:
    with pytest.raises(ValidationError):
        SpawnAgentParams(title="t", prompt="p", tools=["remember"])  # meta tool, not delegatable
    with pytest.raises(ValidationError):
        SpawnAgentParams(title="t", prompt="p", tools=["spawn_agent"])  # no recursion


def test_params_accept_a_valid_subset() -> None:
    p = SpawnAgentParams(title="research", prompt="find X", tools=["web_search", "web_fetch"])
    assert p.tools == ["web_search", "web_fetch"]


# --- REPL approval surface ---------------------------------------------------


def test_spawn_agent_is_never_persistable_and_shows_prompt_and_scope() -> None:
    assert "spawn_agent" in Repl._NEVER_PERSIST  # a stray "a" can't open delegation
    call = ToolCall(
        "s1",
        "spawn_agent",
        {"title": "research", "prompt": "find X and Y", "tools": ["web_search"]},
    )
    summary = _call_summary(call)
    assert "research" in summary
    assert "find X and Y" in summary  # full prompt, untruncated
    assert "web_search" in summary  # the tool scope is shown


# --- the D8 pin: out-of-scope child attempts are denied AND observable -------


class _CompromisedChild:
    """A child that tries an out-of-scope write, then reports."""

    def __init__(self) -> None:
        self._responses = [
            tool_use_message([ToolCall("t1", "write_file", {"path": "pwned.txt", "content": "x"})]),
            text_message("I was told to write a file but it isn't in my scope."),
        ]

    async def create(self, **_kw: object):
        return self._responses.pop(0)


async def _service(tmp_path: Path, client: object) -> tuple[SubAgentService, object]:
    cfg = _cfg(tmp_path)
    db = await connect(tmp_path / "db.db")
    lock = asyncio.Lock()
    registry = ToolRegistry()
    registry.discover("jarvis.tools.builtin", ToolContext(config=cfg))
    svc = SubAgentService(
        session_store=SessionStore(db, lock),
        run_store=AgentRunStore(db, lock),
        client=client,  # type: ignore[arg-type]
        executor=ToolExecutor(),
        gate=PermissionGate(Policy(), tmp_path),
        config=cfg,
    )
    svc.bind(registry=registry)
    return svc, db


async def test_out_of_scope_child_attempt_denied_and_visible(tmp_path: Path) -> None:
    svc, db = await _service(tmp_path, _CompromisedChild())
    events: list[object] = []
    svc.emit = events.append
    tool = SpawnAgentTool(ToolContext(config=_cfg(tmp_path), agents=svc))
    try:
        # scope grants read_file only; the child tries write_file
        result = await tool.run(SpawnAgentParams(title="c", prompt="p", tools=["read_file"]))
        assert isinstance(result.content, str)
        # the write NEVER happened (no side effect)
        assert not (tmp_path / "pwned.txt").exists()
        # ...but the ATTEMPT is observable in the forwarded stream (out-of-scope tool
        # -> the child loop's unknown-tool path emits a denied ToolDecision).
        decisions = [
            e.inner
            for e in events
            if isinstance(e, SubAgentEvent) and type(e.inner).__name__ == "ToolDecision"
        ]
        assert any(d.name == "write_file" and d.resolution == "deny" for d in decisions)
    finally:
        await db.close()
