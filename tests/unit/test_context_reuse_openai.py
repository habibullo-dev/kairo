"""S7 enable-step, OpenAI arm (Phase 13 M0 Task 2). Keyless: an injected fake OpenAI SDK captures
the on-the-wire kwargs.

OpenAI's mode is ``automatic_prefix`` with a ``prompt_cache_key`` (it routes a request to a warm
prefix cache); we set the key = the stable-prefix hash when the flag is on, and NOTHING when off
(byte-identical). Gemini rides the SAME client but its mode is ``provider_default`` (implicit
caching) — the capability data alone means it never gets a key. The factory threads the flag +
provider label through. The normalized ledger recording for OpenAI is proven in
``test_context_reuse_wiring`` (it is provider-agnostic and reads the response usage)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from jarvis.config import load_config
from jarvis.models.context_reuse import plan_for_prefix
from jarvis.models.factory import ClientFactory
from jarvis.models.openai_client import OpenAIChatClient
from jarvis.models.roles import ModelRoute


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("OPENAI_API_KEY", "GEMINI_API_KEY"):
        monkeypatch.delenv(var, raising=False)


# --- fake OpenAI SDK (captures the request kwargs) --------------------------


class _FakeCompletions:
    def __init__(self, completion: object) -> None:
        self._c = completion
        self.captured: dict | None = None

    async def create(self, **kwargs: object) -> object:
        self.captured = kwargs
        return self._c


class _FakeChat:
    def __init__(self, completions: _FakeCompletions) -> None:
        self.completions = completions


class _FakeOpenAI:
    def __init__(self, completion: object) -> None:
        self.chat = _FakeChat(_FakeCompletions(completion))


def _completion(model: str = "gpt-5.2", cached: int = 0) -> SimpleNamespace:
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="hi"))],
        usage=SimpleNamespace(
            prompt_tokens=1000, completion_tokens=10,
            prompt_tokens_details=SimpleNamespace(cached_tokens=cached),
        ),
        model=model,
    )


async def _call(client: OpenAIChatClient, **over):
    kw = dict(
        model="gpt-5.2", system="STABLE", messages=[{"role": "user", "content": "hi"}],
        tools=[], max_tokens=10, stable_prefix="STABLE",
    )
    kw.update(over)
    return await client.create(**kw)


# --- flag-off byte-identity -------------------------------------------------


async def test_flag_off_sets_no_cache_key() -> None:
    fake = _FakeOpenAI(_completion())
    client = OpenAIChatClient(client=fake, provider="openai", context_reuse=False)
    await _call(client)  # stable_prefix passed, but inert with the flag off
    assert "prompt_cache_key" not in fake.chat.completions.captured


async def test_flag_off_leaves_request_untouched_but_for_the_key() -> None:
    # Turning caching on ADDS exactly `prompt_cache_key` and nothing else.
    off = _FakeOpenAI(_completion())
    on = _FakeOpenAI(_completion())
    await _call(OpenAIChatClient(client=off, provider="openai", context_reuse=False))
    await _call(OpenAIChatClient(client=on, provider="openai", context_reuse=True))
    off_kw = off.chat.completions.captured
    on_kw = {k: v for k, v in on.chat.completions.captured.items() if k != "prompt_cache_key"}
    assert on_kw == off_kw


# --- flag-on emits the key --------------------------------------------------


async def test_flag_on_sets_prompt_cache_key_to_prefix_hash() -> None:
    fake = _FakeOpenAI(_completion())
    client = OpenAIChatClient(client=fake, provider="openai", context_reuse=True)
    resp = await _call(client)
    _, assembled = plan_for_prefix("openai", "STABLE")
    assert fake.chat.completions.captured["prompt_cache_key"] == assembled.stable_prefix_hash
    assert resp.stable_prefix_hash == assembled.stable_prefix_hash


async def test_no_stable_prefix_no_key() -> None:
    # Even with the flag on, if the caller supplies no stable prefix, no key is set.
    fake = _FakeOpenAI(_completion())
    client = OpenAIChatClient(client=fake, provider="openai", context_reuse=True)
    await _call(client, stable_prefix=None)
    assert "prompt_cache_key" not in fake.chat.completions.captured


# --- Gemini (same client, implicit caching) gets NO key ---------------------


async def test_gemini_never_gets_a_key() -> None:
    fake = _FakeOpenAI(_completion(model="gemini-2.5-flash"))
    client = OpenAIChatClient(client=fake, provider="gemini", context_reuse=True)
    await _call(client, model="gemini-2.5-flash")
    # Gemini = provider_default (implicit) ⇒ the capability resolves emit=False ⇒ no key.
    assert "prompt_cache_key" not in fake.chat.completions.captured


# --- factory threads the flag + provider ------------------------------------


def _cfg(tmp_path: Path, **secrets: str):
    cfg = load_config(root=tmp_path, env_file=None)
    cfg.secrets = cfg.secrets.model_copy(update=secrets)
    return cfg


def test_factory_flag_off_builds_non_caching_openai(tmp_path: Path) -> None:
    factory = ClientFactory(_cfg(tmp_path, openai_api_key="k"))
    client = factory.for_route(ModelRoute("openai", "gpt-5.2", text_only=True))
    assert isinstance(client, OpenAIChatClient)
    assert client.context_reuse is False and client._provider == "openai"


def test_factory_flag_on_threads_context_reuse(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path, openai_api_key="k")
    cfg.context_reuse = cfg.context_reuse.model_copy(update={"enabled": True})
    factory = ClientFactory(cfg)
    client = factory.for_route(ModelRoute("openai", "gpt-5.2", text_only=True))
    assert client.context_reuse is True

    gcfg = _cfg(tmp_path, gemini_api_key="k")
    gcfg.context_reuse = gcfg.context_reuse.model_copy(update={"enabled": True})
    groute = ModelRoute("gemini", "gemini-2.5-flash", text_only=True)
    gclient = ClientFactory(gcfg).for_route(groute)
    assert gclient._provider == "gemini"  # provider label drives the (no-op) Gemini capability
