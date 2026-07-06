"""End-to-end agent-loop tests against a scripted FakeClient (no network)."""

from __future__ import annotations

from pathlib import Path

import structlog
from pydantic import BaseModel

from jarvis.config import Config, LimitsConfig, ModelsConfig, PathsConfig, Secrets
from jarvis.core import (
    AgentLoop,
    FakeClient,
    ToolCall,
    ToolFinished,
    ToolStarted,
    TurnCompleted,
    text_message,
    tool_use_message,
)
from jarvis.core.events import Event, ToolDecision
from jarvis.observability.cost import Usage
from jarvis.permissions import PermissionGate, Policy
from jarvis.tools import Permission, Tool, ToolExecutor, ToolRegistry

# --- fixtures / tools ------------------------------------------------------


class EchoParams(BaseModel):
    text: str


class EchoTool(Tool):
    name = "echo"
    description = "Echo text."
    Params = EchoParams
    permission_default = Permission.ALLOW

    async def run(self, params: EchoParams) -> str:
        return params.text


class Empty(BaseModel):
    pass


class DangerTool(Tool):
    name = "danger"
    description = "Needs approval."
    Params = Empty
    permission_default = Permission.ASK

    async def run(self, params: Empty) -> str:
        return "did the dangerous thing"


class BoomTool(Tool):
    name = "boom"
    description = "Raises."
    Params = Empty
    permission_default = Permission.ALLOW

    async def run(self, params: Empty) -> str:
        raise RuntimeError("nope")


def make_config(**limit_overrides: object) -> Config:
    return Config(
        root=Path.cwd(),
        models=ModelsConfig(),
        limits=LimitsConfig(**limit_overrides),
        paths=PathsConfig(),
        secrets=Secrets(_env_file=None),  # type: ignore[call-arg]
    )


def build_loop(
    responses: list,
    *,
    approver=None,
    config: Config | None = None,
    context_manager=None,
    memory=None,
) -> AgentLoop:
    reg = ToolRegistry()
    reg.register(EchoTool())
    reg.register(DangerTool())
    reg.register(BoomTool())
    return AgentLoop(
        client=FakeClient(responses),
        registry=reg,
        executor=ToolExecutor(),
        gate=PermissionGate(Policy(), Path.cwd()),
        config=config or make_config(),
        approver=approver,
        context_manager=context_manager,
        memory=memory,
    )


def user(text: str) -> list[dict]:
    return [{"role": "user", "content": text}]


async def allow(_c: ToolCall, _d: object) -> Permission:
    return Permission.ALLOW


async def deny(_c: ToolCall, _d: object) -> Permission:
    return Permission.DENY


# --- basic flows -----------------------------------------------------------


async def test_text_only_turn() -> None:
    loop = build_loop([text_message("hello there")])
    result = await loop.run_turn(user("hi"))
    assert result.text == "hello there"
    assert result.stop_reason == "end_turn"
    assert result.iterations == 1
    assert [m["role"] for m in result.messages] == ["user", "assistant"]


async def test_single_tool_then_answer() -> None:
    loop = build_loop(
        [
            tool_use_message([ToolCall("t1", "echo", {"text": "HI"})]),
            text_message("done"),
        ]
    )
    result = await loop.run_turn(user("say HI"))
    assert result.text == "done"
    assert result.iterations == 2

    # user, assistant(tool_use), user(tool_result), assistant(text)
    roles = [m["role"] for m in result.messages]
    assert roles == ["user", "assistant", "user", "assistant"]
    tool_result = result.messages[2]["content"][0]
    assert tool_result["type"] == "tool_result"
    assert tool_result["tool_use_id"] == "t1"
    assert tool_result["content"] == "HI"
    assert tool_result["is_error"] is False


async def test_tool_result_fed_back_to_model() -> None:
    loop = build_loop(
        [tool_use_message([ToolCall("t1", "echo", {"text": "X"})]), text_message("ok")]
    )
    await loop.run_turn(user("go"))
    # The second model call must have seen the tool_result in its messages.
    second_call_messages = loop.client.calls[1]["messages"]
    assert second_call_messages[-1]["content"][0]["type"] == "tool_result"


async def test_assistant_blocks_appended_verbatim() -> None:
    call_block = tool_use_message([ToolCall("t1", "echo", {"text": "X"})])
    loop = build_loop([call_block, text_message("ok")])
    result = await loop.run_turn(user("go"))
    # The assistant turn is the response's content blocks, unchanged.
    assert result.messages[1]["content"] == call_block.content_blocks


# --- parallel tools --------------------------------------------------------


async def test_parallel_tools_one_result_each_in_order() -> None:
    loop = build_loop(
        [
            tool_use_message(
                [
                    ToolCall("a", "echo", {"text": "A"}),
                    ToolCall("b", "echo", {"text": "B"}),
                ]
            ),
            text_message("both done"),
        ]
    )
    result = await loop.run_turn(user("go"))
    results_turn = result.messages[2]["content"]
    assert [r["tool_use_id"] for r in results_turn] == ["a", "b"]
    assert [r["content"] for r in results_turn] == ["A", "B"]


# --- errors, denials, unknown tools become results -------------------------


async def test_tool_error_becomes_result_not_crash() -> None:
    loop = build_loop([tool_use_message([ToolCall("t1", "boom", {})]), text_message("recovered")])
    result = await loop.run_turn(user("go"))
    assert result.text == "recovered"
    err = result.messages[2]["content"][0]
    assert err["is_error"] is True
    assert "RuntimeError" in err["content"]


async def test_ask_denied_becomes_result() -> None:
    loop = build_loop(
        [tool_use_message([ToolCall("t1", "danger", {})]), text_message("understood")],
        approver=deny,
    )
    result = await loop.run_turn(user("do danger"))
    blk = result.messages[2]["content"][0]
    assert blk["is_error"] is True
    assert "Denied" in blk["content"]
    assert result.text == "understood"


async def test_ask_approved_runs_tool() -> None:
    loop = build_loop(
        [tool_use_message([ToolCall("t1", "danger", {})]), text_message("ok")],
        approver=allow,
    )
    result = await loop.run_turn(user("do danger"))
    assert result.messages[2]["content"][0]["content"] == "did the dangerous thing"


async def test_ask_without_approver_defaults_deny() -> None:
    loop = build_loop(
        [tool_use_message([ToolCall("t1", "danger", {})]), text_message("ok")],
        approver=None,
    )
    result = await loop.run_turn(user("do danger"))
    assert result.messages[2]["content"][0]["is_error"] is True


async def test_approver_receives_call_and_decision() -> None:
    seen: dict = {}

    async def recording_approver(call: ToolCall, decision: object) -> Permission:
        seen["name"] = call.name
        seen["permission"] = decision.permission  # type: ignore[attr-defined]
        return Permission.DENY

    loop = build_loop(
        [tool_use_message([ToolCall("t1", "danger", {})]), text_message("x")],
        approver=recording_approver,
    )
    await loop.run_turn(user("go"))
    assert seen["name"] == "danger"
    assert seen["permission"] is Permission.ASK


async def test_unknown_tool_becomes_result() -> None:
    loop = build_loop([tool_use_message([ToolCall("t1", "ghost", {})]), text_message("noted")])
    result = await loop.run_turn(user("go"))
    blk = result.messages[2]["content"][0]
    assert blk["is_error"] is True
    assert "Unknown tool: ghost" in blk["content"]


# --- guards & bookkeeping --------------------------------------------------


async def test_max_iterations_guard() -> None:
    responses = [tool_use_message([ToolCall(f"t{i}", "echo", {"text": "x"})]) for i in range(3)]
    loop = build_loop(responses, config=make_config(max_iterations=3))
    result = await loop.run_turn(user("loop forever"))
    assert result.stop_reason == "max_iterations"
    assert result.iterations == 3
    assert len(loop.client.calls) == 3


async def test_usage_accumulates_across_calls() -> None:
    loop = build_loop(
        [
            tool_use_message(
                [ToolCall("t1", "echo", {"text": "x"})],
                usage=Usage(input_tokens=100, output_tokens=10),
            ),
            text_message("done", usage=Usage(input_tokens=50, output_tokens=20)),
        ]
    )
    result = await loop.run_turn(user("go"))
    assert result.usage.input_tokens == 150
    assert result.usage.output_tokens == 30


async def test_caller_messages_not_mutated() -> None:
    loop = build_loop([text_message("hi")])
    original = user("hello")
    await loop.run_turn(original)
    assert len(original) == 1  # loop worked on a copy


async def test_tool_use_with_no_blocks_ends_turn() -> None:
    # Defensive: stop_reason=tool_use but no tool_use blocks -> treat as terminal.
    from jarvis.core.client import ModelResponse

    weird = ModelResponse(
        content_blocks=[{"type": "text", "text": "hi"}],
        stop_reason="tool_use",
        usage=Usage(),
    )
    loop = build_loop([weird])
    result = await loop.run_turn(user("go"))
    assert result.stop_reason == "tool_use"
    assert result.text == "hi"
    assert len(loop.client.calls) == 1  # did not loop


# --- events & audit --------------------------------------------------------


async def test_events_emitted() -> None:
    events: list[Event] = []
    loop = build_loop(
        [tool_use_message([ToolCall("t1", "echo", {"text": "hi"})]), text_message("done")]
    )
    await loop.run_turn(user("go"), on_event=events.append)
    kinds = [type(e) for e in events]
    assert ToolStarted in kinds
    assert ToolFinished in kinds
    assert TurnCompleted in kinds


# --- Phase 5: ToolDecision (the attempts tap) ------------------------------


async def test_tool_decision_emitted_for_allowed_call() -> None:
    events: list[Event] = []
    loop = build_loop(
        [tool_use_message([ToolCall("t1", "echo", {"text": "hi"})]), text_message("done")]
    )
    await loop.run_turn(user("go"), on_event=events.append)
    decisions = [e for e in events if isinstance(e, ToolDecision)]
    assert len(decisions) == 1
    assert decisions[0].name == "echo"
    assert decisions[0].gate_decision == "allow" and decisions[0].resolution == "allow"


async def test_tool_decision_emitted_for_ask_denied_call() -> None:
    # The load-bearing case: a denied ASK emits NO ToolStarted, but MUST emit a
    # ToolDecision so an eval sees the model attempted it.
    events: list[Event] = []
    loop = build_loop(
        [tool_use_message([ToolCall("t1", "danger", {})]), text_message("ok")], approver=deny
    )
    await loop.run_turn(user("go"), on_event=events.append)
    assert not any(isinstance(e, ToolStarted) for e in events)  # never ran
    (decision,) = [e for e in events if isinstance(e, ToolDecision)]
    assert decision.name == "danger"
    assert decision.gate_decision == "ask" and decision.resolution == "deny"


async def test_tool_decision_emitted_for_unknown_tool() -> None:
    events: list[Event] = []
    loop = build_loop([tool_use_message([ToolCall("t1", "ghost", {"x": 1})]), text_message("ok")])
    await loop.run_turn(user("go"), on_event=events.append)
    (decision,) = [e for e in events if isinstance(e, ToolDecision)]
    assert decision.name == "ghost"
    assert decision.input == {"x": 1}
    assert decision.resolution == "deny"


async def test_tool_decision_carries_input_for_attempt_matching() -> None:
    events: list[Event] = []
    loop = build_loop(
        [tool_use_message([ToolCall("t1", "echo", {"text": "SECRET-PATH"})]), text_message("ok")]
    )
    await loop.run_turn(user("go"), on_event=events.append)
    (decision,) = [e for e in events if isinstance(e, ToolDecision)]
    assert decision.input == {"text": "SECRET-PATH"}  # input preserved for input-level checks


# --- Phase 5: latency aggregation ------------------------------------------


async def test_turn_result_aggregates_latency() -> None:
    # FakeClient stamps 1.0ms per call when unset; a two-call turn sums to 2.0.
    loop = build_loop(
        [tool_use_message([ToolCall("t1", "echo", {"text": "x"})]), text_message("done")]
    )
    result = await loop.run_turn(user("go"))
    assert result.latency_ms == 2.0


async def test_model_call_logs_latency_and_cache_fields() -> None:
    loop = build_loop([text_message("hi")])
    with structlog.testing.capture_logs() as logs:
        await loop.run_turn(user("go"))
    call = next(e for e in logs if e["event"] == "model_call")
    assert "latency_ms" in call
    assert "cache_creation_input_tokens" in call and "cache_read_input_tokens" in call


async def test_audit_events_logged() -> None:
    loop = build_loop(
        [tool_use_message([ToolCall("t1", "echo", {"text": "hi"})]), text_message("done")]
    )
    with structlog.testing.capture_logs() as logs:
        await loop.run_turn(user("go"))
    names = {e["event"] for e in logs}
    expected = {
        "turn_start",
        "model_call",
        "permission_decision",
        "tool_call",
        "tool_result",
        "turn_end",
    }
    assert expected <= names


# --- Phase 2: context manager + memory integration (optional collaborators) ---


def _long_history(n_turns: int) -> list[dict]:
    """A long but simple alternating conversation (every user msg is a real boundary)."""
    msgs: list[dict] = []
    for i in range(n_turns):
        msgs.append({"role": "user", "content": f"message {i} " + "x" * 200})
        msgs.append({"role": "assistant", "content": [{"type": "text", "text": "ok " + "y" * 200}]})
    return msgs


async def test_null_path_sends_full_messages_unchanged() -> None:
    # No context_manager, no memory => byte-identical Phase-1 behavior.
    loop = build_loop([text_message("ok")])
    await loop.run_turn(user("hi"))
    assert loop.client.calls[0]["messages"] == [{"role": "user", "content": "hi"}]
    assert loop.client.calls[0]["system"] == loop.system


async def test_compaction_sends_view_but_persists_full_history() -> None:
    from jarvis.core.context import ContextManager

    history = _long_history(30)
    history.append({"role": "user", "content": "the final question"})
    cm = ContextManager(context_token_budget=2000, compaction_threshold=0.7, keep_fraction=0.5)
    loop = build_loop([text_message("answer")], context_manager=cm)

    result = await loop.run_turn(history)

    sent = loop.client.calls[0]["messages"]
    assert len(sent) < len(result.messages)  # the API saw a compacted view...
    assert sent[0]["role"] == "user"  # ...that still starts at a real user turn
    # ...while the full history is preserved for persistence (history + 1 assistant).
    assert len(result.messages) == len(history) + 1
    assert result.messages[: len(history)] == history


async def test_context_overflow_stops_turn_without_calling_model() -> None:
    from jarvis.core.context import ContextManager

    # One giant user message: no boundary to cut, no tool_result to elide.
    huge = [{"role": "user", "content": "x" * 400_000}]
    cm = ContextManager(context_token_budget=2000, compaction_threshold=0.7)
    loop = build_loop([text_message("unreached")], context_manager=cm)

    result = await loop.run_turn(huge)
    assert result.stop_reason == "max_context"
    assert len(loop.client.calls) == 0  # never sent a doomed request


async def test_auto_recall_injected_into_system_prompt() -> None:
    class FakeMemory:
        async def auto_recall_context(self, text: str) -> str:
            return "MEMORY-BLOCK: the user prefers neovim"

    loop = build_loop([text_message("ok")], memory=FakeMemory())
    await loop.run_turn(user("what editor do I use?"))
    assert "MEMORY-BLOCK" in loop.client.calls[0]["system"]


async def test_no_recall_block_leaves_system_unchanged() -> None:
    class NoneMemory:
        async def auto_recall_context(self, text: str) -> None:
            return None

    loop = build_loop([text_message("ok")], memory=NoneMemory())
    await loop.run_turn(user("hi"))
    assert loop.client.calls[0]["system"] == loop.system


def _compacting_manager(summary_responses: list):
    from jarvis.core.context import ContextManager

    return ContextManager(
        context_token_budget=2000,
        compaction_threshold=0.7,
        keep_fraction=0.5,
        summarizer=FakeClient(summary_responses),
        utility_model="claude-sonnet-5",
    )


async def test_summary_frozen_and_injected_across_iterations() -> None:
    cm = _compacting_manager([text_message("FROZEN SUMMARY")])
    history = _long_history(30)
    history.append({"role": "user", "content": "the final question"})
    loop = build_loop(
        [tool_use_message([ToolCall("t1", "echo", {"text": "x"})]), text_message("done")],
        context_manager=cm,
    )
    await loop.run_turn(history)

    systems = [c["system"] for c in loop.client.calls]
    assert len(systems) == 2  # a two-iteration turn (tool call, then answer)
    assert all("FROZEN SUMMARY" in s for s in systems)  # same summary both times
    assert len(cm.summarizer.calls) == 1  # summarized once for the turn, not per iteration


async def test_system_extras_ordered_identity_then_summary_then_recall() -> None:
    cm = _compacting_manager([text_message("THE-SUMMARY")])

    class FakeMemory:
        async def auto_recall_context(self, text: str) -> str:
            return "THE-RECALL"

    history = _long_history(30)
    history.append({"role": "user", "content": "final"})
    loop = build_loop([text_message("done")], context_manager=cm, memory=FakeMemory())
    await loop.run_turn(history)

    system = loop.client.calls[0]["system"]
    assert system.startswith(loop.system)  # identity/guidance first
    assert system.index("THE-SUMMARY") < system.index("THE-RECALL")  # summary before recall
