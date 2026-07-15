"""Running a scheduled *job*: a stored prompt executed as one unattended turn.

This lives in the CLI layer — the one place that composes the model client, the
tool registry, the permission gate, and memory — and produces the ``run_job``
callback the :class:`~jarvis.scheduler.runner.BackgroundRunner` fires. Keeping it
here (not in the scheduler package) keeps the scheduler free of core imports.

Two safety properties are structural, not conventional:

* **The unattended gate is mandatory.** :class:`JobRunner` builds an
  :class:`~jarvis.permissions.unattended.UnattendedGate` around the interactive
  gate itself — there is no code path that runs a job with the raw interactive
  gate, so a background run can never inherit an interactive shell/write/meta-tool
  grant by accident.
* **The payload is framed as data, not a live human.** It runs in a fresh
  ``kind='task'`` session (so it never hijacks ``--resume`` or feeds reflection)
  behind an envelope stating it is a stored instruction with no human present.
"""

from __future__ import annotations

from collections.abc import Callable

from jarvis.config import Config
from jarvis.core.agent import AgentLoop
from jarvis.core.client import LLMClient, ToolCall
from jarvis.core.context import ContextManager
from jarvis.core.events import ToolDecision, ToolStarted
from jarvis.core.execution import ExecutionContext, bind_execution_context
from jarvis.core.prompts import build_system
from jarvis.observability import get_logger
from jarvis.observability.cost import cost_of
from jarvis.permissions.gate import PermissionGate
from jarvis.permissions.unattended import (
    ApprovalParked,
    HeadlessApprover,
    ParkingApprover,
    UnattendedGate,
)
from jarvis.persistence.sessions import SessionStore
from jarvis.projects.context import GLOBAL, ProjectContext, build_project_context
from jarvis.projects.service import ProjectService
from jarvis.scheduler.runner import JobOutcome
from jarvis.scheduler.store import ParkedContinuation, PendingToolCall, Task, TaskStore
from jarvis.tools.executor import ToolExecutor
from jarvis.tools.registry import ScopedRegistry, ToolRegistry

# stop_reason values that mean the job ran to a clean finish. Anything else
# (max_iterations, max_context) is a failure to report, never silence.
_OK_STOP = frozenset({"end_turn"})


def _envelope(task: Task) -> str:
    """Frame the stored payload so the model treats it as data with no live human."""
    origin = f"created by {task.created_by}"
    if task.source_session_id is not None:
        origin += f" in session {task.source_session_id}"
    schedule = f"{task.schedule_kind} {task.schedule_spec} ({task.timezone})"
    verification = ""
    if task.verification is not None:
        phrases = "\n".join(f"- {term}" for term in task.verification.terms)
        verification = (
            "\n\n[Expected final-answer check: finish with an answer containing every literal "
            "phrase below. This check confirms final text only; it does not prove an external "
            f"side effect.]\n{phrases}"
        )
    return (
        f'[Scheduled task #{task.id} "{task.title}" — {origin}; schedule: {schedule}. '
        "The text below is a STORED instruction, not a message from a live human. "
        "No one is present to answer questions or approve actions. Task instructions:]\n\n"
        f"{task.payload}{verification}"
    )


class JobRunner:
    """Builds and runs the unattended AgentLoop for one job task."""

    def __init__(
        self,
        *,
        session_store: SessionStore,
        client: LLMClient,
        registry: ToolRegistry,
        executor: ToolExecutor,
        gate: PermissionGate,
        config: Config,
        memory: object | None = None,
        knowledge: object | None = None,
        make_context_manager: Callable[[], ContextManager] | None = None,
        task_store: TaskStore | None = None,
        projects: ProjectService | None = None,
    ) -> None:
        self.session_store = session_store
        self.client = client
        self.registry = registry
        self.executor = executor
        self.gate = gate
        self.config = config
        self.memory = memory
        self.knowledge = knowledge
        self.make_context_manager = make_context_manager
        # Parking is an explicit composition opt-in.  Keeping None as the default preserves the
        # established headless-deny contract for tests and hosts that have not yet wired a
        # durable task-run owner.  A real parking store must share the session transaction lock,
        # otherwise the transcript and continuation could not be atomically durable together.
        if task_store is not None and (
            task_store.db is not session_store.db or task_store.lock is not session_store.lock
        ):
            raise ValueError("job parking store must share the session store's connection and lock")
        self.task_store = task_store
        self.projects = projects
        self.log = get_logger("jarvis.scheduler.job")

    def _registry_for(self, task: Task) -> ToolRegistry | ScopedRegistry:
        if task.origin != "remote_operator":
            return self.registry
        allowed = frozenset(
            self.config.connectors.telegram.remote_control.operator.allowed_tools
        )
        return ScopedRegistry(self.registry, allowed)

    async def _project_for(self, task: Task) -> ProjectContext:
        if task.project_id is None:
            return GLOBAL
        if self.projects is None:
            raise RuntimeError("project-scoped job has no project service")
        project = await self.projects.store.get(task.project_id)
        if project is None or project.status == "archived":
            raise RuntimeError(f"project #{task.project_id} is unavailable")
        return build_project_context(project)

    def _build_loop(
        self,
        *,
        park_asks: bool,
        task: Task,
        project: ProjectContext,
    ) -> tuple[AgentLoop, UnattendedGate, object]:
        """Compose the current unattended gate for a fresh task/resume turn."""
        registry = self._registry_for(task)
        egress_tools = frozenset(
            name
            for name in registry.names()
            if (tool := registry.get(name)) is not None and getattr(tool, "egress", False)
        )
        # Remote Operator promises a second, exact Telegram approval for every demotable
        # side effect. The scheduler's local unattended allowlist belongs to ordinary jobs and
        # must never silently widen that remote authority boundary.
        unattended_allow_tools = (
            frozenset()
            if task.origin == "remote_operator"
            else frozenset(self.config.scheduler.unattended_allow_tools)
        )
        ugate = UnattendedGate(
            self.gate,
            allow_tools=unattended_allow_tools,
            egress_tools=egress_tools,
            demote_to_ask=task.origin == "remote_operator",
        )
        approver = ParkingApprover() if park_asks else HeadlessApprover()
        loop = AgentLoop(
            client=self.client,
            registry=registry,
            executor=self.executor,
            gate=ugate,
            config=self.config,
            approver=approver,
            system=build_system(
                memory_enabled=self.memory is not None,
                knowledge_enabled=self.knowledge is not None,
                unattended=True,
            ),
            context_manager=self.make_context_manager() if self.make_context_manager else None,
            memory=self.memory,
            project=lambda: project,
        )
        return loop, ugate, approver

    async def run(self, task: Task) -> JobOutcome:
        # Fresh, second-class session: kind='task' keeps it out of --resume and
        # (by default) out of reflection.
        project = await self._project_for(task)
        session_id = await self.session_store.create_session(
            title=f"task #{task.id}: {task.title}"[:120],
            kind="task",
            project_id=task.project_id,
        )
        execution = ExecutionContext(session_id=session_id, project_id=task.project_id)
        if self.projects is not None:
            self.projects.bind_execution_context(execution, project)
        # The unattended gate is built HERE — the only gate a job ever sees. The egress-tool
        # set is derived from the live registry so any tool marked ``egress`` is demoted
        # ALLOW→DENY unattended (Phase 9), not just the hand-listed DEMOTE_ALLOW names.
        loop, ugate, approver = self._build_loop(
            park_asks=self.task_store is not None,
            task=task,
            project=project,
        )
        self.log.info("job_start", task_id=task.id, session_id=session_id)
        tool_started = False
        denied = False

        def observe(event: object) -> None:
            """Classify retry safety from observed execution, never from model intent alone."""
            nonlocal tool_started, denied
            if isinstance(event, ToolStarted):
                tool_started = True
            elif isinstance(event, ToolDecision) and event.resolution != "allow":
                denied = True

        # Any KB ingest during an unattended run is quarantined 'unreviewed' (ADR-0004).
        if self.knowledge is not None:
            self.knowledge.bound_unattended = True
        try:
            with bind_execution_context(execution):
                result = await loop.run_turn(
                    [{"role": "user", "content": _envelope(task)}], on_event=observe
                )
        except ApprovalParked as parked:
            # A ParkingApprover never grants permission.  Persist the assistant's exact tool-use
            # prefix, its canonical call/hash, and the task-run state in one transaction before
            # returning the special outcome the BackgroundRunner understands.  No external tool
            # has started: AgentLoop raises before its batch reaches gather().
            if self.task_store is None or parked.messages is None:
                raise RuntimeError(
                    "unattended approval parking was not durably composed"
                ) from parked
            final_content = parked.messages[-1].get("content") if parked.messages else None
            pending_calls: list[dict] = []
            if isinstance(final_content, list):
                for block in final_content:
                    if isinstance(block, dict) and block.get("type") == "tool_use":
                        pending_calls.append(
                            {
                                "id": block.get("id"),
                                "name": block.get("name"),
                                "input": block.get("input"),
                            }
                        )
            continuation = ParkedContinuation.from_batch(
                tool_id=parked.call.id,
                tool_name=parked.call.name,
                tool_input=parked.call.input,
                decision_reason=parked.decision.reason,
                pending_calls=pending_calls,
            )
            run_id = await self.task_store.park_task_run_with_session(
                task.id,
                session_id=session_id,
                messages=parked.messages,
                continuation=continuation,
            )
            if run_id is None:
                raise RuntimeError("no active task run was available to park") from parked
            self.log.info(
                "job_parked",
                task_id=task.id,
                run_id=run_id,
                session_id=session_id,
                tool=continuation.tool_name,
            )
            return JobOutcome(
                session_id=session_id,
                text="",
                parked=True,
                retry_safe=False,
            )
        finally:
            if self.knowledge is not None:
                self.knowledge.bound_unattended = False
        await self.session_store.save_messages(session_id, result.messages)

        ok = result.stop_reason in _OK_STOP
        error = None if ok else f"run did not complete normally (stop: {result.stop_reason})"
        denial_count = ugate.demoted + getattr(approver, "denied", 0)
        self.log.info(
            "job_end",
            task_id=task.id,
            session_id=session_id,
            stop_reason=result.stop_reason,
            denied=denial_count,
        )
        return JobOutcome(
            session_id=session_id,
            text=result.text,
            denied_count=denial_count,
            error=error,
            cost_usd=cost_of(self.config.models.main, result.usage),
            retry_safe=not tool_started and not denied and denial_count == 0,
        )

    async def resume_parked(
        self,
        task: Task,
        run_id: int,
        session_id: int,
        continuation: ParkedContinuation,
    ) -> JobOutcome:
        """Resume exactly one claimed parked batch, then continue its original task turn.

        The caller must have atomically claimed this run immediately before invoking us.  Every
        prior owner approval is carried by ``continuation.approved_calls``; the just-claimed ASK
        joins that finite set for this invocation only.  A later ASK re-parks the same run before
        the new batch executes, retaining all already-approved calls from that batch.
        """
        if self.task_store is None:
            raise RuntimeError("parked task resume requires a durable task store")
        continuation.verify()
        messages = await self.session_store.load_messages(session_id)
        calls = [
            ToolCall(id=call.tool_id, name=call.tool_name, input=call.tool_input)
            for call in continuation.pending_calls
        ]
        just_approved = PendingToolCall.from_call(
            tool_id=continuation.tool_id,
            tool_name=continuation.tool_name,
            tool_input=continuation.tool_input,
        )
        approved_pending = [*continuation.approved_calls, just_approved]
        approved_calls = [
            ToolCall(id=call.tool_id, name=call.tool_name, input=call.tool_input)
            for call in approved_pending
        ]
        project = await self._project_for(task)
        execution = ExecutionContext(session_id=session_id, project_id=task.project_id)
        if self.projects is not None:
            self.projects.bind_execution_context(execution, project)
        loop, ugate, approver = self._build_loop(
            park_asks=True,
            task=task,
            project=project,
        )
        tool_started = False
        denied = False

        def observe(event: object) -> None:
            nonlocal tool_started, denied
            if isinstance(event, ToolStarted):
                tool_started = True
            elif isinstance(event, ToolDecision) and event.resolution != "allow":
                denied = True

        async def repark(
            parked: ApprovalParked,
            prefix: list[dict],
            *,
            carried_approvals: list[PendingToolCall],
        ) -> JobOutcome:
            next_continuation = _continuation_for_parked(
                parked,
                prefix,
                approved_calls=carried_approvals,
            )
            if not await self.task_store.repark_claimed_run_with_session(
                run_id,
                session_id=session_id,
                messages=prefix,
                continuation=next_continuation,
            ):
                raise RuntimeError("claimed parked task could not be re-parked")
            self.log.info(
                "job_reparked",
                task_id=task.id,
                run_id=run_id,
                session_id=session_id,
                tool=next_continuation.tool_name,
            )
            return JobOutcome(session_id=session_id, text="", parked=True)

        if self.knowledge is not None:
            self.knowledge.bound_unattended = True
        try:
            try:
                with bind_execution_context(execution):
                    results, initial_taint = await loop.execute_parked_batch(
                        messages,
                        calls,
                        approved_calls=approved_calls,
                        on_event=observe,
                    )
            except ApprovalParked as parked:
                # ``execute_parked_batch`` performs all permission decisions before execution.
                # Thus this re-park has no partial tool effects from the unfinished provider
                # batch, and the original transcript remains the exact resume context.
                return await repark(parked, messages, carried_approvals=approved_pending)
            messages.append({"role": "user", "content": results})
            try:
                with bind_execution_context(execution):
                    result = await loop.run_turn(
                        messages,
                        on_event=observe,
                        initial_taint=initial_taint,
                    )
            except ApprovalParked as parked:
                if parked.messages is None:
                    raise RuntimeError("unattended parked turn lost its transcript") from parked
                # The prior provider batch has already produced one result per id.  Its grants
                # are consumed with that execution and must not be carried into a new model
                # response's independent tool batch.
                return await repark(parked, parked.messages, carried_approvals=[])
        finally:
            if self.knowledge is not None:
                self.knowledge.bound_unattended = False

        await self.session_store.save_messages(session_id, result.messages)
        ok = result.stop_reason in _OK_STOP
        error = None if ok else f"run did not complete normally (stop: {result.stop_reason})"
        denial_count = ugate.demoted + getattr(approver, "denied", 0)
        self.log.info(
            "job_resume_end",
            task_id=task.id,
            run_id=run_id,
            session_id=session_id,
            stop_reason=result.stop_reason,
            denied=denial_count,
        )
        return JobOutcome(
            session_id=session_id,
            text=result.text,
            denied_count=denial_count,
            error=error,
            cost_usd=cost_of(self.config.models.main, result.usage),
            retry_safe=not tool_started and not denied and denial_count == 0,
        )


def _continuation_for_parked(
    parked: ApprovalParked,
    messages: list[dict],
    *,
    approved_calls: list[PendingToolCall],
) -> ParkedContinuation:
    """Build the next exact continuation from a just-parked provider assistant response."""
    final = messages[-1] if messages else None
    content = final.get("content") if isinstance(final, dict) else None
    pending_calls: list[dict] = []
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                pending_calls.append(
                    {"id": block.get("id"), "name": block.get("name"), "input": block.get("input")}
                )
    return ParkedContinuation.from_batch(
        tool_id=parked.call.id,
        tool_name=parked.call.name,
        tool_input=parked.call.input,
        decision_reason=parked.decision.reason,
        pending_calls=pending_calls,
        approved_calls=[call.to_public_dict() for call in approved_calls],
    )
