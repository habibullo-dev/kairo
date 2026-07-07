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
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from dataclasses import replace as _dc_replace
from typing import TYPE_CHECKING

from jarvis.config import Config
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
from jarvis.core.prompts import build_system
from jarvis.observability import bind_trace, get_logger
from jarvis.observability.cost import Usage, cost_of
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


def _default_now() -> _dt.datetime:
    """Current time as an aware datetime in the machine's local zone."""
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
        total = Usage()
        total_latency_ms = 0.0
        limits = self.config.limits
        self._turn_tainted = False  # egress taint is per-turn (Phase 9)
        # Freeze the permissive side of mode for the whole turn (Phase 10, pre-mortem #12):
        # a mid-turn flip into Auto never applies to an in-flight turn. Plan (restrictive) is
        # read live per iteration, so tightening takes effect immediately.
        self._turn_started_auto = self.mode is not None and self.mode() is Mode.AUTO

        self.log.info("turn_start", trace_id=trace_id, model=self.config.models.main)

        # Snapshot the active project once per turn (a switch mid-conversation applies from
        # the next turn, not mid-flight). None provider => global scope, no extra.
        project = self.project() if self.project is not None else None
        project_extra = project.system_extra if project is not None else None
        # Bind the cost-ledger context for this turn: the loop OWNS purpose (from cost_purpose),
        # trace_id, and — when it has a project layer — project_id. It MERGES over the current
        # context so caller-set orchestration attribution (team / role / run / stage, set by
        # SubAgentService before run_turn) is preserved, not wiped. A child loop has no project
        # provider, so its run's project_id (set by the caller) rides through untouched.
        base = cost_context.get()
        cost_context.set(
            _dc_replace(
                base,
                purpose=self.cost_purpose,
                project_id=(project.project_id if project is not None else base.project_id),
                trace_id=trace_id,
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

            response = await self.client.create(
                model=self.config.models.main,
                system=self._system_with_extras(
                    recall_block, summary=summary, project_extra=project_extra
                ),
                messages=api_messages,
                tools=self.registry.specs(),
                max_tokens=limits.max_output_tokens,
                on_text_delta=lambda t: emit(TextDelta(t)),
            )
            total = total + response.usage
            total_latency_ms += response.latency_ms or 0.0
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
                    latency_ms=total_latency_ms,
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
            latency_ms=total_latency_ms,
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
