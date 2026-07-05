"""The agent loop: the while-loop where the model calls tools until it's done.

This is the heart of the system; everything else is infrastructure around it. The
invariants below are each a classic agent bug when violated:

* **Tool errors are model feedback, not crashes** — captured by the executor and
  returned as ``is_error`` results the model reads and recovers from.
* **Denials are also results** — the model must learn "no", not silently retry.
* **Exactly one ``tool_result`` per ``tool_use`` id** — the API rejects the turn
  otherwise; ``gather`` over the calls preserves order and count.
* **Assistant blocks are appended verbatim** — ``tool_use`` blocks must round-trip
  unchanged.
* **Max-iteration guard** — a runaway tool loop is a matter of when, not if.

Permission is resolved sequentially (human prompts stay orderly) but approved
tools execute in parallel.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from jarvis.config import Config
from jarvis.core.client import LLMClient, ToolCall
from jarvis.core.context import ContextManager
from jarvis.core.events import Event, TextDelta, ToolFinished, ToolStarted, TurnCompleted
from jarvis.core.prompts import build_system
from jarvis.observability import bind_trace, get_logger
from jarvis.observability.cost import Usage, cost_of
from jarvis.permissions.gate import Decision, PermissionGate
from jarvis.tools.base import Permission
from jarvis.tools.executor import ToolExecutor
from jarvis.tools.registry import ToolRegistry

if TYPE_CHECKING:
    from jarvis.memory.service import MemoryService


def _latest_user_text(messages: list[dict]) -> str | None:
    """The most recent plain-text user message (what auto-recall queries on)."""
    for message in reversed(messages):
        if message.get("role") == "user" and isinstance(message.get("content"), str):
            return message["content"]
    return None


# Called when a tool needs human approval; returns the resolved permission.
Approver = Callable[[ToolCall, Decision], Awaitable[Permission]]
# Called for each event as the turn unfolds (rendering is the interface's job).
EventSink = Callable[[Event], None]


@dataclass
class TurnResult:
    """Outcome of one user turn."""

    text: str
    messages: list[dict]
    stop_reason: str
    usage: Usage
    iterations: int


class AgentLoop:
    def __init__(
        self,
        *,
        client: LLMClient,
        registry: ToolRegistry,
        executor: ToolExecutor,
        gate: PermissionGate,
        config: Config,
        approver: Approver | None = None,
        system: str | None = None,
        context_manager: ContextManager | None = None,
        memory: MemoryService | None = None,
    ) -> None:
        self.client = client
        self.registry = registry
        self.executor = executor
        self.gate = gate
        self.config = config
        self.approver = approver
        self.system = system if system is not None else build_system()
        # Optional Phase-2 collaborators. Both None => byte-identical Phase-1 behavior.
        self.context_manager = context_manager
        self.memory = memory
        self.log = get_logger("jarvis.agent")

    def _system_with_extras(self, recall_block: str | None, summary: str | None) -> str:
        """Base system prompt plus dynamic extras, ordered stable → volatile
        (identity/guidance → compaction summary → recalled memories)."""
        extras = [x for x in (summary, recall_block) if x]
        if not extras:
            return self.system
        return self.system + "\n\n" + "\n\n".join(extras)

    async def _recall_block(self, messages: list[dict]) -> str | None:
        if self.memory is None:
            return None
        text = _latest_user_text(messages)
        return await self.memory.auto_recall_context(text) if text else None

    async def run_turn(
        self,
        messages: list[dict],
        *,
        on_event: EventSink | None = None,
    ) -> TurnResult:
        """Run one turn to completion. ``messages`` must already include the new user
        turn; the returned ``messages`` has the assistant + tool-result turns appended.
        The caller's list is not mutated."""
        emit: EventSink = on_event or (lambda _e: None)
        trace_id = bind_trace()
        messages = list(messages)
        total = Usage()
        limits = self.config.limits

        self.log.info("turn_start", trace_id=trace_id, model=self.config.models.main)

        # Auto-recall runs once per turn, on the new user message (not per iteration).
        recall_block = await self._recall_block(messages)
        # Freeze the compaction cut + summary for the whole turn: the summary stays
        # stable across iterations, and within-turn growth is absorbed by elision.
        frozen_cut, summary = 0, None
        if self.context_manager is not None:
            frozen_cut, summary = await self.context_manager.summary_for(messages)

        for iteration in range(limits.max_iterations):
            # Compact the *view* sent to the API; `messages` (full history) is untouched.
            view = (
                self.context_manager.view(messages, cut=frozen_cut)
                if self.context_manager
                else None
            )
            if view is not None and view.overflow:
                emit(TurnCompleted(text="", stop_reason="max_context"))
                self.log.warning("turn_end", stop_reason="max_context", iterations=iteration)
                return TurnResult("", messages, "max_context", total, iteration)
            api_messages = view.messages if view is not None else messages
            if view is not None and (view.cut or view.elided):
                self.log.info(
                    "context_compacted",
                    cut=view.cut,
                    elided=view.elided,
                    sent_messages=len(api_messages),
                    full_messages=len(messages),
                )

            response = await self.client.create(
                model=self.config.models.main,
                system=self._system_with_extras(recall_block, summary=summary),
                messages=api_messages,
                tools=self.registry.specs(),
                max_tokens=limits.max_output_tokens,
                on_text_delta=lambda t: emit(TextDelta(t)),
            )
            total = total + response.usage
            if self.context_manager is not None:
                self.context_manager.observe(response.usage)
            self.log.info(
                "model_call",
                iteration=iteration,
                model=response.model,
                stop_reason=response.stop_reason,
                input_tokens=response.usage.input_tokens,
                output_tokens=response.usage.output_tokens,
                cost_usd=round(cost_of(response.model, response.usage), 6),
            )
            messages.append({"role": "assistant", "content": response.content_blocks})

            tool_calls = response.tool_calls
            if response.stop_reason != "tool_use" or not tool_calls:
                emit(TurnCompleted(text=response.text, stop_reason=response.stop_reason))
                self.log.info(
                    "turn_end", stop_reason=response.stop_reason, iterations=iteration + 1
                )
                return TurnResult(
                    text=response.text,
                    messages=messages,
                    stop_reason=response.stop_reason,
                    usage=total,
                    iterations=iteration + 1,
                )

            results = await self._handle_tools(tool_calls, emit)
            messages.append({"role": "user", "content": results})

        emit(TurnCompleted(text="", stop_reason="max_iterations"))
        self.log.warning("turn_end", stop_reason="max_iterations", iterations=limits.max_iterations)
        return TurnResult(
            text="",
            messages=messages,
            stop_reason="max_iterations",
            usage=total,
            iterations=limits.max_iterations,
        )

    async def _handle_tools(self, tool_calls: list[ToolCall], emit: EventSink) -> list[dict]:
        # Phase 1 — resolve permission for each call sequentially (orderly prompts).
        resolved: list[tuple[ToolCall, object, Permission]] = []
        for call in tool_calls:
            tool = self.registry.get(call.name)
            if tool is None:
                self.log.warning("permission_decision", tool=call.name, reason="unknown_tool")
                resolved.append((call, None, Permission.DENY))
                continue
            decision = self.gate.check(call.name, call.input, tool_default=tool.permission_default)
            self.log.info(
                "permission_decision",
                tool=call.name,
                permission=str(decision.permission),
                reason=decision.reason,
            )
            perm = decision.permission
            if perm is Permission.ASK:
                perm = await self.approver(call, decision) if self.approver else Permission.DENY
                self.log.info("permission_resolved", tool=call.name, permission=str(perm))
            resolved.append((call, tool, perm))

        # Phase 2 — run approved tools in parallel; denied/unknown become error results.
        return await asyncio.gather(*(self._run_one(c, t, p, emit) for c, t, p in resolved))

    async def _run_one(
        self, call: ToolCall, tool: object, perm: Permission, emit: EventSink
    ) -> dict:
        if tool is None:
            emit(ToolFinished(call.id, call.name, is_error=True, preview="unknown tool"))
            return _tool_result(call.id, f"Unknown tool: {call.name}", is_error=True)
        if perm is not Permission.ALLOW:
            emit(ToolFinished(call.id, call.name, is_error=True, preview="denied"))
            self.log.info("tool_denied", tool=call.name)
            return _tool_result(
                call.id,
                "Denied by user or policy. Do not retry this call; "
                "explain or try another approach.",
                is_error=True,
            )

        emit(ToolStarted(call.id, call.name, call.input))
        self.log.info("tool_call", tool=call.name, input=call.input)
        result = await self.executor.execute(tool, call.input)  # type: ignore[arg-type]
        emit(
            ToolFinished(call.id, call.name, is_error=result.is_error, preview=result.content[:200])
        )
        self.log.info(
            "tool_result", tool=call.name, is_error=result.is_error, chars=len(result.content)
        )
        return _tool_result(call.id, result.content, is_error=result.is_error)


def _tool_result(tool_use_id: str, content: str, *, is_error: bool = False) -> dict:
    """Build a ``tool_result`` content block — exactly one per ``tool_use`` id."""
    return {
        "type": "tool_result",
        "tool_use_id": tool_use_id,
        "content": content,
        "is_error": is_error,
    }
