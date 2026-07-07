"""Versioned, provider-keyed pricing (Phase 10 Task 7).

Pins: provider isolation (an OpenAI model never matches an Anthropic price), Anthropic
dated-snapshot prefix matching, exact-only for other providers (fail-closed), and the
yaml loader's fallback to the code table on a missing/malformed file."""

from __future__ import annotations

from pathlib import Path

from jarvis.observability.cost import Usage, load_pricing


def test_anthropic_prefix_matches_dated_snapshot() -> None:
    table = load_pricing(None)  # code fallback (anthropic only)
    assert table.price_for("anthropic", "claude-opus-4-8") is not None
    # a dated snapshot resolves to the base price (longest-prefix, anthropic only)
    assert table.price_for("anthropic", "claude-haiku-4-5-20251001") is not None


def test_unknown_model_is_unpriced_not_zero() -> None:
    table = load_pricing(None)
    assert table.price_for("anthropic", "totally-unknown-model") is None
    assert table.cost("anthropic", "totally-unknown-model", Usage(input_tokens=1000)) is None


def test_provider_isolation(tmp_path: Path) -> None:
    yaml = tmp_path / "pricing.yaml"
    yaml.write_text(
        "schema_version: 1\neffective: '2026-07-01'\n"
        "cache_multipliers: {write: 1.25, read: 0.10}\n"
        "models:\n"
        "  anthropic: {claude-opus-4-8: {input: 5.0, output: 25.0}}\n"
        "  openai: {gpt-5.2: {input: 1.75, output: 14.0}}\n",
        encoding="utf-8",
    )
    table = load_pricing(yaml)
    # openai is EXACT-only: a prefix that would match under anthropic must NOT match here.
    assert table.price_for("openai", "gpt-5.2") is not None
    assert table.price_for("openai", "gpt-5.2-turbo") is None  # no prefix match for openai
    # a model under the wrong provider doesn't resolve
    assert table.price_for("openai", "claude-opus-4-8") is None


def test_cost_math(tmp_path: Path) -> None:
    table = load_pricing(None)
    # 1M input + 1M output on opus (5/25) = $30
    cost = table.cost(
        "anthropic", "claude-opus-4-8", Usage(input_tokens=1_000_000, output_tokens=1_000_000)
    )
    assert abs(cost - 30.0) < 1e-6


def test_malformed_yaml_falls_back_to_code(tmp_path: Path) -> None:
    bad = tmp_path / "pricing.yaml"
    bad.write_text("this: is: not: valid: yaml: [unclosed", encoding="utf-8")
    table = load_pricing(bad)
    assert table.version == "code-fallback"
    assert table.price_for("anthropic", "claude-opus-4-8") is not None


def test_missing_file_falls_back_to_code(tmp_path: Path) -> None:
    table = load_pricing(tmp_path / "nope.yaml")
    assert table.version == "code-fallback"


def test_real_pricing_yaml_loads() -> None:
    # The shipped config/pricing.yaml parses and prices the default models.
    from jarvis.config import project_root

    table = load_pricing(project_root() / "config" / "pricing.yaml")
    assert table.price_for("anthropic", "claude-fable-5") is not None
    assert table.price_for("openai", "gpt-5.2") is not None
