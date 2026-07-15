"""Egress taint demotion for the Phase 13 research services (Task 9). A service tool is
egress=True (derived from its spec), so it composes with the Phase-9 taint pipe: once a private
read happens in a turn, an egress service that would otherwise run (an ALLOW, e.g. a persisted
'always allow') is demoted to a NON-PERSISTABLE ask the human must see. Keyless — a real
FirecrawlScrapeTool in the loop, scripted FakeClient, no network (the approver denies)."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import BaseModel

import kira.core  # noqa: F401 - load core first (ledger<->core.context import cycle in isolation)
from kira.config import Config, LimitsConfig, ModelsConfig, PathsConfig, Secrets
from kira.core import AgentLoop, FakeClient, ToolCall, text_message, tool_use_message
from kira.permissions.gate import Decision, PermissionGate
from kira.permissions.policy import Policy
from kira.services.firecrawl import FirecrawlScrapeTool
from kira.tools import Permission
from kira.tools.base import Tool, ToolContext
from kira.tools.executor import ToolExecutor
from kira.tools.registry import ToolRegistry


@pytest.fixture(autouse=True)
def _no_live_firecrawl(monkeypatch):
    # No FIRECRAWL_API_KEY ⇒ the tool short-circuits before any network (the control test runs it
    # as ALLOW); reset the injected transport so nothing leaks across tests.
    monkeypatch.delenv("FIRECRAWL_API_KEY", raising=False)
    yield
    FirecrawlScrapeTool.transport = None


class _Empty(BaseModel):
    pass


class _GmailReadTool(Tool):
    name = "gmail_read"
    description = "Read private mail (taints the turn)."
    Params = _Empty
    permission_default = Permission.ALLOW
    reads_private = True

    async def run(self, params: _Empty) -> str:
        return "private mail body"


class _Approver:
    def __init__(self, result: Permission = Permission.DENY) -> None:
        self.result = result
        self.calls: list[tuple[ToolCall, Decision]] = []

    async def __call__(self, call: ToolCall, decision: Decision) -> Permission:
        self.calls.append((call, decision))
        return self.result

    def decision_for(self, name: str) -> Decision | None:
        return next((d for c, d in self.calls if c.name == name), None)


def _config() -> Config:
    return Config(
        root=Path.cwd(), models=ModelsConfig(), limits=LimitsConfig(),
        paths=PathsConfig(), secrets=Secrets(_env_file=None),  # type: ignore[call-arg]
    )


def _loop(responses: list, approver: _Approver) -> AgentLoop:
    reg = ToolRegistry()
    reg.register(_GmailReadTool())
    # A real service tool. Grant it ALLOW via policy (a persisted "always allow firecrawl_scrape")
    # so the taint DEMOTION (ALLOW -> non-persistable ASK) is the thing under test.
    reg.register(FirecrawlScrapeTool(ToolContext(config=_config())))
    gate = PermissionGate(Policy(tools={"firecrawl_scrape": Permission.ALLOW}), Path.cwd())
    return AgentLoop(
        client=FakeClient(responses), registry=reg, executor=ToolExecutor(),
        gate=gate, config=_config(), approver=approver,
    )


def test_service_tool_is_egress() -> None:
    # The property the taint pipe keys on — derived from the spec, not hand-set.
    assert FirecrawlScrapeTool.egress is True


async def test_egress_service_allow_runs_without_a_private_read() -> None:
    # Control: no private read this turn ⇒ the ALLOW service tool is NOT demoted (approver never
    # consulted for it). (It errors at run for lack of a key, but the point is it wasn't gated.)
    approver = _Approver(result=Permission.ALLOW)
    loop = _loop(
        [tool_use_message([ToolCall("t1", "firecrawl_scrape", {"url": "https://ex.test"})]),
         text_message("done")],
        approver,
    )
    await loop.run_turn([{"role": "user", "content": "scrape it"}])
    assert approver.decision_for("firecrawl_scrape") is None  # ran as ALLOW, not demoted to ask


async def test_private_read_then_service_is_demoted_non_persistable() -> None:
    # gmail_read (private) taints the turn; the next batch's firecrawl_scrape (ALLOW) is demoted
    # to a NON-PERSISTABLE ask the human sees — the exfil pipe (silent read -> silent send) closed.
    approver = _Approver(result=Permission.DENY)
    loop = _loop(
        [
            tool_use_message([ToolCall("t1", "gmail_read", {})]),
            tool_use_message([ToolCall("t2", "firecrawl_scrape", {"url": "https://ex.test"})]),
            text_message("An egress service after a private read needs your approval."),
        ],
        approver,
    )
    await loop.run_turn([{"role": "user", "content": "read my mail then research"}])
    decision = approver.decision_for("firecrawl_scrape")
    assert decision is not None
    assert decision.permission is Permission.ASK
    assert decision.persistable is False  # cannot be "always allow"ed while private data is in play
    assert "private data" in decision.reason


async def test_same_batch_read_and_service_is_demoted() -> None:
    # The read and the egress service in ONE batch: permission for the whole batch is resolved
    # before anything runs, so the service is demoted even though the read hasn't "executed" yet.
    approver = _Approver(result=Permission.DENY)
    loop = _loop(
        [
            tool_use_message([
                ToolCall("t1", "gmail_read", {}),
                ToolCall("t2", "firecrawl_scrape", {"url": "https://ex.test"}),
            ]),
            text_message("done"),
        ],
        approver,
    )
    await loop.run_turn([{"role": "user", "content": "go"}])
    decision = approver.decision_for("firecrawl_scrape")
    assert decision is not None and decision.permission is Permission.ASK
    assert decision.persistable is False
