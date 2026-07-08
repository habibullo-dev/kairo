"""Cassette (VCR) layer for eval model calls — cost control.

A :class:`CassetteClient` decorates any :class:`~jarvis.core.client.LLMClient` (same pattern as
``LedgeredClient``) and intercepts every ``create`` call:

* **replay** (default): a cassette HIT returns the cached :class:`ModelResponse`; a MISS fails
  closed (:class:`CassetteMissError`) — never a silent live call. Keyless, $0, deterministic.
* **record**: HIT → cached; MISS → live call + record (fills the cache cheaply). Cost-capped.
* **live**: always call live + record (refresh), for adapter fidelity. Cost-capped.

The cassette key hashes provider + client signature (effort/thinking/compat) + model + system +
messages + tools + max_tokens + tool_choice + temperature — the full determinant of the response.
Replay is deterministic because eval tools run over temp-dir fixtures, so each call's ``messages``
reproduce given the prior cached responses. Cassettes store model OUTPUT only (assistant content
+ usage) and are committed to git so keyless CI/dev replay needs no API key.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from jarvis.core.client import ModelResponse
from jarvis.observability.cost import PricingTable, Usage

Mode = Literal["replay", "record", "live"]


class CassetteMissError(RuntimeError):
    """Replay mode hit a request with no cassette. Fail closed — never call live implicitly."""


class CostCapExceeded(RuntimeError):
    """A live/record run reached its ``--max-cost-usd`` hard cap (or hit an unpriced model under
    a cap, which cannot be measured). The run aborts rather than spend unbounded/untracked."""


def _canonical(obj: object) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=True, default=str)


def cassette_key(
    *,
    provider: str,
    signature: dict,
    model: str,
    system: str,
    messages: list[dict],
    tools: list[dict],
    max_tokens: int,
    tool_choice: dict | None,
    temperature: float | None,
) -> str:
    """Stable sha256 over the full request determinant. Not reversible — the stored value is the
    response, and the key never leaks system/messages content."""
    blob = _canonical(
        {
            "provider": provider,
            "signature": signature,
            "model": model,
            "system": system,
            "messages": messages,
            "tools": tools,
            "max_tokens": max_tokens,
            "tool_choice": tool_choice,
            "temperature": temperature,
        }
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def signature_of(client: object | None) -> dict:
    """The output-affecting client config (effort/thinking/compat) for the cassette key. Prefer
    computing this from config (stable across record/replay); introspecting a live client is a
    convenience for when one is present."""
    return {
        k: getattr(client, k)
        for k in ("effort", "thinking", "compat")
        if client is not None and hasattr(client, k)
    }


def _response_to_dict(r: ModelResponse) -> dict:
    return {
        "content_blocks": r.content_blocks,
        "stop_reason": r.stop_reason,
        "model": r.model,
        "usage": {
            "input_tokens": r.usage.input_tokens,
            "output_tokens": r.usage.output_tokens,
            "cache_creation_input_tokens": r.usage.cache_creation_input_tokens,
            "cache_read_input_tokens": r.usage.cache_read_input_tokens,
        },
    }


def _response_from_dict(d: dict) -> ModelResponse:
    u = d.get("usage") or {}
    return ModelResponse(
        content_blocks=list(d.get("content_blocks") or []),
        stop_reason=d.get("stop_reason") or "end_turn",
        usage=Usage(
            input_tokens=int(u.get("input_tokens", 0)),
            output_tokens=int(u.get("output_tokens", 0)),
            cache_creation_input_tokens=int(u.get("cache_creation_input_tokens", 0)),
            cache_read_input_tokens=int(u.get("cache_read_input_tokens", 0)),
        ),
        model=d.get("model") or "cassette",
        latency_ms=0.0,  # replayed — no real latency
    )


class CassetteStore:
    """Reads/writes one JSON file per cassette key under ``dir`` (committed to git)."""

    def __init__(self, directory: Path) -> None:
        self.dir = directory

    def _path(self, key: str) -> Path:
        return self.dir / f"{key}.json"

    def has(self, key: str) -> bool:
        return self._path(key).is_file()

    def get(self, key: str) -> ModelResponse:
        data = json.loads(self._path(key).read_text(encoding="utf-8"))
        return _response_from_dict(data["response"])

    def put(self, key: str, response: ModelResponse, *, meta: dict) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        payload = {"meta": meta, "response": _response_to_dict(response)}
        self._path(key).write_text(_canonical(payload), encoding="utf-8")

    def count(self) -> int:
        return len(list(self.dir.glob("*.json"))) if self.dir.is_dir() else 0


class CostCap:
    """Tracks cumulative LIVE spend against a hard cap. Inactive when ``max_usd`` is None."""

    def __init__(self, max_usd: float | None, pricing: PricingTable) -> None:
        self.max_usd = max_usd
        self.pricing = pricing
        self.spent = 0.0

    def guard_before_call(self) -> None:
        """Refuse to START another live call if already at/over the cap (bounds the overshoot to
        at most one in-flight call)."""
        if self.max_usd is not None and self.spent >= self.max_usd:
            raise CostCapExceeded(
                f"cost cap ${self.max_usd:.2f} reached (spent ${self.spent:.4f}) — aborting before "
                f"the next live call"
            )

    def charge(self, provider: str, model: str, usage: Usage) -> None:
        if self.max_usd is None:
            return  # no cap configured ⇒ don't track/charge
        cost = self.pricing.cost(provider, model, usage)
        if cost is None:
            raise CostCapExceeded(
                f"model {provider}/{model} is unpriced — cannot enforce a ${self.max_usd:.2f} cost "
                f"cap on an unmeasurable call (fail closed)"
            )
        self.spent += cost
        if self.spent > self.max_usd:
            raise CostCapExceeded(
                f"cost cap ${self.max_usd:.2f} exceeded (spent ${self.spent:.4f})"
            )


class CassetteClient:
    """LLMClient decorator that records/replays model calls. ``clock`` is injectable for tests
    (recorded_at); it defaults to a fixed marker so cassettes stay reproducible in CI."""

    def __init__(
        self,
        inner: object | None,
        *,
        provider: str,
        signature: dict | None = None,
        store: CassetteStore,
        mode: Mode = "replay",
        cost_cap: CostCap | None = None,
        scenario: str = "",
        clock: Callable[[], str] | None = None,
    ) -> None:
        self.inner = inner
        self.provider = provider
        # The client config that affects output (effort/thinking/compat). MUST be supplied
        # explicitly so the key is identical whether recording (inner present) or replaying
        # (inner is None). Falls back to introspecting a present inner client for convenience.
        self.signature = signature if signature is not None else signature_of(inner)
        self.store = store
        self.mode = mode
        self.cost_cap = cost_cap
        self.scenario = scenario
        self._clock = clock or (lambda: "recorded")
        self._seq = 0
        self.hits = 0
        self.recorded = 0

    async def create(
        self,
        *,
        model: str,
        system: str,
        messages: list[dict],
        tools: list[dict],
        max_tokens: int,
        on_text_delta: Callable[[str], None] | None = None,
        tool_choice: dict | None = None,
        temperature: float | None = None,
    ) -> ModelResponse:
        self._seq += 1
        key = cassette_key(
            provider=self.provider,
            signature=self.signature,
            model=model,
            system=system,
            messages=messages,
            tools=tools,
            max_tokens=max_tokens,
            tool_choice=tool_choice,
            temperature=temperature,
        )
        if self.mode != "live" and self.store.has(key):
            self.hits += 1
            resp = self.store.get(key)
            if on_text_delta and resp.text:
                on_text_delta(resp.text)  # replay streamed text for the consumer
            return resp
        if self.mode == "replay":
            raise CassetteMissError(
                f"no cassette for {self.provider}/{model} (scenario {self.scenario!r}, call "
                f"#{self._seq}); run with --record (or --live) to record it. key={key[:12]}"
            )
        # record / live: make the real call under the cost cap, then persist.
        if self.cost_cap is not None:
            self.cost_cap.guard_before_call()
        assert self.inner is not None, "live/record mode needs a real inner client"
        resp = await self.inner.create(
            model=model,
            system=system,
            messages=messages,
            tools=tools,
            max_tokens=max_tokens,
            on_text_delta=on_text_delta,
            tool_choice=tool_choice,
            temperature=temperature,
        )
        if self.cost_cap is not None:
            self.cost_cap.charge(self.provider, resp.model or model, resp.usage)
        self.store.put(
            key,
            resp,
            meta={
                "provider": self.provider,
                "model": model,
                "scenario": self.scenario,
                "seq": self._seq,
                "recorded_at": self._clock(),
            },
        )
        self.recorded += 1
        return resp


@dataclass(frozen=True)
class CassetteConfig:
    """How the runner should build cassette clients for a run."""

    mode: Mode
    store_dir: Path
    max_cost_usd: float | None = None

    def cost_cap(self, pricing: PricingTable) -> CostCap | None:
        return None if self.mode == "replay" else CostCap(self.max_cost_usd, pricing)


def _fixed_clock() -> str:
    # Cassettes must be reproducible; timestamps in tests are pinned via the injected clock.
    return "recorded"


def wrap(
    inner: object | None,
    *,
    provider: str,
    cfg: CassetteConfig,
    pricing: PricingTable,
    signature: dict | None = None,
    cost_cap: CostCap | None = None,
    scenario: str = "",
) -> CassetteClient:
    """Build a CassetteClient for ``inner`` per ``cfg`` (shared ``cost_cap`` across clients so one
    cap bounds the whole run). ``signature`` should be computed from config so it is identical
    across record and replay; it defaults to introspecting ``inner`` when present."""
    return CassetteClient(
        inner,
        provider=provider,
        signature=signature,
        store=CassetteStore(cfg.store_dir),
        mode=cfg.mode,
        cost_cap=cost_cap if cost_cap is not None else cfg.cost_cap(pricing),
        scenario=scenario,
        clock=_fixed_clock,
    )
