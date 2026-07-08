"""Cassette layer for eval cost control (E1): replay/record/live + fail-closed + cost cap."""

from __future__ import annotations

from pathlib import Path

import pytest
from tests.evals.cassette import (
    CassetteClient,
    CassetteConfig,
    CassetteMissError,
    CassetteStore,
    CostCap,
    CostCapExceeded,
    cassette_key,
    wrap,
)

from jarvis.core.client import ModelResponse, ToolCall, text_message, tool_use_message
from jarvis.observability.cost import Price, PricingTable, Usage


def _pricing() -> PricingTable:
    return PricingTable(
        version="t",
        effective="t",
        cache_write_multiplier=1.25,
        cache_read_multiplier=0.1,
        models={"deepseek": {"deepseek-v4-flash": Price(0.14, 0.28)}, "anthropic": {}},
        services={},
    )


class _Inner:
    """A scripted live client that also records how many real calls happened."""

    def __init__(
        self, responses: list[ModelResponse], *, effort="high", thinking=True, compat=False
    ):
        self.responses = list(responses)
        self.effort, self.thinking, self.compat = effort, thinking, compat
        self.calls = 0

    async def create(self, **kw) -> ModelResponse:
        self.calls += 1
        return self.responses.pop(0)


_REQ = dict(
    model="deepseek-v4-flash",
    system="s",
    messages=[{"role": "user", "content": "hi"}],
    tools=[],
    max_tokens=100,
)

# The output-affecting client config — passed identically to record and replay wraps so the
# cassette key matches even though the replay client has no live inner to introspect.
_SIG = {"effort": "high", "thinking": True, "compat": True}


def _cfg(tmp_path: Path, mode: str, max_cost=None) -> CassetteConfig:
    return CassetteConfig(mode=mode, store_dir=tmp_path / "cassettes", max_cost_usd=max_cost)


# --- key stability ----------------------------------------------------------


def test_key_is_stable_and_sensitive_to_inputs() -> None:
    base = dict(
        provider="deepseek",
        signature={"compat": True},
        model="m",
        system="s",
        messages=[{"role": "user", "content": "x"}],
        tools=[],
        max_tokens=10,
        tool_choice=None,
        temperature=None,
    )
    k1 = cassette_key(**base)
    assert k1 == cassette_key(**base)  # stable
    assert k1 != cassette_key(**{**base, "system": "different"})
    assert k1 != cassette_key(**{**base, "signature": {"compat": False}})  # config affects output


def test_key_normalizes_random_temp_workdir() -> None:
    # Eval scenarios run in a random temp workdir; that path leaks into tool results. The key
    # must normalize it out so replay is deterministic across runs (same machine).
    def _k(wd: str) -> str:
        return cassette_key(
            provider="anthropic", signature={}, model="m", system="s",
            messages=[{"role": "user", "content": f"wrote 91 bytes to {wd}\\summary.md"}],
            tools=[], max_tokens=10, tool_choice=None, temperature=None,
        )

    a = _k("C:\\Users\\h\\AppData\\Local\\Temp\\jarvis-eval-aaaa111")
    b = _k("C:\\Users\\h\\AppData\\Local\\Temp\\jarvis-eval-bbbb222")
    assert a == b  # random workdir normalized ⇒ deterministic replay key


# --- replay fails closed on a miss ------------------------------------------


async def test_replay_miss_fails_closed(tmp_path: Path) -> None:
    client = wrap(
        None, provider="deepseek", cfg=_cfg(tmp_path, "replay"), pricing=_pricing(), signature=_SIG
    )
    with pytest.raises(CassetteMissError):
        await client.create(**_REQ)


# --- record then replay -----------------------------------------------------


async def test_record_then_replay_no_second_live_call(tmp_path: Path) -> None:
    inner = _Inner(
        [
            text_message(
                "cached answer",
                model="deepseek-v4-flash",
                usage=Usage(input_tokens=10, output_tokens=5),
            )
        ],
        compat=True,
    )
    rec = wrap(
        inner,
        provider="deepseek",
        cfg=_cfg(tmp_path, "record", max_cost=1.0),
        pricing=_pricing(),
        signature=_SIG,
    )
    r1 = await rec.create(**_REQ)
    assert r1.text == "cached answer" and inner.calls == 1 and rec.recorded == 1

    # A fresh replay client over the same store returns the cassette with NO live call available.
    rep = wrap(
        None, provider="deepseek", cfg=_cfg(tmp_path, "replay"), pricing=_pricing(), signature=_SIG
    )
    r2 = await rep.create(**_REQ)
    assert r2.text == "cached answer" and rep.hits == 1


async def test_replay_streams_cached_text(tmp_path: Path) -> None:
    inner = _Inner(
        [
            text_message(
                "hello world",
                model="deepseek-v4-flash",
                usage=Usage(input_tokens=3, output_tokens=2),
            )
        ],
        compat=True,
    )
    await wrap(
        inner,
        provider="deepseek",
        cfg=_cfg(tmp_path, "record", max_cost=1.0),
        pricing=_pricing(),
        signature=_SIG,
    ).create(**_REQ)
    chunks: list[str] = []
    rep = wrap(
        None, provider="deepseek", cfg=_cfg(tmp_path, "replay"), pricing=_pricing(), signature=_SIG
    )
    await rep.create(**_REQ, on_text_delta=chunks.append)
    assert chunks == ["hello world"]


async def test_record_preserves_tool_calls(tmp_path: Path) -> None:
    resp = tool_use_message(
        [ToolCall(id="t1", name="read_file", input={"path": "a"})],
        model="deepseek-v4-flash",
        usage=Usage(input_tokens=4, output_tokens=3),
    )
    inner = _Inner([resp], compat=True)
    await wrap(
        inner,
        provider="deepseek",
        cfg=_cfg(tmp_path, "record", max_cost=1.0),
        pricing=_pricing(),
        signature=_SIG,
    ).create(**_REQ)
    rep = wrap(
        None, provider="deepseek", cfg=_cfg(tmp_path, "replay"), pricing=_pricing(), signature=_SIG
    )
    r = await rep.create(**_REQ)
    assert r.tool_calls[0].name == "read_file" and r.tool_calls[0].input == {"path": "a"}


# --- live always calls through + records ------------------------------------


async def test_live_calls_through_even_on_hit(tmp_path: Path) -> None:
    first = text_message(
        "v1", model="deepseek-v4-flash", usage=Usage(input_tokens=2, output_tokens=1)
    )
    second = text_message(
        "v2", model="deepseek-v4-flash", usage=Usage(input_tokens=2, output_tokens=1)
    )
    inner = _Inner([first, second], compat=True)
    live = wrap(
        inner,
        provider="deepseek",
        cfg=_cfg(tmp_path, "live", max_cost=1.0),
        pricing=_pricing(),
        signature=_SIG,
    )
    assert (await live.create(**_REQ)).text == "v1"
    assert (await live.create(**_REQ)).text == "v2"  # live refreshes; no replay shortcut
    assert inner.calls == 2


# --- cost cap ---------------------------------------------------------------


async def test_cost_cap_aborts_when_exceeded(tmp_path: Path) -> None:
    # Each call ~ (1000*0.14 + 1000*0.28)/1e6 = $0.00042; cap tiny so the 2nd guard trips.
    big = Usage(input_tokens=1_000_000, output_tokens=1_000_000)  # ~$0.42 per call
    inner = _Inner(
        [
            text_message("a", model="deepseek-v4-flash", usage=big),
            text_message("b", model="deepseek-v4-flash", usage=big),
        ],
        compat=True,
    )
    cap = CostCap(0.5, _pricing())
    client = CassetteClient(
        inner, provider="deepseek", store=CassetteStore(tmp_path / "c"), mode="live", cost_cap=cap
    )
    await client.create(**_REQ)  # ~$0.42 spent, under $0.50
    with pytest.raises(CostCapExceeded):
        await client.create(**_REQ)  # guard_before_call sees spent >= cap ⇒ abort


async def test_cost_cap_unpriced_model_fails_closed(tmp_path: Path) -> None:
    inner = _Inner(
        [text_message("x", model="mystery", usage=Usage(input_tokens=5, output_tokens=5))]
    )
    cap = CostCap(1.0, _pricing())  # "mystery" not priced
    client = CassetteClient(
        inner,
        provider="unknownprov",
        store=CassetteStore(tmp_path / "c"),
        mode="live",
        cost_cap=cap,
    )
    with pytest.raises(CostCapExceeded, match="unpriced"):
        await client.create(**_REQ)


async def test_no_cap_does_not_track(tmp_path: Path) -> None:
    inner = _Inner(
        [
            text_message(
                "x", model="deepseek-v4-flash", usage=Usage(input_tokens=9, output_tokens=9)
            )
        ],
        compat=True,
    )
    client = wrap(
        inner,
        provider="deepseek",
        cfg=_cfg(tmp_path, "live", max_cost=None),
        pricing=_pricing(),
        signature=_SIG,
    )
    await client.create(**_REQ)  # no cap ⇒ no CostCapExceeded regardless
    assert inner.calls == 1


# --- E6a: external-service cassettes (embeddings + web) ---------------------


async def test_embedder_replay_fails_closed(tmp_path: Path) -> None:
    from tests.evals.cassette import CassetteEmbedder

    emb = CassetteEmbedder(None, store=CassetteStore(tmp_path / "e"), mode="replay", model="m")
    with pytest.raises(CassetteMissError):
        await emb.embed_query("hello")  # no cassette + no live client ⇒ fail closed


async def test_embedder_record_then_replay(tmp_path: Path) -> None:
    from tests.evals.cassette import CassetteEmbedder

    class _FakeEmb:
        calls = 0

        async def embed_query(self, t: str):
            _FakeEmb.calls += 1
            return [0.1, 0.2]

        async def embed_documents(self, ts):
            return [[0.3]] * len(ts)

    d = tmp_path / "e"
    rec = CassetteEmbedder(_FakeEmb(), store=CassetteStore(d), mode="record", model="m")
    assert await rec.embed_query("hi") == [0.1, 0.2] and _FakeEmb.calls == 1
    rep = CassetteEmbedder(None, store=CassetteStore(d), mode="replay", model="m")
    assert await rep.embed_query("hi") == [0.1, 0.2]  # replay hit; no live embedder needed
    assert _FakeEmb.calls == 1  # replay made no live call


async def test_web_tool_replay_fails_closed(tmp_path: Path) -> None:
    from tests.evals.cassette import wrap_web_tool

    class _P:
        def model_dump(self):
            return {"query": "x"}

    class _T:
        name = "web_search"

        async def run(self, params):
            raise AssertionError("live web call must not happen on replay")

    t = _T()
    wrap_web_tool(t, store=CassetteStore(tmp_path / "w"), mode="replay")
    with pytest.raises(CassetteMissError):
        await t.run(_P())


async def test_web_tool_record_then_replay(tmp_path: Path) -> None:
    from tests.evals.cassette import wrap_web_tool

    from jarvis.tools.base import ToolResult

    calls = {"n": 0}

    class _P:
        def model_dump(self):
            return {"query": "x"}

    class _T:
        name = "web_search"

        async def run(self, params):
            calls["n"] += 1
            return ToolResult(content="live results")

    d = tmp_path / "w"
    rec = _T()
    wrap_web_tool(rec, store=CassetteStore(d), mode="record")
    assert (await rec.run(_P())).content == "live results" and calls["n"] == 1
    rep = _T()
    wrap_web_tool(rep, store=CassetteStore(d), mode="replay")
    assert (await rep.run(_P())).content == "live results"  # replay hit
    assert calls["n"] == 1  # replay made no live call


# --- E6b: frozen eval clock -------------------------------------------------


def test_eval_clock_is_deterministic(monkeypatch: pytest.MonkeyPatch) -> None:
    from jarvis.core.agent import _default_now

    monkeypatch.setenv("JARVIS_EVAL_CLOCK", "2026-01-01T12:00:00+00:00")
    a, b = _default_now(), _default_now()
    assert a == b and a.year == 2026 and a.hour == 12
    monkeypatch.delenv("JARVIS_EVAL_CLOCK")
    assert _default_now().year >= 2026  # unset ⇒ real wall clock


def test_scheduler_clock_respects_eval_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    from jarvis.scheduler.service import utc_now

    monkeypatch.setenv("JARVIS_EVAL_CLOCK", "2026-01-01T12:00:00+00:00")
    t = utc_now()
    assert (t.year, t.month, t.day, t.hour) == (2026, 1, 1, 12) and t.tzinfo is not None
    monkeypatch.delenv("JARVIS_EVAL_CLOCK")
    assert utc_now().year >= 2026  # unset ⇒ real wall clock
