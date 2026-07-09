"""Phase 15.6 Task 4: the AgentLoop's Auto/Manual dispatch seam, end-to-end and keyless.

Proves the loop applies a RouteDecision for the whole turn — it uses the client_selector's client
for the routed provider (Gemini vs Anthropic), NOT always self.client — and that with NO router the
loop is byte-identical (self.client + config.models.main). Uses FakeClients per provider so we can
assert exactly which client ran and on which model.
"""

from __future__ import annotations

from pathlib import Path

from jarvis.config import load_config
from jarvis.core import AgentLoop, FakeClient, build_system, text_message
from jarvis.permissions import PermissionGate, Policy
from jarvis.routing import Classifier, RoutingMode
from jarvis.routing.router import Router, RoutingState
from jarvis.tools import ToolContext, ToolExecutor, ToolRegistry

_ALL = lambda _p: True  # noqa: E731 - every provider available in the test


def _classifier(json_text: str) -> Classifier:
    return Classifier(FakeClient([text_message(json_text)]), "gemini-2.5-flash-lite")


def _loop(tmp_path: Path, *, router, anth: FakeClient, gem: FakeClient):
    cfg = load_config(root=tmp_path, env_file=None)
    reg = ToolRegistry()
    reg.discover("jarvis.tools.builtin", ToolContext(config=cfg))
    clients = {"anthropic": anth, "gemini": gem}
    loop = AgentLoop(
        client=anth,
        registry=reg,
        executor=ToolExecutor(),
        gate=PermissionGate(Policy(), tmp_path),
        config=cfg,
        system=build_system(),
        router=router,
        client_selector=lambda d: clients.get(d.provider),
    )
    return loop, cfg


def _router(state, classifier):
    return Router(
        state=state,
        manual_model=lambda: "claude-opus-4-8",
        manual_effort=lambda: None,
        classifier=classifier,
        is_available=_ALL,
    )


async def test_auto_toolfree_simple_dispatches_to_gemini_with_tools_off(tmp_path: Path) -> None:
    anth = FakeClient([text_message("should-not-run")])
    gem = FakeClient([text_message("done")])
    clf = _classifier(
        '{"difficulty":"simple","sensitivity":"non_sensitive","category":"chat","needs_tools":false}'
    )
    loop, _ = _loop(
        tmp_path, router=_router(RoutingState(RoutingMode.AUTO), clf), anth=anth, gem=gem
    )
    await loop.run_turn([{"role": "user", "content": "what's 2+2"}])
    assert gem.calls and gem.calls[-1]["model"] == "gemini-2.5-flash"
    assert gem.calls[-1]["tools"] == []  # text-only provider ⇒ NO tools that turn
    assert not anth.calls  # the anthropic client never ran for a tool-free simple turn


async def test_auto_simple_needing_tools_dispatches_to_haiku_with_tools(tmp_path: Path) -> None:
    # A simple but tool-needing turn must NOT go to text-only Gemini — it goes to Haiku (cheap,
    # tool-capable), and the full toolset is sent so it can actually act.
    anth = FakeClient([text_message("done")])
    gem = FakeClient([text_message("should-not-run")])
    clf = _classifier(
        '{"difficulty":"simple","sensitivity":"non_sensitive","category":"other","needs_tools":true}'
    )
    loop, _ = _loop(
        tmp_path, router=_router(RoutingState(RoutingMode.AUTO), clf), anth=anth, gem=gem
    )
    await loop.run_turn([{"role": "user", "content": "search the web for X"}])
    assert anth.calls and anth.calls[-1]["model"] == "claude-haiku-4-5-20251001"
    assert anth.calls[-1]["tools"]  # tool-capable route ⇒ full toolset present
    assert not gem.calls


async def test_auto_private_dispatches_to_sonnet(tmp_path: Path) -> None:
    anth = FakeClient([text_message("done")])
    gem = FakeClient([text_message("should-not-run")])
    clf = _classifier('{"difficulty":"moderate","sensitivity":"private","category":"email"}')
    loop, _ = _loop(
        tmp_path, router=_router(RoutingState(RoutingMode.AUTO), clf), anth=anth, gem=gem
    )
    await loop.run_turn([{"role": "user", "content": "summarize my inbox"}])
    assert anth.calls and anth.calls[-1]["model"] == "claude-sonnet-5"
    assert not gem.calls  # private content NEVER touches the cheap tier


async def test_auto_classifier_failure_escalates_to_sonnet(tmp_path: Path) -> None:
    # Unparseable classifier output ⇒ FAILSAFE (private/hard) ⇒ trusted Sonnet, never Gemini.
    anth = FakeClient([text_message("done")])
    gem = FakeClient([text_message("should-not-run")])
    loop, _ = _loop(tmp_path, router=_router(RoutingState(RoutingMode.AUTO), _classifier("nope")),
                    anth=anth, gem=gem)
    await loop.run_turn([{"role": "user", "content": "hello"}])
    assert anth.calls and anth.calls[-1]["model"] == "claude-sonnet-5"
    assert not gem.calls


async def test_manual_dispatches_to_pinned_model(tmp_path: Path) -> None:
    anth = FakeClient([text_message("done")])
    gem = FakeClient([text_message("should-not-run")])
    loop, _ = _loop(
        tmp_path, router=_router(RoutingState(RoutingMode.MANUAL), None), anth=anth, gem=gem
    )
    await loop.run_turn([{"role": "user", "content": "anything"}])
    assert anth.calls and anth.calls[-1]["model"] == "claude-opus-4-8"
    assert not gem.calls


async def test_no_router_is_byte_identical(tmp_path: Path) -> None:
    # No router ⇒ self.client + config.models.main (REPL / sub-agents / evals unchanged).
    cfg = load_config(root=tmp_path, env_file=None)
    reg = ToolRegistry()
    reg.discover("jarvis.tools.builtin", ToolContext(config=cfg))
    client = FakeClient([text_message("done")])
    loop = AgentLoop(
        client=client, registry=reg, executor=ToolExecutor(),
        gate=PermissionGate(Policy(), tmp_path), config=cfg, system=build_system(),
    )
    await loop.run_turn([{"role": "user", "content": "x"}])
    assert client.calls[-1]["model"] == cfg.models.main
