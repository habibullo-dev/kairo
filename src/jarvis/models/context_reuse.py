"""Provider-agnostic context-reuse capability + policy (S7).

Capability is DATA (on :class:`~jarvis.models.providers.ProviderSpec`); behavior is DERIVED here,
never hand-set per call site — the same discipline as the provider/service catalogs.
:func:`capability` answers: what can this provider reuse, and how? Fail-closed — an unknown
provider, a provider whose ``supports_context_reuse`` is False, or an unrecognized mode all
resolve to the OFF capability (cache nothing; still benefit from stable ordering). The directive
that turns a capability into a concrete cache control (and the private-content gate) is the
policy layer built on top of this.

Nothing here caches, stores, or sends anything — it only classifies. Cache is NOT memory.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum

from jarvis.models.prompt_layout import AssembledPrompt
from jarvis.models.providers import provider_spec


class ContextReuseMode(StrEnum):
    """How a provider reuses a repeated prompt prefix."""

    OFF = "off"  # emit no cache control (still order stable-first)
    AUTOMATIC_PREFIX = "automatic_prefix"  # provider caches repeated prefixes (OpenAI/DeepSeek)
    EXPLICIT_BREAKPOINT = "explicit_breakpoint"  # we mark a breakpoint (Anthropic/Qwen)
    EXPLICIT_RESOURCE = "explicit_resource"  # we create a provider-side cached resource (Gemini)
    PROVIDER_DEFAULT = "provider_default"  # defer to the provider default (Gemini implicit)


_MODES = frozenset(m.value for m in ContextReuseMode)


@dataclass(frozen=True)
class CacheCapability:
    """A provider's resolved context-reuse capability (fail-closed to OFF)."""

    provider: str
    supported: bool
    mode: ContextReuseMode
    supports_cache_key: bool
    supports_cache_ttl: bool
    reports_cached_tokens: bool
    cache_min_tokens: int
    cache_ttl_options: tuple[str, ...]
    cache_private_allowed: bool


#: The fail-closed capability: cache nothing. Returned for unknown / unsupported / bad-mode.
_OFF = CacheCapability(
    provider="",
    supported=False,
    mode=ContextReuseMode.OFF,
    supports_cache_key=False,
    supports_cache_ttl=False,
    reports_cached_tokens=False,
    cache_min_tokens=0,
    cache_ttl_options=(),
    cache_private_allowed=False,
)


def capability(provider_name: str) -> CacheCapability:
    """Resolve a provider's context-reuse capability from its catalog spec, fail-closed."""
    spec = provider_spec(provider_name)
    if spec is None or not spec.supports_context_reuse:
        return replace(_OFF, provider=provider_name)
    if spec.context_reuse_mode not in _MODES:
        return replace(_OFF, provider=provider_name)  # unrecognized mode ⇒ OFF (fail closed)
    return CacheCapability(
        provider=provider_name,
        supported=True,
        mode=ContextReuseMode(spec.context_reuse_mode),
        supports_cache_key=spec.supports_cache_key,
        supports_cache_ttl=spec.supports_cache_ttl,
        reports_cached_tokens=spec.reports_cached_tokens,
        cache_min_tokens=spec.cache_min_tokens,
        cache_ttl_options=tuple(spec.cache_ttl_options),
        cache_private_allowed=spec.cache_private_allowed,
    )


# --- policy: capability + assembled prompt -> a concrete cache directive ---------------------


@dataclass(frozen=True)
class CacheDirective:
    """What the client should emit for one request. ``emit`` gates everything; ``cache_key`` is
    the OpenAI ``prompt_cache_key`` (automatic_prefix); ``breakpoint`` marks that a cache_control
    should be set at the stable/volatile seam (explicit_breakpoint); ``ttl`` is the chosen TTL."""

    mode: ContextReuseMode
    emit: bool
    cache_key: str | None
    breakpoint: bool
    ttl: str | None
    reason: str


def plan(
    cap: CacheCapability,
    assembled: AssembledPrompt,
    *,
    route_allows_private: bool = False,
    ttl: str | None = None,
) -> CacheDirective:
    """Decide the cache directive for one request. Fail-closed + the PRIVATE-CONTENT GATE:

    * OFF / unsupported ⇒ emit nothing.
    * A SENSITIVE stable prefix is cached ONLY if the provider permits private caching
      (``cache_private_allowed``, i.e. private_ok) AND the route explicitly allows it. Otherwise
      no cache — the default is stable, NON-sensitive prefix only.
    * provider_default (implicit) and explicit_resource (deferred, privacy review) emit no control.
    * automatic_prefix ⇒ a prompt_cache_key (= the stable-prefix hash) where the provider honors
      one; explicit_breakpoint ⇒ a breakpoint at the seam (+ a supported TTL if requested).
    """
    off = CacheDirective(
        cap.mode, emit=False, cache_key=None, breakpoint=False, ttl=None, reason=""
    )
    if not cap.supported or cap.mode is ContextReuseMode.OFF:
        return replace(off, reason="provider caches nothing (off / unsupported)")
    if assembled.stable_is_sensitive and not (cap.cache_private_allowed and route_allows_private):
        return replace(off, reason="stable prefix is private; caching not permitted")
    if cap.mode is ContextReuseMode.PROVIDER_DEFAULT:
        return replace(off, reason="defer to the provider's implicit caching (no control emitted)")
    if cap.mode is ContextReuseMode.EXPLICIT_RESOURCE:
        return replace(off, reason="explicit_resource deferred (privacy review pending)")
    if cap.mode is ContextReuseMode.AUTOMATIC_PREFIX:
        key = assembled.stable_prefix_hash if cap.supports_cache_key else None
        return CacheDirective(
            cap.mode, emit=True, cache_key=key, breakpoint=False, ttl=None,
            reason="automatic prefix caching" + (" + prompt_cache_key" if key else ""),
        )
    # EXPLICIT_BREAKPOINT
    chosen = ttl if (cap.supports_cache_ttl and ttl in cap.cache_ttl_options) else None
    return CacheDirective(
        cap.mode, emit=True, cache_key=None, breakpoint=True, ttl=chosen,
        reason="explicit cache breakpoint at the stable/volatile seam",
    )


# --- provider-specific emitters (what the enable-step attaches to the live request) ----------
# These are the exact controls the client layer emits when caching is turned on. The live wiring
# (passing the system prefix as a cached block / setting the SDK cache key) rides on top of these
# in the enable-step; the substrate proves the controls are correct, keyless.


def anthropic_cache_control(directive: CacheDirective) -> dict | None:
    """The ``cache_control`` object to attach to the last stable block (Anthropic / Qwen), or
    None when nothing should be emitted."""
    if not (directive.emit and directive.breakpoint):
        return None
    control: dict = {"type": "ephemeral"}
    if directive.ttl:
        control["ttl"] = directive.ttl
    return control


def openai_prompt_cache_key(directive: CacheDirective) -> str | None:
    """The ``prompt_cache_key`` for the OpenAI request (routes to a warm prefix cache), or None."""
    return directive.cache_key if directive.emit else None
