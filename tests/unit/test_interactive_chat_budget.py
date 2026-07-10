"""Hard ordinary-chat budget pins: fail closed before provider calls, never reroute on price."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from pydantic import BaseModel

from jarvis.config import ChatConfig, Config, LimitsConfig, ModelsConfig, PathsConfig, Secrets
from jarvis.core import AgentLoop, FakeClient, ToolCall, text_message, tool_use_message
from jarvis.core.execution import ExecutionContext, bind_execution_context
from jarvis.observability.cost import Price, PricingTable, Usage
from jarvis.observability.ledger import cost_context
from jarvis.permissions import PermissionGate, Policy
from jarvis.routing import Classifier, RoutingMode
from jarvis.routing.router import Router, RoutingState
from jarvis.tools import Permission, Tool, ToolExecutor, ToolRegistry


class _EchoParams(BaseModel):
    text: str


class _Echo(Tool):
    name = "echo"
    description = "Echo text."
    Params = _EchoParams
    permission_default = Permission.ALLOW

    async def run(self, params: _EchoParams) -> str:
        return params.text


def _pricing(*, include_sonnet: bool = True) -> PricingTable:
    models = {"claude-opus-4-8": Price(5.0, 25.0)}
    if include_sonnet:
        models["claude-sonnet-5"] = Price(3.0, 15.0)
    return PricingTable(
        version="test",
        effective="",
        cache_write_multiplier=1.25,
        cache_read_multiplier=0.1,
        models={"anthropic": models},
        services={},
    )


def _pricing_with_router() -> PricingTable:
    pricing = _pricing()
    return PricingTable(
        version=pricing.version,
        effective=pricing.effective,
        cache_write_multiplier=pricing.cache_write_multiplier,
        cache_read_multiplier=pricing.cache_read_multiplier,
        models={
            **pricing.models,
            "gemini": {
                "gemini-2.5-flash-lite": Price(0.1, 0.4),
                "gemini-2.5-flash": Price(0.3, 2.5),
            },
        },
        services={},
    )


def _config(chat: ChatConfig) -> Config:
    return Config(
        root=Path.cwd(),
        models=ModelsConfig(main="claude-sonnet-5"),
        limits=LimitsConfig(),
        chat=chat,
        paths=PathsConfig(),
        secrets=Secrets(_env_file=None),  # type: ignore[call-arg]
    )


def _loop(client: FakeClient, *, chat: ChatConfig, pricing: PricingTable) -> AgentLoop:
    registry = ToolRegistry()
    registry.register(_Echo())
    return AgentLoop(
        client=client,
        registry=registry,
        executor=ToolExecutor(),
        gate=PermissionGate(Policy(), Path.cwd()),
        config=_config(chat),
        chat_limits=chat,
        pricing=pricing,
        provider_override=lambda: "anthropic",
    )


async def test_cheap_chat_turn_under_cap_succeeds() -> None:
    client = FakeClient([text_message("done", model="claude-sonnet-5")])
    loop = _loop(
        client,
        chat=ChatConfig(max_iterations=2, max_output_tokens=100, hard_stop_usd_per_turn=0.10),
        pricing=_pricing(),
    )

    result = await loop.run_turn([{"role": "user", "content": "hello"}])

    assert result.text == "done"
    assert result.cost_usd is not None and result.cost_usd < result.budget_usd
    assert len(client.calls) == 1
    assert client.calls[0]["max_tokens"] == 100


async def test_chat_turn_over_cap_refuses_before_model_call() -> None:
    client = FakeClient([text_message("must not be used", model="claude-sonnet-5")])
    loop = _loop(
        client,
        chat=ChatConfig(max_output_tokens=4_096, hard_stop_usd_per_turn=0.0001),
        pricing=_pricing(),
    )

    result = await loop.run_turn([{"role": "user", "content": "hello"}])

    assert result.stop_reason == "cost_cap"
    assert "before the next model call" in result.text
    assert client.calls == []


async def test_mid_loop_cap_stops_before_tools_or_another_model_call() -> None:
    client = FakeClient(
        [
            tool_use_message(
                [ToolCall("tool-1", "echo", {"text": "not executed"})],
                usage=Usage(input_tokens=10, output_tokens=1_000),
                model="claude-sonnet-5",
            ),
            text_message("must not be used", model="claude-sonnet-5"),
        ]
    )
    loop = _loop(
        client,
        chat=ChatConfig(max_iterations=4, max_output_tokens=100, hard_stop_usd_per_turn=0.01),
        pricing=_pricing(),
    )

    result = await loop.run_turn([{"role": "user", "content": "use a tool"}])

    assert result.stop_reason == "cost_cap"
    assert "No further model or tool calls" in result.text
    assert len(client.calls) == 1
    assert not any(
        isinstance(message.get("content"), list)
        and any(block.get("type") == "tool_result" for block in message["content"])
        for message in result.messages
    )


async def test_unpriced_chat_model_fails_closed_without_expensive_fallback() -> None:
    client = FakeClient([text_message("must not be used", model="claude-opus-4-8")])
    loop = _loop(
        client,
        chat=ChatConfig(max_output_tokens=100, hard_stop_usd_per_turn=0.10),
        pricing=_pricing(include_sonnet=False),
    )

    result = await loop.run_turn([{"role": "user", "content": "hello"}])

    assert result.stop_reason == "cost_cap"
    assert "no verified price" in result.text
    assert result.model == "claude-sonnet-5"
    assert client.calls == []


async def test_auto_classifier_is_preflighted_before_it_can_fallback() -> None:
    classifier_client = FakeClient([text_message('{"difficulty":"hard"}')])
    main_client = FakeClient([text_message("must not be used", model="claude-sonnet-5")])
    chat = ChatConfig(max_output_tokens=100, hard_stop_usd_per_turn=0.10)
    registry = ToolRegistry()
    registry.register(_Echo())
    router = Router(
        state=RoutingState(RoutingMode.AUTO),
        manual_model=lambda: "claude-sonnet-5",
        manual_effort=lambda: None,
        classifier=Classifier(classifier_client, "gemini-2.5-flash-lite"),
        is_available=lambda _provider: True,
    )
    loop = AgentLoop(
        client=main_client,
        registry=registry,
        executor=ToolExecutor(),
        gate=PermissionGate(Policy(), Path.cwd()),
        config=_config(chat),
        router=router,
        client_selector=lambda _decision: main_client,
        chat_limits=chat,
        # Gemini is intentionally absent: the classifier must fail closed before its request,
        # rather than fall back to the more expensive Anthropic main client.
        pricing=_pricing(),
    )

    result = await loop.run_turn([{"role": "user", "content": "hello"}])

    assert result.stop_reason == "cost_cap"
    assert "routing classifier has no verified price" in result.text
    assert classifier_client.calls == []
    assert main_client.calls == []


async def test_auto_classifier_carries_the_chat_execution_context() -> None:
    class _TrackingClient(FakeClient):
        async def create(self, **kwargs):  # type: ignore[override]
            self.contexts.append(cost_context.get())
            return await super().create(**kwargs)

    classifier_client = _TrackingClient(
        [
            text_message(
                '{"difficulty":"simple","sensitivity":"non_sensitive",'
                '"category":"chat","needs_tools":false}',
                model="gemini-2.5-flash-lite",
            )
        ]
    )
    classifier_client.contexts = []
    main_client = FakeClient([text_message("done", model="gemini-2.5-flash")])
    chat = ChatConfig(max_output_tokens=100, hard_stop_usd_per_turn=0.10)
    registry = ToolRegistry()
    registry.register(_Echo())
    router = Router(
        state=RoutingState(RoutingMode.AUTO),
        manual_model=lambda: "claude-sonnet-5",
        manual_effort=lambda: None,
        classifier=Classifier(classifier_client, "gemini-2.5-flash-lite"),
        is_available=lambda _provider: True,
    )
    loop = AgentLoop(
        client=main_client,
        registry=registry,
        executor=ToolExecutor(),
        gate=PermissionGate(Policy(), Path.cwd()),
        config=_config(chat),
        router=router,
        client_selector=lambda _decision: main_client,
        chat_limits=chat,
        pricing=_pricing_with_router(),
        project=lambda: SimpleNamespace(project_id=44, system_extra=""),
    )

    with bind_execution_context(ExecutionContext(session_id=33, project_id=44)):
        result = await loop.run_turn([{"role": "user", "content": "hello"}])

    assert result.text == "done"
    assert classifier_client.contexts[0].session_id == 33
    assert classifier_client.contexts[0].project_id == 44


def test_chat_defaults_are_lower_than_general_loop_defaults() -> None:
    chat = ChatConfig()
    limits = LimitsConfig()
    assert chat.max_iterations < limits.max_iterations
    assert chat.max_output_tokens < limits.max_output_tokens
    assert chat.hard_stop_usd_per_turn > 0
