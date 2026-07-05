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
from jarvis.core.client import LLMClient
from jarvis.core.context import ContextManager
from jarvis.core.prompts import build_system
from jarvis.observability import get_logger
from jarvis.observability.cost import cost_of
from jarvis.permissions.gate import PermissionGate
from jarvis.permissions.unattended import HeadlessApprover, UnattendedGate
from jarvis.persistence.sessions import SessionStore
from jarvis.scheduler.runner import JobOutcome
from jarvis.scheduler.store import Task
from jarvis.tools.executor import ToolExecutor
from jarvis.tools.registry import ToolRegistry

# stop_reason values that mean the job ran to a clean finish. Anything else
# (max_iterations, max_context) is a failure to report, never silence.
_OK_STOP = frozenset({"end_turn"})


def _envelope(task: Task) -> str:
    """Frame the stored payload so the model treats it as data with no live human."""
    origin = f"created by {task.created_by}"
    if task.source_session_id is not None:
        origin += f" in session {task.source_session_id}"
    schedule = f"{task.schedule_kind} {task.schedule_spec} ({task.timezone})"
    return (
        f'[Scheduled task #{task.id} "{task.title}" — {origin}; schedule: {schedule}. '
        "The text below is a STORED instruction, not a message from a live human. "
        "No one is present to answer questions or approve actions. Task instructions:]\n\n"
        f"{task.payload}"
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
        make_context_manager: Callable[[], ContextManager] | None = None,
    ) -> None:
        self.session_store = session_store
        self.client = client
        self.registry = registry
        self.executor = executor
        self.gate = gate
        self.config = config
        self.memory = memory
        self.make_context_manager = make_context_manager
        self.log = get_logger("jarvis.scheduler.job")

    async def run(self, task: Task) -> JobOutcome:
        # Fresh, second-class session: kind='task' keeps it out of --resume and
        # (by default) out of reflection.
        session_id = await self.session_store.create_session(
            title=f"task #{task.id}: {task.title}"[:120], kind="task"
        )
        # The unattended gate is built HERE — the only gate a job ever sees.
        ugate = UnattendedGate(
            self.gate,
            allow_tools=frozenset(self.config.scheduler.unattended_allow_tools),
        )
        approver = HeadlessApprover()
        loop = AgentLoop(
            client=self.client,
            registry=self.registry,
            executor=self.executor,
            gate=ugate,
            config=self.config,
            approver=approver,
            system=build_system(memory_enabled=self.memory is not None, unattended=True),
            context_manager=self.make_context_manager() if self.make_context_manager else None,
            memory=self.memory,
        )
        self.log.info("job_start", task_id=task.id, session_id=session_id)
        result = await loop.run_turn([{"role": "user", "content": _envelope(task)}])
        await self.session_store.save_messages(session_id, result.messages)

        ok = result.stop_reason in _OK_STOP
        error = None if ok else f"run did not complete normally (stop: {result.stop_reason})"
        denied = ugate.demoted + approver.denied
        self.log.info(
            "job_end",
            task_id=task.id,
            session_id=session_id,
            stop_reason=result.stop_reason,
            denied=denied,
        )
        return JobOutcome(
            session_id=session_id,
            text=result.text,
            denied_count=denied,
            error=error,
            cost_usd=cost_of(self.config.models.main, result.usage),
        )
