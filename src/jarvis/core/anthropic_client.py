"""The real model client: Anthropic Messages API over streaming.

Implements the same :class:`LLMClient` interface the loop was built and tested
against, so nothing in the loop changes going live. Design choices, all
quality-first (API cost is not a constraint):

* **Streaming** — required for the large ``max_tokens`` we allow; also gives the
  REPL live text and avoids HTTP timeouts.
* **Adaptive thinking + effort** — Opus 4.8 uses ``thinking={"type":"adaptive"}``
  (the only on-mode; ``budget_tokens`` is rejected) with ``output_config.effort``.
* **SDK retries** — the client retries 429/5xx with exponential backoff; we just
  raise ``max_retries``. No hand-rolled retry loop.

Content blocks are serialized back to dicts preserving **all** block types —
notably ``thinking`` blocks (with their ``signature``), which must round-trip
unchanged on the same model or the next turn is rejected.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from jarvis.core.client import ModelResponse
from jarvis.observability.cost import Usage

if TYPE_CHECKING:
    from jarvis.config import Config


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


class AnthropicClient:
    """Live :class:`LLMClient`. Inject ``client`` in tests; otherwise built from a key."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        client: object | None = None,
        effort: str = "high",
        max_retries: int = 4,
        thinking: bool = True,
    ) -> None:
        if client is None:
            from anthropic import AsyncAnthropic

            client = AsyncAnthropic(api_key=api_key, max_retries=max_retries)
        self._client = client
        self.effort = effort
        self.thinking = thinking

    @classmethod
    def from_config(cls, config: Config) -> AnthropicClient:
        config.require("anthropic")
        return cls(
            api_key=config.secrets.anthropic_api_key,
            effort=config.limits.effort,
            max_retries=config.limits.max_retries,
        )

    def _build_kwargs(
        self,
        model: str,
        system: str,
        messages: list[dict],
        tools: list[dict],
        max_tokens: int,
        tool_choice: dict | None = None,
    ) -> dict:
        kwargs: dict = {
            "model": model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": messages,
            "output_config": {"effort": self.effort},
        }
        if tools:
            kwargs["tools"] = tools
        if tool_choice is not None:
            # Forcing a tool is incompatible with extended thinking; callers that
            # force a tool use a thinking-off client (utility). Belt and suspenders:
            kwargs["tool_choice"] = tool_choice
        if self.thinking and tool_choice is None:
            kwargs["thinking"] = {"type": "adaptive"}
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
    ) -> ModelResponse:
        kwargs = self._build_kwargs(model, system, messages, tools, max_tokens, tool_choice)
        async with self._client.messages.stream(**kwargs) as stream:  # type: ignore[attr-defined]
            async for text in stream.text_stream:
                if on_text_delta:
                    on_text_delta(text)
            message = await stream.get_final_message()
        return to_model_response(message, fallback_model=model)
