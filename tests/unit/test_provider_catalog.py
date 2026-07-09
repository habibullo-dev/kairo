"""Provider catalog invariants + fail-closed availability (Phase 10C, T1)."""

from __future__ import annotations

from jarvis.models.providers import (
    PROVIDER_CATALOG,
    TRUSTED_AUTHORITY_PROVIDERS,
    ProviderRegistry,
    ProviderState,
)

_API_STYLES = {"anthropic", "anthropic_compat", "openai_compat"}


def test_catalog_fully_classified() -> None:
    assert set(PROVIDER_CATALOG) == {"anthropic", "openai", "deepseek", "qwen", "zai", "gemini"}
    for name, spec in PROVIDER_CATALOG.items():
        assert spec.name == name
        assert spec.api_style in _API_STYLES
        assert spec.credential_env  # every provider names its key env var
        # tool-capable ⇔ an anthropic-style client (native or compat); openai_compat is text-only
        assert spec.tool_capable == (spec.api_style in ("anthropic", "anthropic_compat"))
        # opt-in providers ship a default endpoint; core providers use the SDK default
        assert (spec.default_base_url is None) == spec.core


def test_trusted_is_anthropic_only_and_private_ok_is_the_15_6_set() -> None:
    # The load-bearing authority invariant: exactly ONE trusted-authority provider (anthropic —
    # planner/judge/utility stay on Claude). Phase 15.6 (Habib-approved) widens private_ok to
    # {anthropic, gemini, openai} so the cost-aware Auto router may route the private main chat to
    # Gemini/OpenAI too; the cheap workers (deepseek/qwen/zai) stay private_ok=False.
    assert set(TRUSTED_AUTHORITY_PROVIDERS) == {"anthropic"}
    trusted = {n for n, s in PROVIDER_CATALOG.items() if s.trusted_authority}
    private = {n for n, s in PROVIDER_CATALOG.items() if s.private_ok}
    assert trusted == {"anthropic"}
    assert private == {"anthropic", "gemini", "openai"}
    for name in ("deepseek", "qwen", "zai"):
        assert not PROVIDER_CATALOG[name].trusted_authority
        assert not PROVIDER_CATALOG[name].private_ok  # non-private workers (fail-closed)
    for name in ("gemini", "openai"):
        assert PROVIDER_CATALOG[name].private_ok  # private-capable...
        assert not PROVIDER_CATALOG[name].trusted_authority  # ...but never final authority


def test_core_providers_are_exactly_anthropic_and_openai() -> None:
    core = {n for n, s in PROVIDER_CATALOG.items() if s.core}
    assert core == {"anthropic", "openai"}


def test_zai_uses_bearer_auth_others_x_api_key() -> None:
    # Verified auth nuance: Z.ai wants Authorization: Bearer; DeepSeek/Qwen want x-api-key.
    assert PROVIDER_CATALOG["zai"].auth_style == "bearer"
    for name in ("deepseek", "qwen"):
        assert PROVIDER_CATALOG[name].auth_style == "x-api-key"


def _reg(enabled, priced, env):
    return ProviderRegistry(enabled=enabled, priced_providers=frozenset(priced), env=env)


def test_availability_matrix_opt_in_provider() -> None:
    key = {"DEEPSEEK_API_KEY": "sk-x"}
    # not enabled ⇒ DISABLED (even with key + pricing)
    assert _reg([], ["deepseek"], key).state("deepseek") is ProviderState.DISABLED
    # enabled, no key ⇒ MISSING_CREDENTIALS
    no_key = _reg(["deepseek"], ["deepseek"], {})
    assert no_key.state("deepseek") is ProviderState.MISSING_CREDENTIALS
    # enabled + key, no pricing ⇒ UNPRICED (fail closed)
    assert _reg(["deepseek"], [], key).state("deepseek") is ProviderState.UNPRICED
    # enabled + key + priced ⇒ AVAILABLE
    assert _reg(["deepseek"], ["deepseek"], key).is_available("deepseek")


def test_core_provider_not_gated_by_enabled() -> None:
    key = {"ANTHROPIC_API_KEY": "sk-x"}
    # anthropic is core: available with key + pricing even though it's not in `enabled`.
    assert _reg([], ["anthropic"], key).is_available("anthropic")
    # but still fails closed without a key.
    assert _reg([], ["anthropic"], {}).state("anthropic") is ProviderState.MISSING_CREDENTIALS


def test_qwen_ships_unpriced_fail_closed() -> None:
    # Qwen has a catalog entry but no official pricing was verified ⇒ it must fail closed.
    key = {"DASHSCOPE_API_KEY": "sk-x"}
    assert _reg(["qwen"], [], key).state("qwen") is ProviderState.UNPRICED
    assert not _reg(["qwen"], [], key).is_available("qwen")


def test_unknown_provider_is_unknown() -> None:
    assert _reg(["deepseek"], ["deepseek"], {}).state("mystery") is ProviderState.UNKNOWN


def test_pricing_covers_priced_providers_including_qwen() -> None:
    # T5: the real pricing table prices deepseek/zai/gemini/qwen (+ core anthropic/openai).
    # Qwen3-Coder is context-tiered upstream; pricing.yaml carries representative flat rates
    # (Habib-provided DashScope numbers) so the coder route is priced, not fail-closed.
    from pathlib import Path

    from jarvis.observability.cost import load_pricing

    pricing = load_pricing(Path("config/pricing.yaml"))
    priced = pricing.priced_providers()
    assert {"anthropic", "openai", "deepseek", "zai", "gemini", "qwen"} <= priced
    assert pricing.price_for("qwen", "qwen3-coder-plus") is not None
    assert pricing.price_for("deepseek", "deepseek-v4-flash") is not None
    assert pricing.price_for("gemini", "gemini-2.5-flash") is not None
    assert pricing.price_for("deepseek", "deepseek-does-not-exist") is None  # exact-match only


def test_availability_view_has_no_secret_values() -> None:
    reg = _reg(["deepseek"], ["deepseek"], {"DEEPSEEK_API_KEY": "sk-SECRET-do-not-leak"})
    view = reg.availability()
    blob = repr(view)
    assert "sk-SECRET-do-not-leak" not in blob
    row = next(r for r in view if r["name"] == "deepseek")
    assert row["credentials_present"] is True
    assert row["credential_env"] == ["DEEPSEEK_API_KEY"]  # name only
    assert row["state"] == "available"
