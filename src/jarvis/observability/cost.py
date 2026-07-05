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
    """Estimated USD cost of one model call. Unknown models cost ``0.0``."""
    price = price_for(model)
    if price is None:
        return 0.0
    dollars = (
        usage.input_tokens * price.input
        + usage.cache_creation_input_tokens * price.input * CACHE_WRITE_MULTIPLIER
        + usage.cache_read_input_tokens * price.input * CACHE_READ_MULTIPLIER
        + usage.output_tokens * price.output
    )
    return dollars / 1_000_000
