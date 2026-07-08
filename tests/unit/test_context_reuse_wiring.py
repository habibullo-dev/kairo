"""S7 enable-step wiring (Phase 13 M0): the live clients attach the context-reuse control ONLY
when the flag is on, and the normalized cache usage lands in the ledger. Keyless — injected fake
SDKs capture the on-the-wire kwargs; a tmp SQLite ledger proves the recorded columns.

The load-bearing pin is **flag-off byte-identity**: with caching off, the request the client
sends is identical to a no-caching build (so replay cassettes stay deterministic and a recording
never embeds a cache control). Then: flag-on emits exactly the S7 emitter's control; FakeClient
never emits one; the compat providers we do NOT wire this phase (DeepSeek/Qwen/GLM/Z.ai, which
ride the Anthropic-compat client) never get a control; and a private stable prefix is refused
caching by default (the gate holds end-to-end). OpenAI client emit is Task 2; here the ledger
recording is proven for both providers (it reads the normalized usage, provider-agnostic)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

import jarvis.core  # noqa: F401 - load core first (ledger<->core.context import cycle in isolation)
from jarvis.core.anthropic_client import AnthropicClient
from jarvis.core.client import FakeClient, ModelResponse, text_message
from jarvis.models.context_reuse import anthropic_cache_control, capability, plan_for_prefix
from jarvis.observability.cost import Usage, load_pricing
from jarvis.observability.ledger import CostContext, CostLedger, LedgeredClient, cost_context
from jarvis.persistence.db import connect
from jarvis.ui.readmodels import cache_reuse_overview

_OPEN: list = []


@pytest.fixture(autouse=True)
async def _close():
    yield
    while _OPEN:
        await _OPEN.pop().close()


# --- fake SDKs (capture the on-the-wire kwargs) -----------------------------


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
        content=[SimpleNamespace(type="text", text="hi")],
        stop_reason="end_turn",
        usage=SimpleNamespace(
            input_tokens=100, output_tokens=20,
            cache_creation_input_tokens=0, cache_read_input_tokens=0,
        ),
        model="claude-opus-4-8",
    )
    d.update(over)
    return SimpleNamespace(**d)


async def _anthropic_call(client: AnthropicClient, **over) -> ModelResponse:
    kw = dict(
        model="claude-opus-4-8",
        system="STABLE\n\nVOLATILE",
        messages=[{"role": "user", "content": "hi"}],
        tools=[],
        max_tokens=10,
        stable_prefix="STABLE",
    )
    kw.update(over)
    return await client.create(**kw)


# --- flag-off byte-identity (the load-bearing pin) --------------------------


async def test_flag_off_anthropic_sends_plain_string() -> None:
    fake = _FakeAnthropic(_FakeStream([], _message()))
    client = AnthropicClient(client=fake, context_reuse=False)
    await _anthropic_call(client)  # stable_prefix is passed but must be inert with the flag off
    kw = fake.messages.captured
    assert kw["system"] == "STABLE\n\nVOLATILE"  # a plain string, unchanged
    assert "cache_control" not in str(kw)


async def test_flag_on_changes_only_the_system_field() -> None:
    # Byte-identity of everything else: turning caching on adds a control to `system` and touches
    # NOTHING else in the request.
    off = _FakeAnthropic(_FakeStream([], _message()))
    on = _FakeAnthropic(_FakeStream([], _message()))
    await _anthropic_call(AnthropicClient(client=off, context_reuse=False))
    await _anthropic_call(AnthropicClient(client=on, context_reuse=True))
    off_kw, on_kw = off.messages.captured, on.messages.captured
    assert {k: v for k, v in off_kw.items() if k != "system"} == {
        k: v for k, v in on_kw.items() if k != "system"
    }


# --- flag-on emits exactly the S7 emitter control ---------------------------


async def test_flag_on_anthropic_emits_breakpoint_at_the_seam() -> None:
    fake = _FakeAnthropic(_FakeStream([], _message()))
    client = AnthropicClient(client=fake, context_reuse=True)
    resp = await _anthropic_call(client)
    _, assembled = plan_for_prefix("anthropic", "STABLE")
    # The stable prefix is a cached block; the volatile tail is a SEPARATE, uncached block.
    assert fake.messages.captured["system"] == [
        {"type": "text", "text": "STABLE", "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": "\n\nVOLATILE"},
    ]
    assert resp.stable_prefix_hash == assembled.stable_prefix_hash


async def test_flag_on_anthropic_no_volatile_tail_is_one_block() -> None:
    # system == stable prefix (no extras) ⇒ a single cached block, no empty trailing block.
    fake = _FakeAnthropic(_FakeStream([], _message()))
    client = AnthropicClient(client=fake, context_reuse=True)
    await _anthropic_call(client, system="STABLE", stable_prefix="STABLE")
    assert fake.messages.captured["system"] == [
        {"type": "text", "text": "STABLE", "cache_control": {"type": "ephemeral"}},
    ]


async def test_flag_on_anthropic_non_prefix_falls_back_to_plain() -> None:
    # Defensive: if `stable_prefix` is not actually a prefix of `system`, cache nothing.
    fake = _FakeAnthropic(_FakeStream([], _message()))
    client = AnthropicClient(client=fake, context_reuse=True)
    resp = await _anthropic_call(client, system="DIFFERENT\n\nX", stable_prefix="STABLE")
    assert fake.messages.captured["system"] == "DIFFERENT\n\nX"
    assert resp.stable_prefix_hash is None


# --- no control for the providers we do NOT wire this phase ------------------


@pytest.mark.parametrize("model", ["qwen-max", "deepseek-v4-pro", "glm-4.6", "zai-any"])
async def test_compat_providers_never_get_a_control(model: str) -> None:
    # DeepSeek/Qwen/GLM/Z.ai ride the Anthropic-compat client (compat=True); none is wired this
    # phase, so `system` stays a plain string even with the flag on (the `not compat` guard).
    fake = _FakeAnthropic(_FakeStream(["hi"], _message(model=model)))
    client = AnthropicClient(client=fake, context_reuse=True, compat=True)
    await _anthropic_call(
        client, model=model, tools=[{"name": "e", "description": "d", "input_schema": {}}]
    )
    assert fake.messages.captured["system"] == "STABLE\n\nVOLATILE"
    assert "cache_control" not in str(fake.messages.captured)


# --- FakeClient stays byte-identical ----------------------------------------


async def test_fakeclient_ignores_stable_prefix() -> None:
    fc = FakeClient([text_message("hi")])
    await fc.create(
        model="m", system="s", messages=[], tools=[], max_tokens=1, stable_prefix="s"
    )
    assert fc.calls[0]["system"] == "s"  # recorded as a plain string
    assert "stable_prefix" not in fc.calls[0]  # not recorded ⇒ every cassette stays identical
    assert "cache_control" not in str(fc.calls[0])


# --- private-content gate holds end-to-end ----------------------------------


def test_private_stable_prefix_is_not_cached_by_default() -> None:
    # A SENSITIVE stable prefix is refused caching without explicit route permission — the gate
    # the live clients call into (`plan_for_prefix`) enforces it, so caching can never widen
    # data-flow to a private prefix by default.
    directive, _ = plan_for_prefix("anthropic", "PRIVATE PROJECT MEMORY", sensitive=True)
    assert directive.emit is False
    assert anthropic_cache_control(directive) is None


# --- cached usage → normalized ledger columns → Cost Center -----------------


async def _bare_ledger(tmp_path: Path) -> CostLedger:
    db = await connect(tmp_path / "cr.db")
    _OPEN.append(db)
    return CostLedger(db, asyncio.Lock(), load_pricing(None))


async def _cache_row(ledger: CostLedger) -> dict:
    cur = await ledger.db.execute(
        "SELECT provider_cache_hit_tokens, cached_input_tokens, provider_cache_mode, "
        "estimated_cache_savings_usd, stable_prefix_hash FROM model_calls ORDER BY id"
    )
    cols = ("hit", "cached_input", "mode", "savings", "hash")
    rows = [dict(zip(cols, r, strict=True)) for r in await cur.fetchall()]
    return rows[-1]


async def _record(ledger: CostLedger, provider: str, model: str, usage: Usage, hash_: str) -> None:
    resp = ModelResponse(
        content_blocks=[{"type": "text", "text": "hi"}], stop_reason="end_turn",
        usage=usage, model=model, stable_prefix_hash=hash_,
    )
    client = LedgeredClient(FakeClient([resp]), ledger=ledger, provider=provider, effort=None)
    token = cost_context.set(CostContext(purpose="turn"))
    try:
        await client.create(model=model, system="s", messages=[], tools=[], max_tokens=1)
    finally:
        cost_context.reset(token)


async def test_openai_cache_hit_normalizes_and_surfaces(tmp_path: Path) -> None:
    ledger = await _bare_ledger(tmp_path)
    await _record(
        ledger, "openai", "gpt-5.2",
        Usage(input_tokens=1000, output_tokens=10, cache_read_input_tokens=800), "abc12345",
    )
    row = await _cache_row(ledger)
    assert row["hit"] == 800  # OpenAI cached_tokens → normalized hit
    assert row["cached_input"] == 800  # automatic_prefix providers report cached input tokens
    assert row["mode"] == "automatic_prefix"
    assert row["hash"] == "abc12345"
    cr = await cache_reuse_overview(ledger.db)  # the Cost Center card sees it
    assert cr["totals"]["hit_tokens"] == 800


async def test_anthropic_cache_hit_records_savings(tmp_path: Path) -> None:
    ledger = await _bare_ledger(tmp_path)
    await _record(
        ledger, "anthropic", "claude-opus-4-8",
        Usage(input_tokens=2000, output_tokens=10, cache_read_input_tokens=1500), "def",
    )
    row = await _cache_row(ledger)
    assert row["hit"] == 1500
    assert row["cached_input"] is None  # explicit_breakpoint: no automatic-prefix "cached input"
    assert row["mode"] == "explicit_breakpoint"
    assert row["savings"] is not None and row["savings"] > 0  # priced provider ⇒ real estimate


async def test_no_cache_activity_records_all_null(tmp_path: Path) -> None:
    # A plain call with no cache tokens records every S7 column as NULL (never a fabricated 0).
    ledger = await _bare_ledger(tmp_path)
    await _record(ledger, "anthropic", "claude-opus-4-8", Usage(input_tokens=500), None)
    row = await _cache_row(ledger)
    assert row == {"hit": None, "cached_input": None, "mode": None, "savings": None, "hash": None}


def test_capability_data_matches_wiring() -> None:
    # The wiring keys off capability DATA: openai caches with a key, gemini/zai do not.
    assert capability("openai").supports_cache_key is True
    assert capability("anthropic").mode.value == "explicit_breakpoint"
    assert capability("gemini").mode.value == "provider_default"
    assert capability("zai").supported is False
