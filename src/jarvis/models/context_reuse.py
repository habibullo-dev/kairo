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
