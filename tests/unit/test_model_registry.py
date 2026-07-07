"""Model/provider registry + ClientFactory + OpenAI text-only adapter (Phase 10 Task 6).

Keyless: the OpenAI adapter is driven by a fake client, the factory by an env-pinned config.
Load-bearing pins: resolution precedence; text-only rejected on a tool-capable role; the
factory fails CLOSED on a missing provider key; the adapter maps usage EXPLICITLY, refuses
tools, and fails loud on empty content; no key value appears in the Hub registry view."""

from __future__ import annotations

from pathlib import Path

import pytest

from jarvis.config import ConfigError, load_config
from jarvis.models import ClientFactory, ModelRegistry, ModelRoute, RouteError
from jarvis.models.openai_client import (
    OpenAIChatClient,
    OpenAIResponseError,
    UnsupportedToolUseError,
)
from jarvis.models.roles import DEFAULT_ROUTES, ROLES

# --- registry resolution + validation --------------------------------------


def test_defaults_cover_every_role() -> None:
    assert set(DEFAULT_ROUTES) == set(ROLES)


def test_default_route_used_when_no_override() -> None:
    r = ModelRegistry().route("planner")
    assert r.provider == "anthropic" and r.model == "claude-fable-5"


def test_resolution_precedence() -> None:
    # settings < project < run — each overrides only the fields it names.
    reg = ModelRegistry({"reviewer": {"model": "settings-model"}})
    assert reg.route("reviewer").model == "settings-model"
    assert (
        reg.route("reviewer", project_routes={"reviewer": {"model": "proj-model"}}).model
        == "proj-model"
    )
    r = reg.route(
        "reviewer",
        project_routes={"reviewer": {"model": "proj-model"}},
        run_routes={"reviewer": {"model": "run-model", "effort": "max"}},
    )
    assert r.model == "run-model" and r.effort == "max"


def test_unknown_role_raises() -> None:
    with pytest.raises(RouteError, match="unknown role"):
        ModelRegistry().route("nonexistent")


def test_unknown_provider_rejected() -> None:
    reg = ModelRegistry({"planner": {"provider": "acme"}})
    with pytest.raises(RouteError, match="unknown provider"):
        reg.route("planner")


def test_text_only_rejected_on_tool_capable_role() -> None:
    # coder is the write-capable executor — a text-only route must be refused.
    reg = ModelRegistry({"coder": {"provider": "openai", "model": "gpt-x", "text_only": True}})
    with pytest.raises(RouteError, match="must drive tools"):
        reg.route("coder")


def test_text_only_allowed_on_analysis_role() -> None:
    # reviewer is an analysis role: not tool-capable, and not authority-pinned (Phase 10C) —
    # so a text-only route is allowed. (judge/planner/utility are authority-pinned to anthropic.)
    reg = ModelRegistry({"reviewer": {"provider": "openai", "model": "gpt-x", "text_only": True}})
    assert reg.route("reviewer").text_only is True


# --- ClientFactory: caching + fail-closed ----------------------------------


def _cfg(tmp_path: Path, *, anthropic: str = "", openai: str = ""):
    cfg = load_config(root=tmp_path, env_file=None)
    cfg.secrets = cfg.secrets.model_copy(
        update={"anthropic_api_key": anthropic, "openai_api_key": openai}
    )
    return cfg


def test_factory_fails_closed_on_missing_key(tmp_path: Path) -> None:
    factory = ClientFactory(_cfg(tmp_path, anthropic="", openai=""))
    with pytest.raises(ConfigError, match="ANTHROPIC_API_KEY"):
        factory.for_route(ModelRoute("anthropic", "claude-opus-4-8"))
    with pytest.raises(ConfigError, match="OPENAI_API_KEY"):
        factory.for_route(ModelRoute("openai", "gpt-x", text_only=True))


def test_factory_caches_by_effort_and_thinking(tmp_path: Path) -> None:
    factory = ClientFactory(_cfg(tmp_path, anthropic="k", openai="k"))
    a1 = factory.for_route(ModelRoute("anthropic", "claude-opus-4-8", "high"))
    a2 = factory.for_route(ModelRoute("anthropic", "claude-fable-5", "high"))  # model is per-call
    assert a1 is a2  # same (effort, thinking) ⇒ same cached client
    a3 = factory.for_route(ModelRoute("anthropic", "claude-opus-4-8", "low"))
    assert a3 is not a1  # different effort ⇒ different client
    txt = factory.for_route(ModelRoute("anthropic", "claude-opus-4-8", "high", text_only=True))
    assert txt is not a1  # thinking-off ⇒ different client
    o1 = factory.for_route(ModelRoute("openai", "gpt-a", text_only=True))
    o2 = factory.for_route(ModelRoute("openai", "gpt-b", text_only=True))
    assert o1 is o2 and isinstance(o1, OpenAIChatClient)  # one OpenAI client for all models


def test_text_only_route_builds_thinking_off_anthropic(tmp_path: Path) -> None:
    factory = ClientFactory(_cfg(tmp_path, anthropic="k"))
    client = factory.for_route(ModelRoute("anthropic", "claude-opus-4-8", "high", text_only=True))
    assert client.thinking is False


# --- OpenAI text-only adapter ----------------------------------------------


class _FakeMessage:
    def __init__(self, content: str) -> None:
        self.message = type("M", (), {"content": content})()


class _FakeUsage:
    prompt_tokens = 123
    completion_tokens = 45
    prompt_tokens_details = type("D", (), {"cached_tokens": 20})()


class _FakeCompletion:
    def __init__(self, content: str, *, model: str = "gpt-x", empty: bool = False) -> None:
        self.choices = [] if empty else [_FakeMessage(content)]
        self.usage = _FakeUsage()
        self.model = model


class _FakeOpenAI:
    """Minimal AsyncOpenAI stand-in: client.chat.completions.create(**kwargs)."""

    def __init__(self, completion: _FakeCompletion) -> None:
        self._completion = completion
        self.last_kwargs: dict | None = None

        async def _create(**kwargs):
            self.last_kwargs = kwargs
            return self._completion

        completions = type("K", (), {"create": staticmethod(_create)})()
        self.chat = type("C", (), {"completions": completions})()


async def test_openai_adapter_maps_usage_explicitly() -> None:
    client = OpenAIChatClient(client=_FakeOpenAI(_FakeCompletion("hello world")))
    resp = await client.create(
        model="gpt-x",
        system="be brief",
        messages=[{"role": "user", "content": "hi"}],
        tools=[],
        max_tokens=100,
    )
    assert resp.text == "hello world" and resp.stop_reason == "end_turn"
    # EXPLICIT mapping — prompt/completion → input/output, cached → cache_read (not zeros).
    assert resp.usage.input_tokens == 123 and resp.usage.output_tokens == 45
    assert resp.usage.cache_read_input_tokens == 20


async def test_openai_adapter_refuses_tools() -> None:
    client = OpenAIChatClient(client=_FakeOpenAI(_FakeCompletion("x")))
    with pytest.raises(UnsupportedToolUseError):
        await client.create(
            model="gpt-x",
            system="s",
            messages=[{"role": "user", "content": "hi"}],
            tools=[{"name": "read_file"}],
            max_tokens=10,
        )


async def test_openai_adapter_fails_loud_on_empty() -> None:
    client = OpenAIChatClient(client=_FakeOpenAI(_FakeCompletion("", empty=True)))
    with pytest.raises(OpenAIResponseError):
        await client.create(
            model="gpt-x",
            system="s",
            messages=[{"role": "user", "content": "hi"}],
            tools=[],
            max_tokens=10,
        )


async def test_openai_adapter_puts_system_first() -> None:
    fake = _FakeOpenAI(_FakeCompletion("ok"))
    client = OpenAIChatClient(client=fake)
    await client.create(
        model="gpt-x",
        system="SYSTEM PROMPT",
        messages=[{"role": "user", "content": "hi"}],
        tools=[],
        max_tokens=10,
    )
    msgs = fake.last_kwargs["messages"]
    assert msgs[0] == {"role": "system", "content": "SYSTEM PROMPT"}
    assert fake.last_kwargs["max_completion_tokens"] == 10


# --- Hub registry view: no keys ---------------------------------------------


def test_hub_model_routes_report_configured_without_keys(tmp_path: Path) -> None:
    from jarvis.ui.readmodels import model_routes_status

    cfg = _cfg(tmp_path, anthropic="SECRET-ANTHROPIC-CANARY", openai="")
    rows = model_routes_status(cfg)
    by_role = {r["role"]: r for r in rows}
    assert by_role["planner"]["configured"] is True  # anthropic key present
    # An OpenAI-routed (non-authority) role with no OpenAI key reports configured=False.
    cfg2 = _cfg(tmp_path, anthropic="k", openai="")
    cfg2.models.routes = {"reviewer": {"provider": "openai", "model": "gpt-x", "text_only": True}}
    j = {r["role"]: r for r in model_routes_status(cfg2)}["reviewer"]
    assert j["provider"] == "openai" and j["configured"] is False
    # No key value anywhere in the serialized view.
    assert "SECRET-ANTHROPIC-CANARY" not in str(rows)


def test_model_routes_report_provider_state(tmp_path: Path) -> None:
    from jarvis.ui.readmodels import model_routes_status

    cfg = _cfg(tmp_path, anthropic="k", openai="")
    by_role = {r["role"]: r for r in model_routes_status(cfg)}
    # anthropic core + key + priced ⇒ available
    assert by_role["planner"]["provider_state"] == "available"


def _write_pricing(tmp_path: Path) -> None:
    (tmp_path / "config").mkdir(exist_ok=True)
    (tmp_path / "config" / "pricing.yaml").write_text(
        "schema_version: 2\nmodels:\n"
        "  anthropic:\n    claude-opus-4-8: {input: 5.0, output: 25.0}\n"
        "  openai:\n    gpt-5.2: {input: 1.75, output: 14.0}\n"
        "  deepseek:\n    deepseek-v4-flash: {input: 0.14, output: 0.28}\n"
        "  gemini:\n    gemini-2.5-flash: {input: 0.3, output: 2.5}\n",
        encoding="utf-8",
    )


def test_providers_status_states_and_secret_sweep(tmp_path: Path) -> None:
    from jarvis.ui.readmodels import providers_status

    _write_pricing(tmp_path)  # deepseek+gemini priced here; qwen intentionally absent
    cfg = _cfg(tmp_path, anthropic="k", openai="")
    # Set ALL provider keys explicitly so the test is hermetic (ambient os.environ can't leak a
    # key into config.secrets and flip a state).
    cfg.secrets = cfg.secrets.model_copy(
        update={
            "deepseek_api_key": "SECRET-DS-CANARY",
            "gemini_api_key": "",
            "zai_api_key": "",
            "dashscope_api_key": "",
        }
    )
    cfg.providers = cfg.providers.model_copy(update={"enabled": ["deepseek", "gemini"]})
    rows = {r["name"]: r for r in providers_status(cfg)}
    assert rows["anthropic"]["state"] == "available"  # core + key + priced
    assert rows["deepseek"]["state"] == "available"  # enabled + key + priced
    assert rows["gemini"]["state"] == "missing_credentials"  # enabled + priced, no key
    assert rows["qwen"]["state"] == "disabled"  # not enabled (and unpriced anyway)
    assert rows["openai"]["state"] == "missing_credentials"  # core, priced, but no key here
    # presence booleans + env-var NAMES only — never a key value.
    assert "SECRET-DS-CANARY" not in str(rows)
    assert rows["deepseek"]["credentials_present"] is True
    assert rows["deepseek"]["credential_env"] == ["DEEPSEEK_API_KEY"]
