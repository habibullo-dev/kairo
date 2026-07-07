"""ClientFactory (Phase 10 Task 6): a :class:`ModelRoute` → a cached ``LLMClient``.

Fails CLOSED: resolving a route whose provider key is unset raises ``ConfigError`` naming the
env var — never a silent downgrade to a different provider/model. Anthropic clients are cached
by ``(effort, thinking)`` (the model is a per-call arg, and a text-only route ⇒ thinking off,
matching the utility-client precedent); the single OpenAI client is cached once. This is the
one place the Phase 10 cost ledger wrap (Task 7) is applied, so every provider is tapped
uniformly — until then it returns the bare clients.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from jarvis.core.anthropic_client import AnthropicClient
from jarvis.models.openai_client import OpenAIChatClient
from jarvis.models.roles import ModelRoute

if TYPE_CHECKING:
    from jarvis.config import Config
    from jarvis.core.client import LLMClient


class ClientFactory:
    """Builds and caches ``LLMClient``s per route. One factory per process."""

    def __init__(self, config: Config) -> None:
        self.config = config
        self._cache: dict[tuple, LLMClient] = {}

    def for_route(self, route: ModelRoute) -> LLMClient:
        """The client for ``route`` (cached). Raises ``ConfigError`` if the provider's key is
        unset (fail-closed — the caller must not fall back to another provider)."""
        if route.provider == "anthropic":
            self.config.require("anthropic")
            thinking = not route.text_only  # text-only analysis roles run thinking-off
            key = ("anthropic", route.effort, thinking)
            if key not in self._cache:
                self._cache[key] = self._build_anthropic(route.effort, thinking)
            return self._cache[key]
        if route.provider == "openai":
            self.config.require("openai")
            key = ("openai",)
            if key not in self._cache:
                self._cache[key] = OpenAIChatClient(api_key=self.config.secrets.openai_api_key)
            return self._cache[key]
        # Unreachable if the route was validated, but fail closed regardless.
        from jarvis.config import ConfigError

        raise ConfigError(f"unknown model provider: {route.provider!r}")

    def _build_anthropic(self, effort: str, thinking: bool) -> LLMClient:
        return AnthropicClient(
            api_key=self.config.secrets.anthropic_api_key,
            effort=effort,
            max_retries=self.config.limits.max_retries,
            thinking=thinking,
        )
