"""Context-reuse policy: directive per mode + the private-content gate + emitters (S7.3). Keyless.

Pins the exact control each provider would emit, and the safety gate: a sensitive (private/
project) stable prefix is cached ONLY on a provider that permits private caching AND when the
route explicitly allows it; otherwise nothing is cached (default = stable non-sensitive only).
"""

from __future__ import annotations

from jarvis.models.context_reuse import (
    ContextReuseMode,
    anthropic_cache_control,
    capability,
    openai_prompt_cache_key,
    plan,
)
from jarvis.models.prompt_layout import PromptSection, SectionKind, assemble


def _prompt(sensitive: bool = False):
    return assemble(
        [
            PromptSection(SectionKind.SYSTEM_CONTRACT, "You are Kairo. Safety contract."),
            PromptSection(SectionKind.TOOL_SCHEMAS, "tools: read_file"),
            PromptSection(
                SectionKind.PROJECT_POLICY, "private project detail", sensitive=sensitive
            ),
            PromptSection(SectionKind.USER_TURN, "hi"),
        ]
    )


def test_anthropic_emits_explicit_breakpoint() -> None:
    d = plan(capability("anthropic"), _prompt())
    assert d.emit and d.breakpoint and d.mode is ContextReuseMode.EXPLICIT_BREAKPOINT
    assert anthropic_cache_control(d) == {"type": "ephemeral"}


def test_anthropic_ttl_only_when_supported_and_offered() -> None:
    d = plan(capability("anthropic"), _prompt(), ttl="1h")
    assert anthropic_cache_control(d) == {"type": "ephemeral", "ttl": "1h"}
    bad = plan(capability("anthropic"), _prompt(), ttl="9d")  # not in cache_ttl_options
    assert anthropic_cache_control(bad) == {"type": "ephemeral"}  # unknown TTL dropped


def test_openai_emits_prompt_cache_key() -> None:
    p = _prompt()
    d = plan(capability("openai"), p)
    assert d.emit and d.mode is ContextReuseMode.AUTOMATIC_PREFIX
    assert openai_prompt_cache_key(d) == p.stable_prefix_hash
    assert anthropic_cache_control(d) is None  # no breakpoint for automatic-prefix providers


def test_deepseek_automatic_prefix_without_cache_key() -> None:
    d = plan(capability("deepseek"), _prompt())
    assert d.emit and d.cache_key is None  # relies on stable ordering; no key control
    assert openai_prompt_cache_key(d) is None


def test_gemini_provider_default_emits_nothing() -> None:
    d = plan(capability("gemini"), _prompt())
    assert not d.emit and "implicit" in d.reason  # defer to the provider


def test_zai_and_unknown_emit_nothing() -> None:
    assert not plan(capability("zai"), _prompt()).emit
    assert not plan(capability("unknown-x"), _prompt()).emit


def test_private_prefix_not_cached_without_route_permission() -> None:
    # anthropic MAY cache private (cache_private_allowed) — but only when the route allows it.
    blocked = plan(capability("anthropic"), _prompt(sensitive=True), route_allows_private=False)
    assert not blocked.emit and "private" in blocked.reason
    allowed = plan(capability("anthropic"), _prompt(sensitive=True), route_allows_private=True)
    assert allowed.emit  # provider permits + route permits ⇒ cacheable


def test_private_prefix_never_cached_on_non_private_provider() -> None:
    # openai is not private_ok ⇒ cache_private_allowed False ⇒ a private prefix is never cached,
    # even if the route "allows" it.
    d = plan(capability("openai"), _prompt(sensitive=True), route_allows_private=True)
    assert not d.emit and "private" in d.reason


def test_non_sensitive_prefix_caches_normally() -> None:
    assert plan(capability("anthropic"), _prompt(sensitive=False)).emit
    assert plan(capability("openai"), _prompt(sensitive=False)).emit


def test_emitters_return_none_when_not_emitting() -> None:
    off = plan(capability("zai"), _prompt())
    assert anthropic_cache_control(off) is None and openai_prompt_cache_key(off) is None
