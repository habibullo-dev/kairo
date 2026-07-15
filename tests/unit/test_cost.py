"""Cost math tests. Concrete numbers so a pricing typo is caught immediately."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from kira.observability.cost import (
    CACHE_READ_MULTIPLIER,
    CACHE_WRITE_MULTIPLIER,
    Usage,
    cost_of,
    price_for,
)

M = 1_000_000


def test_opus_input_and_output_rates() -> None:
    assert cost_of("claude-opus-4-8", Usage(input_tokens=M)) == pytest.approx(5.0)
    assert cost_of("claude-opus-4-8", Usage(output_tokens=M)) == pytest.approx(25.0)


def test_mixed_input_output() -> None:
    # 200k input @ $5/M + 100k output @ $25/M = 1.0 + 2.5
    cost = cost_of("claude-opus-4-8", Usage(input_tokens=200_000, output_tokens=100_000))
    assert cost == pytest.approx(3.5)


def test_cache_read_and_write_multipliers() -> None:
    read = cost_of("claude-opus-4-8", Usage(cache_read_input_tokens=M))
    write = cost_of("claude-opus-4-8", Usage(cache_creation_input_tokens=M))
    assert read == pytest.approx(5.0 * CACHE_READ_MULTIPLIER)  # 0.50
    assert write == pytest.approx(5.0 * CACHE_WRITE_MULTIPLIER)  # 6.25


def test_other_model_rates() -> None:
    assert cost_of("claude-sonnet-5", Usage(input_tokens=M, output_tokens=M)) == pytest.approx(18.0)
    assert cost_of("claude-haiku-4-5", Usage(input_tokens=M, output_tokens=M)) == pytest.approx(6.0)
    assert cost_of("claude-fable-5", Usage(input_tokens=M, output_tokens=M)) == pytest.approx(60.0)


def test_dated_snapshot_resolves_via_prefix() -> None:
    price = price_for("claude-haiku-4-5-20251001")
    assert price is not None
    assert (price.input, price.output) == (1.0, 5.0)


def test_unknown_model_costs_zero() -> None:
    assert price_for("gpt-4") is None
    assert cost_of("gpt-4", Usage(input_tokens=M, output_tokens=M)) == 0.0


def test_usage_from_response_object_and_dict() -> None:
    obj = SimpleNamespace(input_tokens=10, output_tokens=20)  # cache fields missing -> 0
    u = Usage.from_response(obj)
    assert (u.input_tokens, u.output_tokens) == (10, 20)
    assert u.cache_creation_input_tokens == 0

    u2 = Usage.from_response({"input_tokens": 1, "output_tokens": 2, "cache_read_input_tokens": 3})
    assert (u2.input_tokens, u2.output_tokens, u2.cache_read_input_tokens) == (1, 2, 3)


def test_usage_add_and_total() -> None:
    a = Usage(input_tokens=1, output_tokens=2, cache_read_input_tokens=3)
    b = Usage(input_tokens=10, output_tokens=20, cache_creation_input_tokens=5)
    total = a + b
    assert total.input_tokens == 11
    assert total.output_tokens == 22
    assert total.cache_read_input_tokens == 3
    assert total.cache_creation_input_tokens == 5
    assert total.total_tokens == 11 + 22 + 3 + 5


def test_none_usage_fields_coerced() -> None:
    # SDK may hand back None for absent cache fields.
    u = Usage.from_response({"input_tokens": 5, "cache_creation_input_tokens": None})
    assert u.input_tokens == 5
    assert u.cache_creation_input_tokens == 0
