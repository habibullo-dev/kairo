"""Context-reuse capability metadata + fail-closed resolver (S7.1). Keyless, pure.

Pins: each provider's declared mode; unknown / unsupported / Z.ai resolve to OFF; every declared
mode is valid; and the safety invariant that private-content caching is only ever permitted on a
provider that may receive private context at all (private_ok)."""

from __future__ import annotations

from kira.models.context_reuse import ContextReuseMode, capability
from kira.models.providers import PROVIDER_CATALOG


def test_anthropic_capability() -> None:
    c = capability("anthropic")
    assert c.supported and c.mode is ContextReuseMode.EXPLICIT_BREAKPOINT
    assert c.supports_cache_ttl and c.cache_ttl_options == ("5m", "1h")
    assert c.reports_cached_tokens and c.cache_private_allowed  # the only private-ok provider


def test_openai_capability() -> None:
    c = capability("openai")
    assert c.supported and c.mode is ContextReuseMode.AUTOMATIC_PREFIX
    assert c.supports_cache_key and not c.cache_private_allowed


def test_deepseek_and_qwen_and_gemini() -> None:
    assert capability("deepseek").mode is ContextReuseMode.AUTOMATIC_PREFIX
    assert capability("qwen").mode is ContextReuseMode.EXPLICIT_BREAKPOINT
    assert capability("gemini").mode is ContextReuseMode.PROVIDER_DEFAULT
    assert capability("gemini").reports_cached_tokens


def test_zai_is_off_until_verified() -> None:
    c = capability("zai")
    assert not c.supported and c.mode is ContextReuseMode.OFF


def test_unknown_provider_is_off() -> None:
    c = capability("totally-unknown-provider")
    assert not c.supported and c.mode is ContextReuseMode.OFF
    assert not c.cache_private_allowed and c.cache_ttl_options == ()


def test_bad_mode_string_fails_closed_to_off(monkeypatch) -> None:
    # A catalog spec with an unrecognized mode must resolve to OFF, not crash or pass it through.
    from dataclasses import replace

    import kira.models.context_reuse as cr

    spec = replace(PROVIDER_CATALOG["anthropic"], context_reuse_mode="turbo-cache-9000")
    monkeypatch.setattr(cr, "provider_spec", lambda name: spec)
    assert capability("anthropic").mode is ContextReuseMode.OFF


def test_every_declared_mode_is_valid() -> None:
    valid = {m.value for m in ContextReuseMode}
    for name, spec in PROVIDER_CATALOG.items():
        assert spec.context_reuse_mode in valid, (name, spec.context_reuse_mode)
        # A supported provider must not declare OFF, and vice versa.
        assert spec.supports_context_reuse == (spec.context_reuse_mode != "off"), name


def test_private_caching_only_where_private_context_is_allowed() -> None:
    # THE safety pin: no provider may permit caching private content unless it may receive
    # private context at all. Prevents a cheap worker from ever caching private prefixes.
    for name, spec in PROVIDER_CATALOG.items():
        if spec.cache_private_allowed:
            assert spec.private_ok, f"{name}: cache_private_allowed but not private_ok"
