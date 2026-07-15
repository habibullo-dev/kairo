"""The real model client: Anthropic Messages API over streaming.

Implements the same :class:`LLMClient` interface the loop was built and tested
against, so nothing in the loop changes going live. Design choices, all
quality-first (API cost is not a constraint):

* **Streaming** — required for the large ``max_tokens`` we allow; also gives the
  REPL live text and avoids HTTP timeouts.
* **Adaptive thinking + effort** — the reasoning tier (Opus/Sonnet/Fable) uses
  ``thinking={"type":"adaptive"}`` (the only on-mode; ``budget_tokens`` is rejected)
  with ``output_config.effort``. The Haiku tier rejects BOTH (400), so both are gated
  off by model there; ``effort`` is per-call overridable on the reasoning tier (the
  UI's per-model effort selector — Haiku simply has no effort knob).
* **SDK retries** — the client retries 429/5xx with exponential backoff; we just
  raise ``max_retries``. No hand-rolled retry loop.

Content blocks are serialized back to dicts preserving **all** block types —
notably ``thinking`` blocks (with their ``signature``), which must round-trip
unchanged on the same model or the next turn is rejected.
"""

from __future__ import annotations

from collections.abc import Callable
from time import perf_counter
from typing import TYPE_CHECKING

from kira.core.client import ModelResponse
from kira.models.context_reuse import anthropic_cache_control, plan_for_prefix
from kira.observability.cost import Usage

if TYPE_CHECKING:
    from kira.config import Config


def _serialize_block(block: object) -> dict:
    """Convert one SDK content block to an API-ready dict.

    Uses documented block attributes rather than a blind ``model_dump`` so we
    don't ship null/extra fields the API might reject. Unknown block types fall
    back to ``model_dump`` so a new block type degrades rather than crashing.
    """
    t = getattr(block, "type", None)
    if t == "text":
        return {"type": "text", "text": block.text}  # type: ignore[attr-defined]
    if t == "thinking":
        return {
            "type": "thinking",
            "thinking": block.thinking,  # type: ignore[attr-defined]
            "signature": block.signature,  # type: ignore[attr-defined]
        }
    if t == "redacted_thinking":
        return {"type": "redacted_thinking", "data": block.data}  # type: ignore[attr-defined]
    if t == "tool_use":
        return {
            "type": "tool_use",
            "id": block.id,  # type: ignore[attr-defined]
            "name": block.name,  # type: ignore[attr-defined]
            "input": block.input or {},  # type: ignore[attr-defined]
        }
    dump = getattr(block, "model_dump", None)
    return dump() if callable(dump) else {"type": str(t)}


def to_model_response(message: object, fallback_model: str) -> ModelResponse:
    """Convert an SDK ``Message`` into the loop's :class:`ModelResponse`."""
    blocks = [_serialize_block(b) for b in message.content]  # type: ignore[attr-defined]
    return ModelResponse(
        content_blocks=blocks,
        stop_reason=getattr(message, "stop_reason", None) or "end_turn",
        usage=Usage.from_response(getattr(message, "usage", {})),
        model=getattr(message, "model", None) or fallback_model,
    )


class CompatResponseError(RuntimeError):
    """A compat provider (DeepSeek/Qwen/GLM via the Anthropic-Messages endpoint) returned an
    unusable response — no content, or zero token usage. Fail loud rather than silently succeed
    with nothing or cost $0 on real tokens (Phase 10C: unmetered spend is untracked spend)."""


def _guard_compat_response(response: ModelResponse, model: str) -> None:
    """Fidelity guard for anthropic_compat endpoints (never applied to native Anthropic, which
    always reports usage). A native-shaped response has content AND usage; if either is missing,
    the compat endpoint is misbehaving — raise rather than proceed on bad data."""
    if not response.content_blocks or (not response.text.strip() and not response.tool_calls):
        raise CompatResponseError(f"compat provider returned empty content for model {model!r}")
    if response.usage.total_tokens == 0:
        raise CompatResponseError(f"compat provider reported zero token usage for model {model!r}")


def _is_haiku_tier(model: str) -> bool:
    """The Anthropic Haiku tier — the fast/economy models. They reject BOTH the extended-reasoning
    knobs the reasoning tier (Opus/Sonnet/Fable) accepts: ``thinking`` (400 "adaptive thinking is
    not supported on this model") AND ``output_config`` (400 "this model does not support the
    effort parameter"). So both are gated off for Haiku; it stays fully usable, just without
    extended reasoning or an effort knob."""
    return "haiku" in model.lower()


def _supports_adaptive_thinking(model: str) -> bool:
    """Whether ``model`` accepts ``thinking={"type":"adaptive"}`` (reasoning tier only)."""
    return not _is_haiku_tier(model)


def _supports_effort(model: str) -> bool:
    """Whether ``model`` accepts ``output_config.effort`` (reasoning tier only — Haiku 400s)."""
    return not _is_haiku_tier(model)


class AnthropicClient:
    """Live :class:`LLMClient`. Inject ``client`` in tests; otherwise built from a key.

    Also serves the Phase 10C ``anthropic_compat`` providers (DeepSeek/Qwen/GLM) via ``base_url``
    + ``compat=True``: the Anthropic-Messages wire format is reused, but the capability
    degradation profile sends ONLY the conservative core — no ``output_config``/effort, no
    ``thinking`` (compat endpoints reject or ignore them). ``auth_style`` selects the auth header:
    ``bearer`` (Z.ai → ``auth_token`` → Authorization: Bearer) vs ``x-api-key`` (the default)."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        client: object | None = None,
        effort: str = "high",
        max_retries: int = 4,
        thinking: bool = True,
        base_url: str | None = None,
        compat: bool = False,
        auth_style: str = "x-api-key",
        context_reuse: bool = False,
    ) -> None:
        if client is None:
            from anthropic import AsyncAnthropic

            kwargs: dict = {"max_retries": max_retries}
            if base_url:
                kwargs["base_url"] = base_url
            # Auth header varies by compat provider; native Anthropic uses api_key (x-api-key).
            kwargs["auth_token" if auth_style == "bearer" else "api_key"] = api_key
            client = AsyncAnthropic(**kwargs)
        self._client = client
        self.effort = effort
        self.thinking = thinking
        self.compat = compat
        # S7 enable-step (Phase 13): attach a cache_control breakpoint to the stable system
        # prefix when on. NATIVE Anthropic only — compat providers (DeepSeek/Qwen/GLM/Z.ai) get
        # NO control this phase (their capability is deferred/off), so the guard is `not compat`.
        self.context_reuse = context_reuse

    @classmethod
    def from_config(cls, config: Config) -> AnthropicClient:
        config.require("anthropic")
        return cls(
            api_key=config.secrets.anthropic_api_key,
            effort=config.limits.effort,
            max_retries=config.limits.max_retries,
            context_reuse=config.context_reuse.enabled,
        )

    def _cache_system(
        self, system: str, stable_prefix: str | None
    ) -> tuple[str | list[dict], str | None]:
        """S7 enable-step: when context reuse is on (NATIVE Anthropic only), split ``system``
        into a cached stable block + an uncached volatile block at the stable/volatile seam,
        marking the stable block with a ``cache_control`` breakpoint. Returns the payload to send
        as ``system`` — a plain string, UNCHANGED, whenever nothing is cached (flag off / compat /
        no prefix / policy declines), so the request stays byte-identical to a no-caching build —
        and the stable-prefix hash to record (None when no control was emitted).

        Only a genuine prefix is ever cached, and only the caller's stable prefix (the volatile,
        possibly-private tail is excluded by construction), so the cached block is
        stable + non-sensitive by default — the private-content gate (in :func:`plan_for_prefix`)
        is the belt-and-suspenders."""
        if not (self.context_reuse and not self.compat and stable_prefix):
            return system, None
        if not system.startswith(stable_prefix):
            return system, None  # defensive: cache a real prefix or nothing
        directive, assembled = plan_for_prefix("anthropic", stable_prefix)
        control = anthropic_cache_control(directive)
        if control is None:
            return system, None
        blocks: list[dict] = [{"type": "text", "text": stable_prefix, "cache_control": control}]
        remainder = system[len(stable_prefix) :]
        if remainder.strip():  # the uncached volatile tail (never marked for caching)
            blocks.append({"type": "text", "text": remainder})
        return blocks, assembled.stable_prefix_hash

    def _build_kwargs(
        self,
        model: str,
        system: str | list[dict],
        messages: list[dict],
        tools: list[dict],
        max_tokens: int,
        tool_choice: dict | None = None,
        temperature: float | None = None,
        effort: str | None = None,
    ) -> dict:
        kwargs: dict = {
            "model": model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": messages,
        }
        if not self.compat and _supports_effort(model):
            # effort/output_config is Anthropic-native; compat endpoints reject/ignore it, and the
            # Haiku tier 400s on it entirely (so it's gated off there). A per-call `effort` (the
            # UI's per-model effort selector) overrides the client default; None ⇒ the configured
            # default ⇒ byte-identical to a build without the selector.
            kwargs["output_config"] = {"effort": effort or self.effort}
        if tools:
            kwargs["tools"] = tools
        if tool_choice is not None:
            # Forcing a tool is incompatible with extended thinking; callers that
            # force a tool use a thinking-off client (utility). Belt and suspenders:
            kwargs["tool_choice"] = tool_choice
        if (
            self.thinking
            and tool_choice is None
            and not self.compat
            and _supports_adaptive_thinking(model)
        ):
            # Adaptive thinking is Anthropic-native — never sent to a compat endpoint, and never
            # to a model that rejects it (the Haiku tier 400s on `thinking`). output_config.effort
            # IS accepted there, so only the thinking param is gated by model capability.
            kwargs["thinking"] = {"type": "adaptive"}
        if temperature is not None:
            # Explicit temperature (the judge sets 1.0); default None leaves the
            # API default untouched. Only used on thinking-off calls (a forced-tool
            # judge), so it never conflicts with adaptive thinking.
            kwargs["temperature"] = temperature
        return kwargs

    async def create(
        self,
        *,
        model: str,
        system: str,
        messages: list[dict],
        tools: list[dict],
        max_tokens: int,
        on_text_delta: Callable[[str], None] | None = None,
        tool_choice: dict | None = None,
        temperature: float | None = None,
        stable_prefix: str | None = None,
        effort: str | None = None,
    ) -> ModelResponse:
        system_payload, cr_hash = self._cache_system(system, stable_prefix)
        kwargs = self._build_kwargs(
            model, system_payload, messages, tools, max_tokens, tool_choice, temperature, effort
        )
        start = perf_counter()
        async with self._client.messages.stream(**kwargs) as stream:  # type: ignore[attr-defined]
            async for text in stream.text_stream:
                if on_text_delta:
                    on_text_delta(text)
            message = await stream.get_final_message()
        response = to_model_response(message, fallback_model=model)
        response.latency_ms = (perf_counter() - start) * 1000.0
        response.stable_prefix_hash = cr_hash  # None unless a cache control was emitted
        if self.compat:
            _guard_compat_response(response, model)  # fail loud on empty content / zero usage
        return response
