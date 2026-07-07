"""Anthropic-compat client path + catalog-driven ClientFactory (Phase 10C, T3).

Keyless: an injected fake SDK client records the kwargs sent on the wire, so we can pin the
capability-degradation profile (no effort/thinking for compat), the fail-loud guards, the
per-provider auth style, and the factory's fail-closed key handling — no network, no real key.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from jarvis.config import ConfigError, load_config
from jarvis.core.anthropic_client import AnthropicClient, CompatResponseError
from jarvis.models.factory import ClientFactory
from jarvis.models.openai_client import OpenAIChatClient
from jarvis.models.providers import provider_spec
from jarvis.models.roles import ModelRoute


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("ANTHROPIC_API_KEY", "DEEPSEEK_API_KEY", "DASHSCOPE_API_KEY", "ZAI_API_KEY",
                "GEMINI_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(var, raising=False)


# --- fake streaming SDK -----------------------------------------------------


class _FakeStream:
    def __init__(self, texts: list[str], message: object) -> None:
        self._texts, self._message = texts, message

    async def __aenter__(self) -> _FakeStream:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    @property
    def text_stream(self):
        async def _gen():
            for t in self._texts:
                yield t

        return _gen()

    async def get_final_message(self) -> object:
        return self._message


class _FakeMessages:
    def __init__(self, stream: _FakeStream) -> None:
        self._stream = stream
        self.captured: dict | None = None

    def stream(self, **kwargs: object) -> _FakeStream:
        self.captured = kwargs
        return self._stream


class _FakeAnthropic:
    def __init__(self, stream: _FakeStream) -> None:
        self.messages = _FakeMessages(stream)


def _message(**over: object) -> SimpleNamespace:
    d = dict(
        content=[
            SimpleNamespace(type="text", text="hi"),
            SimpleNamespace(type="tool_use", id="t1", name="echo", input={"x": 1}),
        ],
        stop_reason="tool_use",
        usage=SimpleNamespace(
            input_tokens=100, output_tokens=20,
            cache_creation_input_tokens=0, cache_read_input_tokens=0,
        ),
        model="deepseek-v4-flash",
    )
    d.update(over)
    return SimpleNamespace(**d)


async def _run(client: AnthropicClient, **over):
    kw = dict(
        model="deepseek-v4-flash",
        system="s",
        messages=[{"role": "user", "content": "hi"}],
        tools=[{"name": "echo", "description": "d", "input_schema": {}}],
        max_tokens=1000,
    )
    kw.update(over)
    return await client.create(**kw)


# --- compat degradation profile ---------------------------------------------


async def test_compat_omits_effort_and_thinking() -> None:
    fake = _FakeAnthropic(_FakeStream(["hi"], _message()))
    resp = await _run(AnthropicClient(client=fake, compat=True))
    sent = fake.messages.captured
    assert "output_config" not in sent  # effort is Anthropic-native — never sent to compat
    assert "thinking" not in sent  # adaptive thinking — never sent to compat
    assert resp.tool_calls[0].name == "echo"  # tool blocks still round-trip


async def test_native_sends_effort_and_thinking() -> None:
    fake = _FakeAnthropic(_FakeStream(["hi"], _message()))
    await _run(AnthropicClient(client=fake, compat=False))  # native path
    sent = fake.messages.captured
    assert sent["output_config"] == {"effort": "high"}
    assert sent["thinking"] == {"type": "adaptive"}


async def test_compat_maps_usage() -> None:
    fake = _FakeAnthropic(_FakeStream(["hi"], _message()))
    resp = await _run(AnthropicClient(client=fake, compat=True))
    assert resp.usage.input_tokens == 100 and resp.usage.output_tokens == 20


# --- fail-loud guards (compat only) -----------------------------------------


async def test_compat_empty_content_fails_loud() -> None:
    empty = _message(content=[], stop_reason="end_turn")
    fake = _FakeAnthropic(_FakeStream([], empty))
    with pytest.raises(CompatResponseError):
        await _run(AnthropicClient(client=fake, compat=True))


async def test_compat_zero_usage_fails_loud() -> None:
    zero = _message(usage=SimpleNamespace(
        input_tokens=0, output_tokens=0, cache_creation_input_tokens=0, cache_read_input_tokens=0
    ))
    fake = _FakeAnthropic(_FakeStream(["hi"], zero))
    with pytest.raises(CompatResponseError):
        await _run(AnthropicClient(client=fake, compat=True))


async def test_native_zero_usage_does_not_raise() -> None:
    # The guard is compat-only; native Anthropic always reports usage, so no guard applies.
    zero = _message(usage=SimpleNamespace(
        input_tokens=0, output_tokens=0, cache_creation_input_tokens=0, cache_read_input_tokens=0
    ))
    fake = _FakeAnthropic(_FakeStream(["hi"], zero))
    await _run(AnthropicClient(client=fake, compat=False))  # no raise


# --- per-provider auth style (construction wiring) --------------------------


def test_bearer_auth_uses_auth_token(monkeypatch: pytest.MonkeyPatch) -> None:
    import anthropic

    captured: dict = {}

    class _Rec:
        def __init__(self, **kw: object) -> None:
            captured.update(kw)

    monkeypatch.setattr(anthropic, "AsyncAnthropic", _Rec)
    AnthropicClient(api_key="k", auth_style="bearer", base_url="https://z.ai", compat=True)
    assert captured.get("auth_token") == "k" and "api_key" not in captured
    assert captured.get("base_url") == "https://z.ai"


def test_x_api_key_auth_default(monkeypatch: pytest.MonkeyPatch) -> None:
    import anthropic

    captured: dict = {}

    class _Rec:
        def __init__(self, **kw: object) -> None:
            captured.update(kw)

    monkeypatch.setattr(anthropic, "AsyncAnthropic", _Rec)
    AnthropicClient(api_key="k", base_url="https://api.deepseek.com/anthropic", compat=True)
    assert captured.get("api_key") == "k" and "auth_token" not in captured


# --- factory: catalog-driven, fail-closed -----------------------------------


def _cfg(tmp_path: Path, **secrets: str):
    cfg = load_config(root=tmp_path, env_file=None)
    cfg.secrets = cfg.secrets.model_copy(update=secrets)
    return cfg


def test_factory_builds_compat_client_for_deepseek(tmp_path: Path) -> None:
    factory = ClientFactory(_cfg(tmp_path, deepseek_api_key="k"))
    client = factory.for_route(ModelRoute("deepseek", "deepseek-v4-flash"))
    assert isinstance(client, AnthropicClient) and client.compat is True


def test_factory_fails_closed_on_missing_deepseek_key(tmp_path: Path) -> None:
    factory = ClientFactory(_cfg(tmp_path))
    with pytest.raises(ConfigError, match="DEEPSEEK_API_KEY"):
        factory.for_route(ModelRoute("deepseek", "deepseek-v4-flash"))


def test_factory_native_anthropic_is_not_compat(tmp_path: Path) -> None:
    factory = ClientFactory(_cfg(tmp_path, anthropic_api_key="k"))
    client = factory.for_route(ModelRoute("anthropic", "claude-opus-4-8"))
    assert isinstance(client, AnthropicClient) and client.compat is False


def test_factory_caches_compat_client(tmp_path: Path) -> None:
    factory = ClientFactory(_cfg(tmp_path, deepseek_api_key="k"))
    a = factory.for_route(ModelRoute("deepseek", "deepseek-v4-flash"))
    b = factory.for_route(ModelRoute("deepseek", "deepseek-v4-pro"))  # model is a per-call arg
    assert a is b


def test_factory_base_url_override_beats_default(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path, deepseek_api_key="k")
    cfg.providers = cfg.providers.model_copy(update={"base_urls": {"deepseek": "https://mirror"}})
    factory = ClientFactory(cfg)
    assert factory._base_url(provider_spec("deepseek")) == "https://mirror"
    # unspecified provider keeps the catalog default
    assert factory._base_url(provider_spec("zai")) == "https://api.z.ai/api/anthropic"


def test_factory_unknown_provider_fails_closed(tmp_path: Path) -> None:
    factory = ClientFactory(_cfg(tmp_path, anthropic_api_key="k"))
    with pytest.raises(ConfigError, match="unknown model provider"):
        factory.for_route(ModelRoute("mystery", "m"))


def test_factory_openai_client_for_core_openai(tmp_path: Path) -> None:
    factory = ClientFactory(_cfg(tmp_path, openai_api_key="k"))
    client = factory.for_route(ModelRoute("openai", "gpt-5.2", text_only=True))
    assert isinstance(client, OpenAIChatClient)


def test_factory_gemini_builds_openai_client_with_base_url(tmp_path: Path) -> None:
    factory = ClientFactory(_cfg(tmp_path, gemini_api_key="k"))
    client = factory.for_route(ModelRoute("gemini", "gemini-2.5-flash", text_only=True))
    assert isinstance(client, OpenAIChatClient)  # Gemini rides the text-only OpenAI-compat client
    base = factory._base_url(provider_spec("gemini"))
    assert base == "https://generativelanguage.googleapis.com/v1beta/openai/"


def test_factory_fails_closed_on_missing_gemini_key(tmp_path: Path) -> None:
    factory = ClientFactory(_cfg(tmp_path))
    with pytest.raises(ConfigError, match="GEMINI_API_KEY"):
        factory.for_route(ModelRoute("gemini", "gemini-2.5-flash", text_only=True))
