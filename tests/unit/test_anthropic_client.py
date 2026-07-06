"""AnthropicClient tests against a fake stream — exercises the streaming loop and
block serialization without any network call."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from jarvis.config import (
    Config,
    ConfigError,
    LimitsConfig,
    ModelsConfig,
    PathsConfig,
    Secrets,
)
from jarvis.core.anthropic_client import AnthropicClient, _serialize_block, to_model_response


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)


# --- fake SDK objects ------------------------------------------------------


class _FakeStream:
    def __init__(self, texts: list[str], message: object) -> None:
        self._texts = texts
        self._message = message

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
    defaults = dict(
        content=[
            SimpleNamespace(type="thinking", thinking="", signature="sig123"),
            SimpleNamespace(type="text", text="hello"),
            SimpleNamespace(type="tool_use", id="tu1", name="echo", input={"x": 1}),
        ],
        stop_reason="tool_use",
        usage=SimpleNamespace(
            input_tokens=100,
            output_tokens=20,
            cache_creation_input_tokens=0,
            cache_read_input_tokens=5,
        ),
        model="claude-opus-4-8",
    )
    defaults.update(over)
    return SimpleNamespace(**defaults)


# --- block serialization ---------------------------------------------------


def test_serialize_text_block() -> None:
    assert _serialize_block(SimpleNamespace(type="text", text="hi")) == {
        "type": "text",
        "text": "hi",
    }


def test_serialize_thinking_preserves_signature() -> None:
    blk = SimpleNamespace(type="thinking", thinking="", signature="abc")
    assert _serialize_block(blk) == {"type": "thinking", "thinking": "", "signature": "abc"}


def test_serialize_tool_use_block() -> None:
    blk = SimpleNamespace(type="tool_use", id="t1", name="echo", input={"a": 1})
    assert _serialize_block(blk) == {
        "type": "tool_use",
        "id": "t1",
        "name": "echo",
        "input": {"a": 1},
    }


def test_serialize_unknown_block_falls_back_to_model_dump() -> None:
    blk = SimpleNamespace(type="future", model_dump=lambda: {"type": "future", "x": 1})
    assert _serialize_block(blk) == {"type": "future", "x": 1}


def test_to_model_response_maps_fields() -> None:
    resp = to_model_response(_message(), fallback_model="fallback")
    assert resp.stop_reason == "tool_use"
    assert resp.model == "claude-opus-4-8"
    assert resp.usage.input_tokens == 100
    assert resp.usage.cache_read_input_tokens == 5
    assert resp.tool_calls[0].name == "echo"
    assert {"type": "thinking", "thinking": "", "signature": "sig123"} in resp.content_blocks


# --- create() streaming ----------------------------------------------------


async def test_create_streams_text_and_converts() -> None:
    client = AnthropicClient(client=_FakeAnthropic(_FakeStream(["Hel", "lo"], _message())))
    chunks: list[str] = []
    resp = await client.create(
        model="claude-opus-4-8",
        system="s",
        messages=[{"role": "user", "content": "hi"}],
        tools=[{"name": "echo", "description": "d", "input_schema": {}}],
        max_tokens=1000,
        on_text_delta=chunks.append,
    )
    assert chunks == ["Hel", "lo"]
    assert resp.text == "hello"
    assert resp.tool_calls[0].input == {"x": 1}


async def test_create_builds_expected_kwargs() -> None:
    fake = _FakeAnthropic(_FakeStream([], _message(stop_reason="end_turn")))
    client = AnthropicClient(client=fake, effort="xhigh")
    await client.create(
        model="claude-opus-4-8",
        system="sys",
        messages=[{"role": "user", "content": "hi"}],
        tools=[{"name": "echo"}],
        max_tokens=1234,
    )
    kw = fake.messages.captured
    assert kw["model"] == "claude-opus-4-8"
    assert kw["max_tokens"] == 1234
    assert kw["system"] == "sys"
    assert kw["thinking"] == {"type": "adaptive"}
    assert kw["output_config"] == {"effort": "xhigh"}
    assert kw["tools"] == [{"name": "echo"}]


async def test_create_omits_tools_and_thinking_when_disabled() -> None:
    fake = _FakeAnthropic(_FakeStream([], _message(stop_reason="end_turn")))
    client = AnthropicClient(client=fake, thinking=False)
    await client.create(
        model="claude-opus-4-8",
        system="sys",
        messages=[{"role": "user", "content": "hi"}],
        tools=[],
        max_tokens=100,
    )
    kw = fake.messages.captured
    assert "tools" not in kw
    assert "thinking" not in kw


# --- Phase 5: latency + temperature ----------------------------------------


async def test_create_populates_latency() -> None:
    fake = _FakeAnthropic(_FakeStream([], _message(stop_reason="end_turn")))
    client = AnthropicClient(client=fake)
    resp = await client.create(
        model="claude-opus-4-8",
        system="s",
        messages=[{"role": "user", "content": "hi"}],
        tools=[],
        max_tokens=100,
    )
    assert resp.latency_ms is not None and resp.latency_ms >= 0.0


async def test_create_passes_temperature_only_when_set() -> None:
    fake = _FakeAnthropic(_FakeStream([], _message(stop_reason="end_turn")))
    client = AnthropicClient(client=fake, thinking=False)
    # default: no temperature sent
    await client.create(
        model="m", system="s", messages=[{"role": "user", "content": "hi"}], tools=[], max_tokens=10
    )
    assert "temperature" not in fake.messages.captured
    # explicit: forwarded (the judge sets 1.0)
    await client.create(
        model="m",
        system="s",
        messages=[{"role": "user", "content": "hi"}],
        tools=[],
        max_tokens=10,
        temperature=1.0,
        tool_choice={"type": "tool", "name": "record_verdict"},
    )
    assert fake.messages.captured["temperature"] == 1.0


# --- from_config -----------------------------------------------------------


def _config(**limits: object) -> Config:
    return Config(
        root=Path.cwd(),
        models=ModelsConfig(),
        limits=LimitsConfig(**limits),
        paths=PathsConfig(),
        secrets=Secrets(_env_file=None),  # type: ignore[call-arg]
    )


def test_from_config_requires_key() -> None:
    with pytest.raises(ConfigError):
        AnthropicClient.from_config(_config())


def test_from_config_uses_config_effort(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    client = AnthropicClient.from_config(_config(effort="max"))
    assert client.effort == "max"
