"""OpenAI chat client — TEXT-ONLY (Phase 10 Task 6).

Implements the :class:`~jarvis.core.client.LLMClient` protocol for tool-less calls only:
analysis/synthesis/review/judge roles that produce text, never drive tools. A call that
passes ``tools`` raises :class:`UnsupportedToolUseError` (fail loud) rather than silently
dropping them — a write-capable executor must stay on Anthropic this phase.

Two correctness pins the pre-mortem (#12/#14) demands:
* **Usage fields are mapped explicitly** — OpenAI's ``prompt_tokens`` / ``completion_tokens``
  → our ``input_tokens`` / ``output_tokens``. Reusing ``Usage.from_response`` (Anthropic-shaped)
  would read zeros and silently cost $0.
* **Empty/short responses fail loud** — an absent choice or empty content raises, so a stage
  can't "succeed" with nothing.
"""

from __future__ import annotations

from collections.abc import Callable
from time import perf_counter

from jarvis.core.client import ModelResponse
from jarvis.observability.cost import Usage


class UnsupportedToolUseError(RuntimeError):
    """Raised when a tool-use call reaches the text-only OpenAI adapter."""


class OpenAIResponseError(RuntimeError):
    """Raised when an OpenAI response has no usable content (fail loud, never silent)."""


def _usage_from_openai(usage: object) -> Usage:
    """Map OpenAI usage → our Usage with EXPLICIT field names (never the Anthropic shape).
    Cached prompt tokens, when reported, are recorded as cache reads."""

    def _get(name: str) -> int:
        if usage is None:
            return 0
        val = getattr(usage, name, None)
        if val is None and isinstance(usage, dict):
            val = usage.get(name)
        return int(val or 0)

    cached = 0
    details = getattr(usage, "prompt_tokens_details", None)
    if details is not None:
        cached = int(getattr(details, "cached_tokens", 0) or 0)
    return Usage(
        input_tokens=_get("prompt_tokens"),
        output_tokens=_get("completion_tokens"),
        cache_read_input_tokens=cached,
    )


class OpenAIChatClient:
    """Text-only :class:`LLMClient` over OpenAI chat completions. Inject ``client`` in tests."""

    def __init__(self, *, api_key: str | None = None, client: object | None = None) -> None:
        if client is None:
            from openai import AsyncOpenAI

            client = AsyncOpenAI(api_key=api_key)
        self._client = client

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
    ) -> ModelResponse:
        if tools:
            raise UnsupportedToolUseError(
                f"OpenAIChatClient is text-only; model {model!r} cannot drive tools this phase"
            )
        # Anthropic keeps `system` separate; OpenAI takes it as the first message.
        oai_messages = [{"role": "system", "content": system}, *_to_openai_messages(messages)]
        kwargs: dict = {
            "model": model,
            "messages": oai_messages,
            "max_completion_tokens": max_tokens,
        }
        if temperature is not None:
            kwargs["temperature"] = temperature
        start = perf_counter()
        completion = await self._client.chat.completions.create(**kwargs)  # type: ignore[attr-defined]
        latency_ms = (perf_counter() - start) * 1000.0

        choices = getattr(completion, "choices", None) or []
        if not choices:
            raise OpenAIResponseError(f"OpenAI returned no choices for model {model!r}")
        content = getattr(choices[0].message, "content", None)
        if not content or not content.strip():
            raise OpenAIResponseError(f"OpenAI returned empty content for model {model!r}")
        if on_text_delta:
            on_text_delta(content)
        return ModelResponse(
            content_blocks=[{"type": "text", "text": content}],
            stop_reason="end_turn",
            usage=_usage_from_openai(getattr(completion, "usage", None)),
            model=getattr(completion, "model", None) or model,
            latency_ms=latency_ms,
        )


def _to_openai_messages(messages: list[dict]) -> list[dict]:
    """Flatten our (possibly block-shaped) messages to OpenAI's plain {role, content} text.
    Tool-use/tool-result blocks are rendered as short text notes — this adapter is text-only,
    so a caller shouldn't be sending them, but we degrade rather than crash."""
    out: list[dict] = []
    for m in messages:
        role = m.get("role", "user")
        content = m.get("content")
        if isinstance(content, str):
            out.append({"role": role, "content": content})
            continue
        parts: list[str] = []
        for block in content if isinstance(content, list) else []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                parts.append(str(block.get("text", "")))
            elif block.get("type") == "tool_use":
                parts.append(f"[requested tool {block.get('name')}]")
            elif block.get("type") == "tool_result":
                parts.append(f"[tool result: {block.get('content')}]")
        out.append({"role": role, "content": "\n".join(p for p in parts if p)})
    return out
