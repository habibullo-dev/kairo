"""The model-client boundary.

``AgentLoop`` talks to *an* :class:`LLMClient`, never to the Anthropic SDK
directly. That interface is what lets the loop be tested end-to-end against a
scripted :class:`FakeClient` with no network (this task), and swapped for the real
streaming client in task 7 with zero loop changes.

A :class:`ModelResponse` carries the assistant's content blocks *verbatim* — they
are appended to the message history unchanged, because the API requires
``tool_use`` blocks to round-trip exactly as sent.
"""

from __future__ import annotations

import copy
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from kira.observability.cost import Usage


@dataclass(frozen=True)
class ToolCall:
    """A single ``tool_use`` request from the model."""

    id: str
    name: str
    input: dict


@dataclass
class ModelResponse:
    """One assistant turn: content blocks (verbatim), stop reason, token usage."""

    content_blocks: list[dict]
    stop_reason: str
    usage: Usage
    model: str = "claude-opus-4-8"
    # Wall-clock of the API call, populated by the live client; None = not measured
    # (default keeps the ~450 FakeClient-built responses byte-identical).
    latency_ms: float | None = None
    # S7 context reuse (Phase 13): the stable-prefix hash a live client cached under, when
    # caching was enabled AND a control was emitted; None otherwise (keeps FakeClient identical).
    # The ledger records it so the Cost Center can group cache benefit by prefix.
    stable_prefix_hash: str | None = None

    @property
    def text(self) -> str:
        return "".join(b.get("text", "") for b in self.content_blocks if b.get("type") == "text")

    @property
    def tool_calls(self) -> list[ToolCall]:
        return [
            ToolCall(id=b["id"], name=b["name"], input=b.get("input") or {})
            for b in self.content_blocks
            if b.get("type") == "tool_use"
        ]


@runtime_checkable
class LLMClient(Protocol):
    """What the loop needs from a model client. The real client (task 7) and the
    FakeClient below both satisfy this."""

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
    ) -> ModelResponse: ...


# --- convenience builders for scripting responses -------------------------


def _default_usage() -> Usage:
    return Usage(input_tokens=10, output_tokens=5)


def text_message(
    text: str, *, usage: Usage | None = None, model: str = "claude-opus-4-8"
) -> ModelResponse:
    """A final assistant answer (stop_reason ``end_turn``)."""
    return ModelResponse(
        content_blocks=[{"type": "text", "text": text}],
        stop_reason="end_turn",
        usage=usage or _default_usage(),
        model=model,
    )


def tool_use_message(
    calls: list[ToolCall],
    *,
    text: str = "",
    usage: Usage | None = None,
    model: str = "claude-opus-4-8",
) -> ModelResponse:
    """An assistant turn that requests one or more tools (stop_reason ``tool_use``)."""
    blocks: list[dict] = []
    if text:
        blocks.append({"type": "text", "text": text})
    for c in calls:
        blocks.append({"type": "tool_use", "id": c.id, "name": c.name, "input": c.input})
    return ModelResponse(
        content_blocks=blocks,
        stop_reason="tool_use",
        usage=usage or _default_usage(),
        model=model,
    )


@dataclass
class FakeClient:
    """A scripted client for tests. Returns queued responses in order and records
    each ``create`` call so tests can assert on the messages/tools the loop sent."""

    responses: list[ModelResponse]
    calls: list[dict] = field(default_factory=list)

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
        # S7 (Phase 13): FakeClient accepts `stable_prefix` for protocol conformance but NEVER
        # acts on it — it emits no cache control and does not record it, so the fake path (and
        # every recorded cassette) stays byte-identical whether or not caching is enabled.
        # `effort` IS recorded (None when the caller didn't override) so tests can assert the
        # loop's per-turn effort plumbing without a live key.
        self.calls.append(
            {
                "model": model,
                "system": system,
                "messages": copy.deepcopy(messages),
                "tools": tools,
                "max_tokens": max_tokens,
                "tool_choice": tool_choice,
                "temperature": temperature,
                "effort": effort,
            }
        )
        if not self.responses:
            raise AssertionError("FakeClient ran out of scripted responses")
        response = self.responses.pop(0)
        # A small non-None fake latency lets keyless tests exercise latency aggregation.
        if response.latency_ms is None:
            response.latency_ms = 1.0
        if on_text_delta and response.text:
            on_text_delta(response.text)
        return response
