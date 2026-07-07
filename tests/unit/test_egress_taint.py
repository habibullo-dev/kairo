"""Egress & taint substrate — the Phase 9 Checkpoint A safety surface (amendment A2).

The load-bearing property: once a tool that reads private data (mail/calendar/drive) runs in
a turn, any egress tool (web fetch/search, a draft, a notification) can no longer run
*silently* — its ALLOW is demoted to a non-persistable ASK the human must see. This closes the
"silent mail read → silent web_fetch exfil" pipe structurally, before any connector exists.

Covered here: the taint matrix (agent loop), the raw-gate-verdict event, per-turn reset, and
the egress ledger (A5). The unattended demotion, the sensitive-shell floor, the glob/list
redaction, and the UI/REPL "always" suppression are pinned in their own suites.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel

from jarvis.config import Config, LimitsConfig, ModelsConfig, PathsConfig, Secrets
from jarvis.core import AgentLoop, FakeClient, ToolCall, text_message, tool_use_message
from jarvis.core.events import Event, ToolDecision
from jarvis.permissions import PermissionGate, Policy
from jarvis.permissions.gate import Decision
from jarvis.tools import Permission, Tool, ToolExecutor, ToolRegistry

ALLOW, ASK, DENY = Permission.ALLOW, Permission.ASK, Permission.DENY


class _Empty(BaseModel):
    pass


class PrivateReadTool(Tool):
    name = "read_mail"
    description = "Read private mail."
    Params = _Empty
    permission_default = Permission.ALLOW
    reads_private = True

    async def run(self, params: _Empty) -> str:
        return "private mail contents"


class EgressTool(Tool):
    name = "send_out"
    description = "Send data off-box."
    Params = _Empty
    permission_default = Permission.ALLOW
    egress = True

    async def run(self, params: _Empty) -> str:
        return "SENT"


class PlainAllowTool(Tool):
    name = "echo"
    description = "A harmless allow tool."
    Params = _Empty
    permission_default = Permission.ALLOW

    async def run(self, params: _Empty) -> str:
        return "echoed"


class RecordingApprover:
    """Captures every (call, decision) it sees; returns a fixed permission."""

    def __init__(self, result: Permission = Permission.DENY) -> None:
        self.result = result
        self.calls: list[tuple[ToolCall, Decision]] = []

    async def __call__(self, call: ToolCall, decision: Decision) -> Permission:
        self.calls.append((call, decision))
        return self.result

    def decision_for(self, name: str) -> Decision | None:
        for call, decision in self.calls:
            if call.name == name:
                return decision
        return None


def _config() -> Config:
    return Config(
        root=Path.cwd(),
        models=ModelsConfig(),
        limits=LimitsConfig(),
        paths=PathsConfig(),
        secrets=Secrets(_env_file=None),  # type: ignore[call-arg]
    )


def _loop(responses: list, approver: RecordingApprover) -> AgentLoop:
    reg = ToolRegistry()
    reg.register(PrivateReadTool())
    reg.register(EgressTool())
    reg.register(PlainAllowTool())
    return AgentLoop(
        client=FakeClient(responses),
        registry=reg,
        executor=ToolExecutor(),
        gate=PermissionGate(Policy(), Path.cwd()),
        config=_config(),
        approver=approver,
    )


def _user() -> list[dict]:
    return [{"role": "user", "content": "go"}]


def _events(collected: list[Event]):
    return lambda e: collected.append(e)


# --- the taint matrix ------------------------------------------------------


async def test_untainted_egress_runs_without_asking() -> None:
    # No private read this turn: an egress ALLOW runs directly, approver never consulted.
    approver = RecordingApprover()
    loop = _loop(
        [tool_use_message([ToolCall("t1", "send_out", {})]), text_message("done")],
        approver,
    )
    result = await loop.run_turn(_user())
    assert approver.calls == []  # egress ALLOW was not demoted
    assert result.stop_reason == "end_turn"


async def test_private_read_then_egress_next_batch_is_demoted() -> None:
    # Cross-batch: read runs in batch 1, egress in batch 2 → egress becomes a non-persistable
    # ASK the approver sees (and here denies).
    approver = RecordingApprover(result=DENY)
    loop = _loop(
        [
            tool_use_message([ToolCall("t1", "read_mail", {})]),
            tool_use_message([ToolCall("t2", "send_out", {})]),
            text_message("done"),
        ],
        approver,
    )
    await loop.run_turn(_user())
    decision = approver.decision_for("send_out")
    assert decision is not None
    assert decision.permission is ASK
    assert decision.persistable is False
    assert "private data" in decision.reason


async def test_same_batch_read_and_egress_is_demoted() -> None:
    # Same batch (model emits both at once): the egress is still demoted — permission for the
    # whole batch is resolved before either executes, so we must taint on batch membership.
    approver = RecordingApprover(result=DENY)
    loop = _loop(
        [
            tool_use_message([ToolCall("t1", "read_mail", {}), ToolCall("t2", "send_out", {})]),
            text_message("done"),
        ],
        approver,
    )
    await loop.run_turn(_user())
    decision = approver.decision_for("send_out")
    assert decision is not None and decision.permission is ASK and decision.persistable is False


async def test_same_batch_demotion_is_order_independent() -> None:
    # Egress listed BEFORE the read in the batch → still demoted.
    approver = RecordingApprover(result=DENY)
    loop = _loop(
        [
            tool_use_message([ToolCall("t1", "send_out", {}), ToolCall("t2", "read_mail", {})]),
            text_message("done"),
        ],
        approver,
    )
    await loop.run_turn(_user())
    assert approver.decision_for("send_out") is not None  # demoted despite appearing first


async def test_taint_is_per_turn_not_per_session() -> None:
    # A fresh turn with no private read does not demote egress, even on a reused loop.
    approver = RecordingApprover(result=ALLOW)
    loop = _loop(
        [
            tool_use_message([ToolCall("t1", "read_mail", {})]),
            text_message("turn one done"),
            tool_use_message([ToolCall("t2", "send_out", {})]),
            text_message("turn two done"),
        ],
        approver,
    )
    await loop.run_turn(_user())  # turn 1 taints
    assert loop._turn_tainted is True
    await loop.run_turn(_user())  # turn 2 starts clean
    assert approver.calls == []  # egress not demoted in the fresh turn


async def test_non_egress_allow_not_demoted_when_tainted() -> None:
    # A plain ALLOW tool (not egress) is never demoted, even in a tainted turn.
    approver = RecordingApprover(result=DENY)
    loop = _loop(
        [
            tool_use_message([ToolCall("t1", "read_mail", {}), ToolCall("t2", "echo", {})]),
            text_message("done"),
        ],
        approver,
    )
    await loop.run_turn(_user())
    assert approver.decision_for("echo") is None  # echo ran without asking


async def test_demoted_event_keeps_raw_gate_verdict() -> None:
    # The ToolDecision event records the RAW gate verdict (allow) as gate_decision and the
    # post-approver result (deny) as resolution — evals must see the gate said allow.
    approver = RecordingApprover(result=DENY)
    events: list[Event] = []
    loop = _loop(
        [
            tool_use_message([ToolCall("t1", "read_mail", {})]),
            tool_use_message([ToolCall("t2", "send_out", {})]),
            text_message("done"),
        ],
        approver,
    )
    await loop.run_turn(_user(), on_event=_events(events))
    egress_events = [e for e in events if isinstance(e, ToolDecision) and e.name == "send_out"]
    assert egress_events, "an egress ToolDecision must be emitted"
    assert egress_events[-1].gate_decision == "allow"  # raw gate verdict, pre-taint
    assert egress_events[-1].resolution == "deny"  # after the human denied the demoted ASK


async def test_reads_private_default_false_on_ordinary_tools() -> None:
    assert PlainAllowTool.reads_private is False
    assert PlainAllowTool.egress is False
