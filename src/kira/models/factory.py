"""ClientFactory (Phase 10 Task 6; Phase 10C providers): a :class:`ModelRoute` → a cached
``LLMClient``.

Fails CLOSED: resolving a route whose provider key is unset raises ``ConfigError`` naming the
env var — never a silent downgrade to a different provider/model. The client adapter is chosen
by the provider's ``api_style`` (the catalog is the source of truth):

* ``anthropic`` — the native streaming client (effort + adaptive thinking).
* ``anthropic_compat`` (DeepSeek/Qwen/GLM) — the SAME client + ``base_url`` + the capability
  degradation profile (``compat=True``: no effort/thinking) + the provider's ``auth_style``.
* ``openai_compat`` (OpenAI, Gemini) — the text-only OpenAI chat client + ``base_url``.

Clients are cached: Anthropic-style by ``(provider, effort, thinking, compat)`` (the model is a
per-call arg; a text-only route ⇒ thinking off), OpenAI-style by ``(provider,)``. This is the one
place the cost-ledger wrap is applied, so every provider is tapped uniformly.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from kira.config import _REQUIRED_KEYS, ConfigError
from kira.core.anthropic_client import AnthropicClient
from kira.models.openai_client import OpenAIChatClient
from kira.models.providers import provider_spec
from kira.models.roles import ModelRoute

if TYPE_CHECKING:
    from kira.config import Config
    from kira.core.client import LLMClient
    from kira.models.providers import ProviderSpec


class ClientFactory:
    """Builds and caches ``LLMClient``s per route. One factory per process."""

    def __init__(self, config: Config, *, ledger: object | None = None) -> None:
        self.config = config
        self.ledger = ledger
        self._cache: dict[tuple, LLMClient] = {}

    def for_route(self, route: ModelRoute) -> LLMClient:
        """The client for ``route`` (cached). Raises ``ConfigError`` if the provider is unknown
        or its key is unset (fail-closed — the caller must not fall back to another provider)."""
        spec = provider_spec(route.provider)
        if spec is None:
            raise ConfigError(f"unknown model provider: {route.provider!r}")
        self.config.require(spec.key_service)  # fail-closed; names the exact env var
        key = self._key_value(spec)
        base_url = self._base_url(spec)

        if spec.api_style in ("anthropic", "anthropic_compat"):
            compat = spec.api_style == "anthropic_compat"
            thinking = not route.text_only and not compat  # compat ⇒ no thinking (degradation)
            cache_key = (route.provider, route.effort, thinking, compat)
            if cache_key not in self._cache:
                client: LLMClient = AnthropicClient(
                    api_key=key,
                    effort=route.effort,
                    max_retries=self.config.limits.max_retries,
                    thinking=thinking,
                    base_url=base_url,
                    compat=compat,
                    auth_style=spec.auth_style,
                    context_reuse=self.config.context_reuse.enabled,
                )
                if self.ledger is not None:
                    client = self._ledgered(
                        client,
                        provider=route.provider,
                        effort=route.effort,
                    )
                self._cache[cache_key] = client
            return self._cache[cache_key]

        # openai_compat — text-only (OpenAI + Gemini)
        cache_key = (route.provider,)
        if cache_key not in self._cache:
            client = OpenAIChatClient(
                api_key=key,
                base_url=base_url,
                provider=route.provider,
                context_reuse=self.config.context_reuse.enabled,
                max_retries=self.config.limits.max_retries,
            )
            if self.ledger is not None:
                client = self._ledgered(
                    client,
                    provider=route.provider,
                    effort=route.effort,
                )
            self._cache[cache_key] = client
        return self._cache[cache_key]

    def _key_value(self, spec: ProviderSpec) -> str:
        """The provider's key value from Secrets (looked up via the same map require() uses —
        one source of truth for the (attr, env var) pair)."""
        attr = _REQUIRED_KEYS[spec.key_service][0]
        return getattr(self.config.secrets, attr)

    def _base_url(self, spec: ProviderSpec) -> str | None:
        """Config override (``providers.base_urls``) beats the catalog default; None ⇒ SDK
        default (native Anthropic / OpenAI)."""
        override = (self.config.providers.base_urls or {}).get(spec.name)
        return override or spec.default_base_url

    def _ledgered(self, client: LLMClient, *, provider: str, effort: str) -> LLMClient:
        """Apply the optional accounting wrapper lazily to avoid the models↔ledger import cycle."""
        from kira.observability.ledger import LedgeredClient

        return LedgeredClient(
            client,
            ledger=self.ledger,  # type: ignore[arg-type]
            provider=provider,
            effort=effort,
        )
