"""Token accounting and cost estimation.

Cost is tracked purely for **observability** — the project optimizes for quality,
never for price (API spend is company-provided). These numbers exist so the audit
log and REPL status bar can show what a session cost, not to drive any decision.

Prices are USD per 1,000,000 tokens, from the Anthropic pricing table. Cache
tokens are billed at multiples of the input rate: a 5-minute ephemeral cache
*write* at ~1.25x and a cache *read* (hit) at ~0.1x.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Price:
    """Per-1M-token price for a model."""

    input: float
    output: float


# Sonnet 5 carries intro pricing ($2/$10 through 2026-08-31); we list the standard
# $3/$15 so the table stays correct once the intro window closes.
PRICES: dict[str, Price] = {
    "claude-fable-5": Price(10.0, 50.0),
    "claude-mythos-5": Price(10.0, 50.0),
    "claude-opus-4-8": Price(5.0, 25.0),
    "claude-opus-4-7": Price(5.0, 25.0),
    "claude-opus-4-6": Price(5.0, 25.0),
    "claude-sonnet-5": Price(3.0, 15.0),
    "claude-sonnet-4-6": Price(3.0, 15.0),
    "claude-haiku-4-5": Price(1.0, 5.0),
}

CACHE_WRITE_MULTIPLIER = 1.25  # 5-minute ephemeral cache write, vs. the input rate
CACHE_READ_MULTIPLIER = 0.10  # cache hit, vs. the input rate


@dataclass(frozen=True)
class Usage:
    """Token counts for one model call, mirroring the Anthropic ``usage`` object."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_creation_input_tokens
            + self.cache_read_input_tokens
        )

    def __add__(self, other: Usage) -> Usage:
        return Usage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cache_creation_input_tokens=self.cache_creation_input_tokens
            + other.cache_creation_input_tokens,
            cache_read_input_tokens=self.cache_read_input_tokens + other.cache_read_input_tokens,
        )

    @classmethod
    def from_response(cls, usage: object) -> Usage:
        """Build from an SDK ``usage`` object or a plain dict (missing fields -> 0)."""

        def get(name: str) -> int:
            value = usage.get(name, 0) if isinstance(usage, dict) else getattr(usage, name, 0)
            return int(value or 0)

        return cls(
            input_tokens=get("input_tokens"),
            output_tokens=get("output_tokens"),
            cache_creation_input_tokens=get("cache_creation_input_tokens"),
            cache_read_input_tokens=get("cache_read_input_tokens"),
        )


def price_for(model: str) -> Price | None:
    """Look up a model's price, tolerating dated snapshot IDs.

    Exact match first, then the longest known prefix so
    ``claude-haiku-4-5-20251001`` resolves to the ``claude-haiku-4-5`` price.
    Returns ``None`` for unknown models.
    """
    if model in PRICES:
        return PRICES[model]
    for key in sorted(PRICES, key=len, reverse=True):
        if model.startswith(key):
            return PRICES[key]
    return None


def cost_of(model: str, usage: Usage) -> float:
    """Estimated USD cost of one model call. Unknown models cost ``0.0``.

    Legacy helper kept for existing callers (REPL status bar, per-run aggregates). The
    Phase 10 ledger uses :class:`PricingTable` / :func:`compute_cost` instead, which fail
    CLOSED (None) on an unknown model rather than returning a silent 0.0."""
    price = price_for(model)
    if price is None:
        return 0.0
    return _dollars(price, usage, CACHE_WRITE_MULTIPLIER, CACHE_READ_MULTIPLIER)


def _dollars(price: Price, usage: Usage, cache_write_mult: float, cache_read_mult: float) -> float:
    dollars = (
        usage.input_tokens * price.input
        + usage.cache_creation_input_tokens * price.input * cache_write_mult
        + usage.cache_read_input_tokens * price.input * cache_read_mult
        + usage.output_tokens * price.output
    )
    return dollars / 1_000_000


# --- Phase 10: versioned, provider-keyed pricing (fail-closed) --------------


@dataclass(frozen=True)
class PricingTable:
    """A versioned pricing table loaded from ``config/pricing.yaml``. Provider-keyed so a
    lookup never crosses provider price spaces: Anthropic tolerates dated-snapshot suffixes
    (longest-prefix), every other provider is matched EXACTLY. Unknown ⇒ ``None`` (the ledger
    records a NULL cost + a warning; orchestration refuses to start) — never a silent $0."""

    version: str
    effective: str
    cache_write_multiplier: float
    cache_read_multiplier: float
    models: dict[str, dict[str, Price]]  # provider -> model -> Price
    services: dict[str, dict] = None  # type: ignore[assignment]  # Phase 10B: service name -> {unit, usd_per_unit}

    def priced_services(self) -> frozenset[str]:
        """Names of services with a pricing entry (a metered service is only 'priced' —
        and therefore usable — when it appears here; unpriced fails closed)."""
        return frozenset(self.services or {})

    def price_for(self, provider: str, model: str) -> Price | None:
        table = self.models.get(provider)
        if not table:
            return None
        if model in table:
            return table[model]
        if provider == "anthropic":  # only Anthropic ids carry dated snapshots
            for key in sorted(table, key=len, reverse=True):
                if model.startswith(key):
                    return table[key]
        return None

    def cost(self, provider: str, model: str, usage: Usage) -> float | None:
        """USD for one call, or ``None`` if the (provider, model) is unpriced (fail-closed)."""
        price = self.price_for(provider, model)
        if price is None:
            return None
        return _dollars(price, usage, self.cache_write_multiplier, self.cache_read_multiplier)


def _code_fallback_table() -> PricingTable:
    """The Anthropic-only table baked into the code — the fallback when pricing.yaml is
    missing or malformed (never invents prices for unknown models)."""
    return PricingTable(
        version="code-fallback",
        effective="",
        cache_write_multiplier=CACHE_WRITE_MULTIPLIER,
        cache_read_multiplier=CACHE_READ_MULTIPLIER,
        models={"anthropic": dict(PRICES)},
        services={},
    )


def load_pricing(path: object | None = None) -> PricingTable:
    """Load the versioned pricing table from ``path`` (a Path to pricing.yaml). A missing or
    malformed file falls back to the code table (logged), so a bad edit degrades to known
    Anthropic prices rather than pricing everything as unknown."""
    from pathlib import Path

    import yaml

    from jarvis.observability import get_logger

    if path is None or not Path(path).is_file():
        return _code_fallback_table()
    try:
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        mult = raw.get("cache_multipliers") or {}
        models: dict[str, dict[str, Price]] = {}
        for provider, table in (raw.get("models") or {}).items():
            models[provider] = {
                model: Price(float(p["input"]), float(p["output"])) for model, p in table.items()
            }
        if not models:
            raise ValueError("no models in pricing table")
        return PricingTable(
            version=str(raw.get("schema_version", "?")),
            effective=str(raw.get("effective", "")),
            cache_write_multiplier=float(mult.get("write", CACHE_WRITE_MULTIPLIER)),
            cache_read_multiplier=float(mult.get("read", CACHE_READ_MULTIPLIER)),
            models=models,
            services=dict(raw.get("services") or {}),
        )
    except Exception as exc:  # noqa: BLE001 - a bad pricing file must not crash startup
        get_logger("jarvis.cost").warning("pricing_load_failed", error=str(exc))
        return _code_fallback_table()
