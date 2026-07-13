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
import datetime as _dt
import json
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from dataclasses import replace as _dc_replace
from typing import TYPE_CHECKING

from jarvis.config import ChatConfig, Config
from jarvis.core.client import LLMClient, ToolCall
from jarvis.core.context import ContextManager
from jarvis.core.events import (
    Event,
    TextDelta,
    ToolDecision,
    ToolFinished,
    ToolStarted,
    TurnCompleted,
)
from jarvis.core.execution import current_execution_context
from jarvis.core.prompts import build_system
from jarvis.observability import bind_trace, get_logger
from jarvis.observability.cost import PricingTable, Usage, cost_of
from jarvis.observability.ledger import cost_context
from jarvis.permissions.gate import Decision, PermissionGate
from jarvis.permissions.modes import Mode, auto_approves, plan_blocks
from jarvis.tools.base import Permission
from jarvis.tools.executor import ToolExecutor
from jarvis.tools.registry import ToolRegistry

if TYPE_CHECKING:
    from jarvis.memory.service import MemoryService
    from jarvis.projects.context import ProjectContext


def _latest_user_text(messages: list[dict]) -> str | None:
    """The most recent plain-text user message (what auto-recall queries on)."""
    for message in reversed(messages):
        if message.get("role") == "user" and isinstance(message.get("content"), str):
            return message["content"]
    return None


def _cancellation_snapshot(messages: list[dict]) -> list[dict]:
    """Return history that is safe to persist after an interrupted turn.

    A cancellation can land after the assistant has emitted ``tool_use`` blocks but before the
    matching ``tool_result`` user message is appended.  That half-batch is invalid input for the
    next provider request, so retain only any ordinary text from that final assistant response.
    Earlier complete assistant/tool-result pairs remain untouched.
    """
    snapshot = list(messages)
    if not snapshot:
        return snapshot
    final = snapshot[-1]
    content = final.get("content") if final.get("role") == "assistant" else None
    if not isinstance(content, list) or not any(
        isinstance(block, dict) and block.get("type") == "tool_use" for block in content
    ):
        return snapshot
    text_blocks = [
        block for block in content if isinstance(block, dict) and block.get("type") == "text"
    ]
    if text_blocks:
        snapshot[-1] = {**final, "content": text_blocks}
    else:
        snapshot.pop()
    return snapshot


#: Eval determinism hook: when this env var holds an ISO timestamp, EVERY agent loop (main,
#: sub-agent, unattended job) reports it as "now" instead of the wall clock. This makes the
#: time-context line in the system prompt stable, so a recorded eval cassette key reproduces on
#: replay. Set ONLY by the eval harness (E6b); unset in production, where the wall clock is used.
_EVAL_CLOCK_ENV = "JARVIS_EVAL_CLOCK"


def _default_now() -> _dt.datetime:
    """Current time as an aware datetime in the machine's local zone (or a fixed eval clock)."""
    fixed = os.environ.get(_EVAL_CLOCK_ENV)
    if fixed:
        return _dt.datetime.fromisoformat(fixed)
    return _dt.datetime.now().astimezone()


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
    latency_ms: float = 0.0  # summed wall-clock of the turn's model calls (0.0 if unmeasured)
    # Populated by the ordinary browser-chat budget; None is unknown, never a fabricated $0.
    cost_usd: float | None = None
    model: str | None = None
    provider: str | None = None
    budget_usd: float | None = None


def _request_token_ceiling(
    *, system: str, messages: list[dict], tools: list[dict], margin: int
) -> int:
    """Conservative preflight input ceiling, used only for length and never persisted.

    UTF-8 wire bytes safely over-estimate normal text-token counts; a small fixed margin covers
    provider message framing. This lets the cap refuse before an external model call without
    logging or retaining the request contents.
    """
    wire = json.dumps(
        {"system": system, "messages": messages, "tools": tools},
        ensure_ascii=False,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return len(wire) + margin


class _ChatBudgetRefusal(Exception):
    """Private control flow for a preflight refusal before a router model call."""


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
        project: Callable[[], ProjectContext] | None = None,
        mode: Callable[[], Mode] | None = None,
        model_override: Callable[[], str | None] | None = None,
        effort_override: Callable[[], str | None] | None = None,
        router: object | None = None,
        client_selector: Callable[[object], object | None] | None = None,
        on_route: Callable[[object], None] | None = None,
        # Browser chat alone supplies these lower limits. Other loop owners retain config.limits.
        chat_limits: ChatConfig | None = None,
        pricing: PricingTable | None = None,
        provider_override: Callable[[], str | None] | None = None,
        cost_purpose: str = "turn",
        add_time_context: bool = False,
        now: Callable[[], _dt.datetime] = _default_now,
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
        # Phase 10: the active project scope, read once per turn (a callable so switching
        # projects on screen applies from the next turn). None => global scope, no extra.
        self.project = project
        # Phase 10: the run mode (Plan/Approval/Auto). None => Approval (the default and the
        # only mode background/voice loops ever run in). Plan/Auto enforcement lives in
        # _handle_tools, co-located with egress taint. auto_allow_tools is the opt-in Auto set.
        self.mode = mode
        # Phase 15.5: the interactive model selector. A callable returning the chosen model id, or
        # None to defer to the config default. Read once per turn (freezes like the project scope —
        # a switch applies next turn). None override => byte-identical to config.models.main.
        self.model_override = model_override
        # Phase 15.5: the per-model effort selector (UI cost control). A callable returning the
        # chosen output-config effort, or None to defer to the client's configured default. Read
        # once per turn (freezes like the model). None override => byte-identical (no effort key).
        self.effort_override = effort_override
        # Phase 15.6: cost-aware Auto routing. When ``router`` is set (the interactive UI loop), it
        # picks the turn's model/effort/mode per message (classify → tier) and ``client_selector``
        # returns the ledgered client for the routed provider (Anthropic vs Gemini). When None
        # (REPL / sub-agents / evals), the loop is byte-identical — self.client + model_override.
        # ``on_route`` (optional) receives each RouteDecision so a surface can show what was picked.
        self.router = router
        self.client_selector = client_selector
        self.on_route = on_route
        self.chat_limits = chat_limits
        self.pricing = pricing
        self.provider_override = provider_override
        self._auto_allow: frozenset[str] = frozenset(config.modes.auto_allow_tools)
        # Phase 10 cost ledger: the purpose recorded for this loop's completions ("turn" for
        # interactive, "subagent"/"orchestration" for children). Nested utility calls
        # (compaction/reflection/dedup/digest) override it via cost_scope at their call sites.
        self.cost_purpose = cost_purpose
        # Per-turn snapshot of "did this turn start in Auto" (pre-mortem #12): a mid-turn flip
        # INTO Auto must not retroactively auto-approve the in-flight turn.
        self._turn_started_auto = False
        # Phase-3: give the model the current date/time (it has no clock otherwise,
        # so scheduling relative times is impossible without it). Off by default so
        # the null path stays byte-identical to earlier phases.
        self.add_time_context = add_time_context
        self.now = now
        self.log = get_logger("jarvis.agent")
        # Phase 9 egress taint: set True once a reads_private tool runs in the current turn,
        # after which an egress tool's ALLOW is demoted to a non-persistable ASK. Reset per
        # turn in run_turn; safe as instance state because turns are serialized (turn_lock).
        self._turn_tainted = False
        # A UI cancellation needs the loop's private, partially-built transcript.  The snapshot
        # is deliberately read-only to callers and is reset for every turn.
        self._cancelled_messages: list[dict] | None = None

    @property
    def cancelled_messages(self) -> list[dict] | None:
        """Protocol-valid transcript snapshot for the most recently cancelled turn, if any."""
        return list(self._cancelled_messages) if self._cancelled_messages is not None else None

    def _record_cancellation(self, messages: list[dict]) -> None:
        self._cancelled_messages = _cancellation_snapshot(messages)

    def _system_with_extras(
        self, recall_block: str | None, summary: str | None, project_extra: str | None = None
    ) -> str:
        """Base system prompt plus dynamic extras, ordered stable → volatile
        (identity/guidance → active project → compaction summary → recalled memories →
        current time). The project extra sits just after the stable identity/guidance so
        it's a stable prefix within a project (a later cache breakpoint still hits)."""
        extras = [x for x in (project_extra, summary, recall_block) if x]
        if self.add_time_context:
            extras.append(f"Current date and time: {self.now():%Y-%m-%d %H:%M %Z} (user's local).")
        if not extras:
            return self.system
        return self.system + "\n\n" + "\n\n".join(extras)

    async def _recall_block(
        self, messages: list[dict], project: ProjectContext | None
    ) -> str | None:
        if self.memory is None:
            return None
        text = _latest_user_text(messages)
        if not text:
            return None
        # No project layer ⇒ unscoped recall (byte-identical to pre-Phase-10). With a project
        # layer, scope recall: global (project_id None) recalls only global memories; a project
        # recalls its own + global. This is what stops a global chat leaking project memories.
        if project is None:
            return await self.memory.auto_recall_context(text)
        return await self.memory.auto_recall_context(text, project_id=project.project_id)

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
        self._cancelled_messages = None
        total = Usage()
        total_latency_ms = 0.0
        # The lower limits apply only where the UI composition opted in below; all other loop
        # owners (voice, REPL, subagents, orchestration) continue to use the shared limits.
        limits = self.chat_limits or self.config.limits
        turn_budget_usd = (
            self.chat_limits.hard_stop_usd_per_turn
            if self.chat_limits is not None and self.chat_limits.hard_stop_usd_per_turn > 0
            else None
        )
        turn_cost_usd = 0.0
        pricing_known = True
        self._turn_tainted = False  # egress taint is per-turn (Phase 9)
        # Freeze the permissive side of mode for the whole turn (Phase 10, pre-mortem #12):
        # a mid-turn flip into Auto never applies to an in-flight turn. Plan (restrictive) is
        # read live per iteration, so tightening takes effect immediately.
        self._turn_started_auto = self.mode is not None and self.mode() is Mode.AUTO
        # Freeze the model for the whole turn (a mid-turn switch applies next turn, like project
        # scope). An override returning falsy defers to the config default => byte-identical.
        turn_model = self.config.models.main
        turn_effort: str | None = None
        turn_client = self.client
        turn_provider: str | None = None
        turn_mode: str | None = None
        turn_tools_enabled = True  # text-only routed providers (Gemini) get NO tools that turn
        router_budget_refusal: str | None = None
        # Bind session/project attribution before Auto's classifier runs: it is a model call too,
        # and must never escape the chat's execution context in the ledger.
        project = self.project() if self.project is not None else None
        project_extra = project.system_extra if project is not None else None
        base = cost_context.get()
        execution = current_execution_context()
        cost_context.set(
            _dc_replace(
                base,
                purpose=self.cost_purpose,
                project_id=(project.project_id if project is not None else base.project_id),
                session_id=(execution.session_id if execution is not None else base.session_id),
                trace_id=trace_id,
            )
        )

        async def classifier_preflight(provider: str, request: dict) -> None:
            """Reserve the router call before it can spend part of this chat turn's cap."""
            if self.pricing is None:
                raise _ChatBudgetRefusal(
                    "This chat is protected by a cost cap, but verified pricing for the routing "
                    "classifier is unavailable. No model call was made."
                )
            model = request.get("model")
            if not isinstance(model, str):
                raise _ChatBudgetRefusal(
                    "This chat is protected by a cost cap, but the routing model is invalid. "
                    "No model call was made."
                )
            estimated = self.pricing.cost(
                provider,
                model,
                Usage(
                    input_tokens=_request_token_ceiling(
                        system=str(request.get("system") or ""),
                        messages=list(request.get("messages") or []),
                        tools=list(request.get("tools") or []),
                        margin=self.chat_limits.input_token_margin if self.chat_limits else 0,
                    ),
                    output_tokens=int(request.get("max_tokens") or 0),
                ),
            )
            if estimated is None:
                raise _ChatBudgetRefusal(
                    "This chat is protected by a cost cap, but the routing classifier has no "
                    "verified price. No model call was made."
                )
            if turn_cost_usd + estimated > (turn_budget_usd or 0.0):
                raise _ChatBudgetRefusal(
                    f"This chat turn reached its ${turn_budget_usd:.2f} cost cap before the "
                    "routing classifier call. No model call was made."
                )

        async def classifier_account(provider: str, response: object) -> None:
            """Add the classifier's exact ledger-price result to this one turn's total."""
            nonlocal turn_cost_usd, pricing_known
            if self.pricing is None:
                pricing_known = False
                return
            model = getattr(response, "model", None)
            usage = getattr(response, "usage", None)
            if not isinstance(model, str) or not isinstance(usage, Usage):
                pricing_known = False
                return
            cost = self.pricing.cost(provider, model, usage)
            if cost is None:
                pricing_known = False
            else:
                turn_cost_usd += cost

        if self.router is not None:
            # Phase 15.6 cost-aware Auto/Manual routing (interactive UI loop only). Classify the
            # latest user message → a RouteDecision (model/effort/mode/provider), then select the
            # ledgered client for the routed provider. The router enforces the private_ok gate +
            # fail-closed fallback; this loop just applies its decision for the whole turn.
            try:
                decision = await self.router.route(
                    _latest_user_text(messages),
                    **(
                        {
                            "before_classifier": classifier_preflight,
                            "after_classifier": classifier_account,
                        }
                        if turn_budget_usd is not None
                        else {}
                    ),
                )
            except _ChatBudgetRefusal as exc:
                router_budget_refusal = str(exc)
            else:
                turn_model = decision.model or self.config.models.main
                turn_provider = decision.provider
                turn_effort = decision.effort
                turn_mode = decision.mode
                turn_tools_enabled = getattr(decision, "tools_enabled", True)
                if self.client_selector is not None:
                    turn_client = self.client_selector(decision) or self.client
                if self.on_route is not None:
                    self.on_route(decision)
                self.log.info(
                    "route_selected",
                    trace_id=trace_id,
                    provider=decision.provider,
                    model=turn_model,
                    tier=decision.tier,
                    mode=decision.mode,
                    reason=decision.reason,
                )
        else:
            # Legacy seam (REPL / tests): a manual model + effort override, or the config default.
            if self.model_override is not None:
                turn_model = self.model_override() or self.config.models.main
            turn_effort = self.effort_override() if self.effort_override is not None else None

        if turn_provider is None:
            turn_provider = (
                self.provider_override() if self.provider_override is not None else None
            ) or getattr(turn_client, "_provider", None)

        def budget_stop(message: str, *, iteration: int) -> TurnResult:
            """End a capped turn as a normal assistant response, never a raw exception.

            A capped tool-use response is intentionally omitted from history: leaving unmatched
            tool_use blocks would make the next transcript invalid. No tools run after this stop.
            """
            messages.append({"role": "assistant", "content": [{"type": "text", "text": message}]})
            emit(TextDelta(message))
            emit(TurnCompleted(text=message, stop_reason="cost_cap"))
            self.log.warning(
                "turn_end",
                stop_reason="cost_cap",
                iterations=iteration,
                cost_usd=round(turn_cost_usd, 6) if pricing_known else None,
            )
            return TurnResult(
                message,
                messages,
                "cost_cap",
                total,
                iteration,
                total_latency_ms,
                turn_cost_usd if pricing_known else None,
                turn_model,
                turn_provider,
                turn_budget_usd,
            )

        if router_budget_refusal is not None:
            return budget_stop(router_budget_refusal, iteration=0)
        if turn_budget_usd is not None and not pricing_known:
            return budget_stop(
                "The routing classifier returned without verified pricing. No further model "
                "call was made.",
                iteration=0,
            )
        if turn_budget_usd is not None and turn_cost_usd >= turn_budget_usd:
            return budget_stop(
                f"This chat turn reached its ${turn_budget_usd:.2f} cost cap at routing. No "
                "further model call was made.",
                iteration=0,
            )

        self.log.info("turn_start", trace_id=trace_id, model=turn_model, effort=turn_effort)

        # The classifier ran with the same session/project context above. Add the resolved
        # routing mode now that it is known, while preserving any caller attribution.
        base = cost_context.get()
        cost_context.set(
            _dc_replace(
                base,
                mode=turn_mode,  # Phase 15.6: 'auto'|'manual'|None → model_calls.routing_mode
            )
        )
        # Auto-recall runs once per turn, on the new user message (not per iteration), scoped
        # to the active project (see _recall_block).
        recall_block = await self._recall_block(messages, project)
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
                return TurnResult("", messages, "max_context", total, iteration, total_latency_ms)
            api_messages = view.messages if view is not None else messages
            if view is not None and (view.cut or view.elided):
                self.log.info(
                    "context_compacted",
                    cut=view.cut,
                    elided=view.elided,
                    sent_messages=len(api_messages),
                    full_messages=len(messages),
                )

            create_kwargs: dict = {
                "model": turn_model,
                "system": self._system_with_extras(
                    recall_block, summary=summary, project_extra=project_extra
                ),
                "messages": api_messages,
                # A text-only routed provider (Gemini) gets NO tools (the router only sends it
                # tool-free turns); every tool-capable route gets the full toolset (unchanged).
                "tools": self.registry.specs() if turn_tools_enabled else [],
                "max_tokens": limits.max_output_tokens,
                "on_text_delta": lambda t: emit(TextDelta(t)),
            }
            if turn_effort is not None:
                # Only when the human picked a non-default effort — otherwise the request stays
                # byte-identical (the client applies its configured default).
                create_kwargs["effort"] = turn_effort
            if self.config.context_reuse.enabled:
                # S7 (Phase 13): hand the live client the stable/volatile seam — the stable
                # framing (self.system) only, so only it is cached; the volatile, possibly-private
                # tail (project extra, compaction summary, memory recall, time) sits after the
                # breakpoint and is never cached. Passed ONLY when caching is on ⇒ a flag-off
                # turn's request is byte-identical to before the enable-step.
                create_kwargs["stable_prefix"] = self.system
            if turn_budget_usd is not None:
                if self.pricing is None or turn_provider is None:
                    return budget_stop(
                        "This chat is protected by a cost cap, but verified pricing for the "
                        "selected provider is unavailable. No model call was made.",
                        iteration=iteration,
                    )
                estimated = self.pricing.cost(
                    turn_provider,
                    turn_model,
                    Usage(
                        input_tokens=_request_token_ceiling(
                            system=create_kwargs["system"],
                            messages=api_messages,
                            tools=create_kwargs["tools"],
                            margin=self.chat_limits.input_token_margin if self.chat_limits else 0,
                        ),
                        output_tokens=limits.max_output_tokens,
                    ),
                )
                if estimated is None:
                    pricing_known = False
                    return budget_stop(
                        "This chat is protected by a cost cap, but the selected model has no "
                        "verified price. No model call was made.",
                        iteration=iteration,
                    )
                if turn_cost_usd + estimated > turn_budget_usd:
                    return budget_stop(
                        f"This chat turn reached its ${turn_budget_usd:.2f} cost cap before "
                        "the next model call. Try a shorter request or start a new turn.",
                        iteration=iteration,
                    )
            try:
                response = await turn_client.create(**create_kwargs)
            except asyncio.CancelledError:
                self._record_cancellation(messages)
                raise
            total = total + response.usage
            total_latency_ms += response.latency_ms or 0.0
            call_cost = (
                self.pricing.cost(turn_provider, response.model, response.usage)
                if turn_budget_usd is not None
                and self.pricing is not None
                and turn_provider is not None
                else None
            )
            if turn_budget_usd is not None:
                if call_cost is None:
                    pricing_known = False
                else:
                    turn_cost_usd += call_cost
            if self.context_manager is not None:
                self.context_manager.observe(response.usage)
            self.log.info(
                "model_call",
                iteration=iteration,
                model=response.model,
                stop_reason=response.stop_reason,
                input_tokens=response.usage.input_tokens,
                output_tokens=response.usage.output_tokens,
                cache_creation_input_tokens=response.usage.cache_creation_input_tokens,
                cache_read_input_tokens=response.usage.cache_read_input_tokens,
                latency_ms=(
                    round(response.latency_ms, 1) if response.latency_ms is not None else None
                ),
                cost_usd=(
                    round(call_cost, 6)
                    if turn_budget_usd is not None and call_cost is not None
                    else round(cost_of(response.model, response.usage), 6)
                ),
            )
            if response.stop_reason == "tool_use" and (
                not pricing_known
                or (turn_budget_usd is not None and turn_cost_usd >= turn_budget_usd)
            ):
                reason = (
                    "The selected model returned without verified pricing. No further model or "
                    "tool calls were made."
                    if not pricing_known
                    else f"This chat turn reached its ${turn_budget_usd:.2f} cost cap. No further "
                    "model or tool calls were made."
                )
                return budget_stop(reason, iteration=iteration + 1)
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
                    latency_ms=total_latency_ms,
                    cost_usd=(
                        turn_cost_usd
                        if pricing_known and turn_budget_usd is not None
                        else None
                    ),
                    model=response.model,
                    provider=turn_provider,
                    budget_usd=turn_budget_usd,
                )

            try:
                results = await self._handle_tools(tool_calls, emit)
            except asyncio.CancelledError:
                self._record_cancellation(messages)
                raise
            messages.append({"role": "user", "content": results})

        emit(TurnCompleted(text="", stop_reason="max_iterations"))
        self.log.warning("turn_end", stop_reason="max_iterations", iterations=limits.max_iterations)
        return TurnResult(
            text="",
            messages=messages,
            stop_reason="max_iterations",
            usage=total,
            iterations=limits.max_iterations,
            latency_ms=total_latency_ms,
            cost_usd=turn_cost_usd if pricing_known and turn_budget_usd is not None else None,
            model=turn_model,
            provider=turn_provider,
            budget_usd=turn_budget_usd,
        )

    async def _handle_tools(self, tool_calls: list[ToolCall], emit: EventSink) -> list[dict]:
        # Egress taint (Phase 9): a batch is tainted if the turn already is, OR if this same
        # batch contains any reads_private call — so a model emitting gmail_read + web_fetch
        # together can't slip the fetch through before the read "runs" (permission for the
        # whole batch is resolved before any executes). Order within the batch is irrelevant.
        effective_taint = self._turn_tainted or self._batch_reads_private(tool_calls)
        # The run mode, read live this iteration (Plan tightening applies immediately; the
        # permissive Auto side is gated by the per-turn _turn_started_auto snapshot).
        mode = self.mode() if self.mode is not None else Mode.APPROVAL

        # Phase 1 — resolve permission for each call sequentially (orderly prompts).
        resolved: list[tuple[ToolCall, object, Permission]] = []
        for call in tool_calls:
            tool = self.registry.get(call.name)
            if tool is None:
                self.log.warning("permission_decision", tool=call.name, reason="unknown_tool")
                # An unknown tool is a call the model attempted — emit it so observers
                # (the eval attempts log) see the attempt, then deny.
                emit(ToolDecision(call.name, call.input, gate_decision="deny", resolution="deny"))
                resolved.append((call, None, Permission.DENY))
                continue
            raw = self.gate.check(call.name, call.input, tool_default=tool.permission_default)
            decision = raw
            # Egress demotion: once private data is in play this turn, an egress ALLOW must
            # not run silently — it becomes a non-persistable ASK the human sees. The exfil
            # pipe (silent mail read → silent web_fetch) is structurally closed.
            egress = getattr(tool, "egress", False)
            if effective_taint and egress and raw.permission is Permission.ALLOW:
                decision = Decision(
                    Permission.ASK,
                    "private data was read this turn; sending off-box requires your approval",
                    persistable=False,
                )
                self.log.info("egress_taint_demotion", tool=call.name)
            # Plan mode (Phase 10): deny anything not in PLAN_SAFE, on the post-taint decision.
            # An allowlist, so a future unclassified tool fails closed. Applied before the
            # approver — a plan-denied tool never prompts and never runs.
            if plan_blocks(mode, call.name):
                decision = Decision(
                    Permission.DENY, "plan mode: only read-only tools are permitted"
                )
            self.log.info(
                "permission_decision",
                tool=call.name,
                permission=str(decision.permission),
                reason=decision.reason,
                mode=str(mode),
            )
            perm = decision.permission
            resolution = str(perm)
            if perm is Permission.ASK:
                # Auto mode (Phase 10): auto-approve a configured low-risk ASK — but ONLY on a
                # still-persistable decision, so a tainted-egress demotion (persistable=False)
                # always reaches the human. Visible as resolution="auto_approved" in Trace.
                if auto_approves(
                    mode=mode,
                    started_auto=self._turn_started_auto,
                    decision=decision,
                    tool_name=call.name,
                    auto_allow_tools=self._auto_allow,
                ):
                    perm = Permission.ALLOW
                    resolution = "auto_approved"
                    self.log.info("mode_auto_approved", tool=call.name, mode=str(mode))
                else:
                    perm = await self.approver(call, decision) if self.approver else Permission.DENY
                    resolution = str(perm)
                self.log.info("permission_resolved", tool=call.name, permission=str(perm))
            # Emitted before execution for EVERY call — including denials, which
            # ToolStarted (post-ALLOW only) never sees. This is what lets an eval
            # record what the model attempted, not just what ran. gate_decision is the RAW
            # gate verdict (pre-taint); resolution is the final disposition (incl. auto_approved).
            emit(
                ToolDecision(
                    call.name,
                    call.input,
                    gate_decision=str(raw.permission),
                    resolution=resolution,
                )
            )
            resolved.append((call, tool, perm))

        # A private read that WILL run taints the rest of the turn.
        if any(
            tool is not None and getattr(tool, "reads_private", False) and perm is Permission.ALLOW
            for _c, tool, perm in resolved
        ):
            self._turn_tainted = True

        # Phase 2 — run approved tools in parallel; denied/unknown become error results.
        return await asyncio.gather(*(self._run_one(c, t, p, emit) for c, t, p in resolved))

    def _batch_reads_private(self, tool_calls: list[ToolCall]) -> bool:
        """True if any call in this batch is a ``reads_private`` tool (order-independent)."""
        for call in tool_calls:
            tool = self.registry.get(call.name)
            if tool is not None and getattr(tool, "reads_private", False):
                return True
        return False

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
