"""OrchestrationController — the UI seam over the OrchestrationEngine (Phase 10B Task 15).

Turns a Studio request (team + workflow + task brief) into an engine run, tracked as a single
in-flight background task (one orchestration run at a time, like one interactive turn at a
time). It:

* resolves the team (with per-project ``settings_json["teams"]`` overrides) + workflow;
* builds a minimal, provenance-tagged :class:`ContextBundle` (the task brief + project metadata
  — never private memory/mail here; richer context selection is a later refinement);
* runs a dry ``estimate`` for the two-step confirm before launching anything;
* streams the engine's schema-v2 lifecycle events to the browser via the connection manager.

Orchestration is project-scoped: a run needs an active project (``orchestration_runs.project_id``
is NOT NULL). The run title is composed from code constants (team + workflow names), never the
user's free-text brief — that brief only ever enters the framed, untrusted context bundle.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from kira.core.execution import ExecutionContext, bind_execution_context
from kira.models.registry import RouteError
from kira.observability import get_logger
from kira.orchestration import (
    WORKFLOWS,
    ContextBundle,
    RunEstimate,
    TeamProfile,
    resolve_team,
)
from kira.orchestration.context import ContextItem, Provenance
from kira.orchestration.engine import (
    ProviderContextError,
    ResumeUnavailableError,
    TeamWorkflowError,
)
from kira.skills import SkillPackError

if TYPE_CHECKING:
    from kira.orchestration import OrchestrationEngine
    from kira.projects import ProjectService
    from kira.projects.context import ProjectContext
    from kira.ui.connections import ConnectionManager


def serialize_estimate(est: RunEstimate | None) -> dict | None:
    """Cost metadata only — no prompts, no secrets. Feeds the Studio's estimate/confirm panel."""
    if est is None:
        return None
    return {
        "total_usd": est.total_usd,
        "head_usd": est.head_usd,
        "decision": est.decision,
        "reason": est.reason,
        "soft_warn": est.soft_warn,
        "over_team_budget": est.over_team_budget,
        "over_role_cap": list(est.over_role_cap),
        "unpriced": list(est.unpriced),
        "members": [
            {
                "member_id": m.member_id,
                "role": m.route_role,
                "provider": m.provider,
                "model": m.model,
                "turns": m.turns,
                "model_usd": m.model_usd,
                "service_usd": m.service_usd,
            }
            for m in est.members
        ],
    }


@dataclass(frozen=True, slots=True)
class OrchestrationCancellation:
    """One immutable ticket for an exact attended run's cancellation settlement."""

    run_id: int
    task: asyncio.Task[None]
    context: ExecutionContext | None


class OrchestrationController:
    def __init__(
        self,
        *,
        engine: OrchestrationEngine,
        connections: ConnectionManager,
        projects: ProjectService | None,
    ) -> None:
        self.engine = engine
        self.connections = connections
        self.projects = projects
        self._operation_lock = asyncio.Lock()
        self._task: asyncio.Task[None] | None = None
        self._current_run_id: int | None = None
        self._current_context: ExecutionContext | None = None
        self._current_project_id: int | None = None
        self._cancel_ticket: OrchestrationCancellation | None = None
        self.log = get_logger("kira.ui.orchestration")

    @property
    def busy(self) -> bool:
        return self._operation_lock.locked()

    def busy_for(self, context: ExecutionContext | None) -> bool:
        """Expose in-flight state only to the workspace that launched the run."""
        if not self.busy:
            return False
        if context is None:
            return True
        if self._current_context is not None:
            return context == self._current_context
        return context.project_id == self._current_project_id

    def busy_project(self, project_id: int) -> bool:
        """Whether the one in-flight run belongs to ``project_id``."""
        return self.busy and self._current_project_id == project_id

    def cancellable_run_id(self, context: ExecutionContext | None) -> int | None:
        """Return the exact attended run this workspace may cancel, if one exists.

        Automatic project assessments also reserve the shared engine, but they are not launched
        through ``self._task`` and deliberately remain outside attended Studio authority.  The
        browser therefore receives an exact run id instead of inferring cancellability from the
        broader ``busy`` flag or a persisted ``running`` row.
        """
        task = self._task
        run_id = self._current_run_id
        if (
            not self.busy
            or task is None
            or task.done()
            or not isinstance(run_id, int)
            or isinstance(run_id, bool)
            or run_id <= 0
            or (context is not None and context != self._current_context)
        ):
            return None
        return run_id

    async def _reserve(
        self,
        project_id: int,
        execution_context: ExecutionContext | None,
        *,
        wait: bool = False,
    ) -> bool:
        """Reserve the engine, then cross the shared service-policy admission fence."""
        if not wait and self._operation_lock.locked():
            return False
        await self._operation_lock.acquire()
        self._current_run_id = None
        self._current_context = execution_context
        self._current_project_id = project_id
        self._cancel_ticket = None
        barrier = getattr(self.projects, "service_access_lock", None)
        try:
            if barrier is not None:
                async with barrier:
                    pass
        except BaseException:
            self._release()
            raise
        return True

    def _release(self) -> None:
        self._current_run_id = None
        self._current_context = None
        self._current_project_id = None
        self._cancel_ticket = None
        if self._operation_lock.locked():
            self._operation_lock.release()

    async def _active_project_id(self) -> int | None:
        return self.projects.current().project_id if self.projects is not None else None

    async def _resolve(self, team_id: str, project_id: int | None) -> TeamProfile:
        """Resolve the team with any per-project override (never widened — validated by
        :func:`resolve_team`)."""
        overrides = None
        if self.projects is not None and project_id is not None:
            proj = await self.projects.store.get(project_id)
            if proj is not None:
                overrides = (proj.settings or {}).get("teams", {}).get(team_id)
        return resolve_team(team_id, overrides)

    def _build_context(self, task: str, project: ProjectContext | None = None) -> ContextBundle:
        items = [
            ContextItem(
                kind="task_brief",
                ref="brief",
                provenance=Provenance.PROJECT_NON_PRIVATE,
                text=task.strip(),
            )
        ]
        cur = project if project is not None else (
            self.projects.current() if self.projects is not None else None
        )
        if cur is not None and cur.name:
            items.append(
                ContextItem(
                    kind="project",
                    ref=f"project:{cur.project_id}",
                    provenance=Provenance.PROJECT_NON_PRIVATE,
                    text=cur.name,
                )
            )
        return ContextBundle(items=tuple(items))

    async def estimate(
        self,
        team_id: str,
        workflow_id: str,
        *,
        task: str = "",
        budget_usd: float | None = None,
        execution_context: ExecutionContext | None = None,
        project: ProjectContext | None = None,
    ) -> dict:
        """Dry-run the worst-case estimate for the Studio preview (no run row, no spawns)."""
        if workflow_id not in WORKFLOWS:
            return {"ok": False, "message": f"unknown workflow {workflow_id!r}"}
        pid = (
            execution_context.project_id
            if execution_context is not None
            else await self._active_project_id()
        )
        team = await self._resolve(team_id, pid)
        try:
            self.engine.validate_team_workflow(team, WORKFLOWS[workflow_id])
            est = self.engine.estimate(
                team,
                WORKFLOWS[workflow_id],
                self._build_context(task, project),
                budget_usd=budget_usd,
            )
        except (RouteError, SkillPackError, TeamWorkflowError) as exc:
            return {"ok": False, "message": str(exc)}
        return {"ok": True, "estimate": serialize_estimate(est)}

    async def start(
        self,
        *,
        team_id: str,
        workflow_id: str,
        task: str,
        budget_usd: float | None = None,
        confirmed: bool = False,
        execution_context: ExecutionContext | None = None,
        project: ProjectContext | None = None,
    ) -> tuple[dict, int]:
        """Launch one orchestration run in the background. Returns (body, http_status). 409 if a
        run is already in flight; 400 on a bad team/workflow or no active project; 200 with
        ``needs_confirmation`` when the estimate crosses the confirm threshold; 202 on launch."""
        if self.busy:
            return {"ok": False, "message": "an orchestration run is already in flight"}, 409
        if workflow_id not in WORKFLOWS:
            return {"ok": False, "message": f"unknown workflow {workflow_id!r}"}, 400
        if not task.strip():
            return {"ok": False, "message": "a task brief is required"}, 400
        pid = (
            execution_context.project_id
            if execution_context is not None
            else await self._active_project_id()
        )
        if pid is None:
            return {
                "ok": False,
                "message": "select a project first (teams are project-scoped)",
            }, 400
        try:
            team = await self._resolve(team_id, pid)
        except Exception as exc:  # noqa: BLE001 - an unknown team is a 400, not a 500
            return {"ok": False, "message": str(exc)}, 400
        workflow = WORKFLOWS[workflow_id]
        context = self._build_context(task, project)

        # Provider privacy + fail-closed routing (Phase 10C): refuse a PRIVATE bundle bound for a
        # non-trusted provider, and surface an unavailable/invalid route as a 400 — before any run
        # row or spawn (never a 500, never a silent launch that immediately dies).
        try:
            self.engine.validate_team_workflow(team, workflow)
            self.engine.check_provider_context(team, context)
            est = self.engine.estimate(team, workflow, context, budget_usd=budget_usd)
        except (ProviderContextError, RouteError, SkillPackError, TeamWorkflowError) as exc:
            return {"ok": False, "message": str(exc)}, 400

        # Two-step confirm: if the worst case needs confirmation and the caller hasn't confirmed,
        # return the estimate WITHOUT launching (the engine would raise; we pre-empt it cleanly).
        if est is not None and est.decision == "confirm" and not confirmed:
            body = {"ok": False, "needs_confirmation": True, "estimate": serialize_estimate(est)}
            return body, 200

        title = f"{team.name} · {workflow.title}"  # code constants only — never the raw brief
        if not await self._reserve(pid, execution_context):
            return {"ok": False, "message": "an orchestration run is already in flight"}, 409
        try:
            self._task = asyncio.create_task(
                self._run(pid, team, workflow, context, title, budget_usd, execution_context)
            )
        except BaseException:
            self._release()
            raise
        return {"ok": True, "started": True, "estimate": serialize_estimate(est)}, 202

    async def _run(
        self,
        project_id,
        team,
        workflow,
        context,
        title,
        budget_usd,
        execution_context: ExecutionContext | None,
    ) -> None:
        try:
            async def sink(payload: dict) -> None:
                await self._sink(payload, execution_context)

            if execution_context is None:
                await self.engine.run(
                    project_id=project_id,
                    team=team,
                    workflow=workflow,
                    context=context,
                    title=title,
                    budget_usd=budget_usd,
                    confirmed=True,
                    on_event=sink,
                )
            else:
                with bind_execution_context(execution_context):
                    await self.engine.run(
                        project_id=project_id,
                        team=team,
                        workflow=workflow,
                        context=context,
                        title=title,
                        budget_usd=budget_usd,
                        confirmed=True,
                        execution_context=execution_context,
                        on_event=sink,
                    )
        except asyncio.CancelledError:
            pass  # the engine already recorded 'cancelled' (shielded) before re-raising
        except Exception:  # noqa: BLE001 - a background run must not crash the server
            self.log.exception("orchestration_run_failed")
        finally:
            self._release()

    async def run_automatic_project_assessment(
        self,
        *,
        project_id: int,
        context: ContextBundle,
        budget_usd: float,
        on_event: Callable[[dict], Awaitable[None]] | None = None,
        on_created_in_transaction: Callable[[int], Awaitable[None]] | None = None,
    ) -> int:
        """Wait for the shared engine, then run the fixed read-only assessment roster.

        The caller chooses neither team nor workflow.  Feature enablement is standing consent
        for this read-only fan-out; ``budget_usd`` remains a hard reservation ceiling.
        """
        if project_id <= 0 or budget_usd <= 0:
            raise ValueError("automatic assessment requires a project and positive budget")
        await self._reserve(project_id, None, wait=True)
        try:
            team = resolve_team("project_intelligence")  # fixed code roster; no project overrides
            workflow = WORKFLOWS["project_assessment"]

            async def sink(payload: dict) -> None:
                if payload.get("kind") == "orchestration_started":
                    self._current_run_id = payload.get("run_id")
                if on_event is not None:
                    await on_event(payload)

            return await self.engine.run(
                project_id=project_id,
                team=team,
                workflow=workflow,
                context=context,
                title=f"{team.name} · {workflow.title}",
                budget_usd=budget_usd,
                confirmed=True,
                on_event=sink,
                on_created_in_transaction=on_created_in_transaction,
            )
        finally:
            self._release()

    async def resume(
        self,
        run_id: int,
        *,
        task: str,
        execution_context: ExecutionContext | None = None,
        project: ProjectContext | None = None,
    ) -> tuple[dict, int]:
        """Explicitly continue the one safe post-synthesis checkpoint.

        The original brief was never persisted.  The user therefore re-enters it here; the
        engine compares only its bodies-free manifest before it can atomically consume a
        checkpoint and let the writer run.
        """
        if self.busy:
            return {"ok": False, "message": "an orchestration run is already in flight"}, 409
        if not task.strip():
            return {"ok": False, "message": "re-enter the original task brief to continue"}, 400
        pid = (
            execution_context.project_id
            if execution_context is not None
            else await self._active_project_id()
        )
        if pid is None:
            return {"ok": False, "message": "select a project first"}, 400
        run = await self.engine.store.get(run_id)
        if run is None or run.project_id != pid:
            return {"ok": False, "message": "no such orchestration run"}, 404
        team_id = str(run.config.get("team") or "")
        if not team_id or run.workflow not in WORKFLOWS:
            return {"ok": False, "message": "checkpoint configuration is no longer available"}, 400
        try:
            team = await self._resolve(team_id, pid)
            context = self._build_context(task, project)
            # Fail before the background task is launched so Studio can show a useful recovery
            # message instead of merely logging a rejected continuation.
            self.engine.validate_resume(
                run,
                project_id=pid,
                team=team,
                workflow=WORKFLOWS[run.workflow],
                context=context,
            )
        except (
            ProviderContextError,
            ResumeUnavailableError,
            RouteError,
            SkillPackError,
            TeamWorkflowError,
        ) as exc:
            return {"ok": False, "message": str(exc)}, 400
        if not await self._reserve(pid, execution_context):
            return {"ok": False, "message": "an orchestration run is already in flight"}, 409
        try:
            self._task = asyncio.create_task(
                self._resume_run(
                    run_id,
                    pid,
                    team,
                    WORKFLOWS[run.workflow],
                    context,
                    execution_context,
                )
            )
        except BaseException:
            self._release()
            raise
        return {"ok": True, "resumed": True, "run_id": run_id}, 202

    async def _resume_run(
        self,
        run_id: int,
        project_id: int,
        team: TeamProfile,
        workflow,
        context: ContextBundle,
        execution_context: ExecutionContext | None,
    ) -> None:
        try:
            async def sink(payload: dict) -> None:
                await self._sink(payload, execution_context)

            if execution_context is None:
                await self.engine.resume(
                    run_id=run_id,
                    project_id=project_id,
                    team=team,
                    workflow=workflow,
                    context=context,
                    on_event=sink,
                )
            else:
                with bind_execution_context(execution_context):
                    await self.engine.resume(
                        run_id=run_id,
                        project_id=project_id,
                        team=team,
                        workflow=workflow,
                        context=context,
                        execution_context=execution_context,
                        on_event=sink,
                    )
        except asyncio.CancelledError:
            pass
        except Exception:  # noqa: BLE001 - a background recovery must not crash the server
            self.log.exception("orchestration_resume_failed")
        finally:
            self._release()

    async def _sink(self, payload: dict, execution_context: ExecutionContext | None) -> None:
        """Capture the run id from the start event (so cancel can target it), then broadcast."""
        if payload.get("kind") in {"orchestration_started", "orchestration_resumed"}:
            run_id = payload.get("run_id")
            self._current_run_id = (
                run_id
                if isinstance(run_id, int) and not isinstance(run_id, bool) and run_id > 0
                else None
            )
            self._current_context = execution_context
        await self.connections.publish(execution_context, payload)

    def request_cancel(
        self,
        run_id: int,
        *,
        execution_context: ExecutionContext | None = None,
    ) -> OrchestrationCancellation | None:
        """Signal one exact attended run once and return its immutable settlement ticket.

        The run id is bound only by the engine's started/resumed event. Before that point no
        arbitrary id can cancel the shared task. Repeated requests reuse the same ticket and never
        inject a second ``CancelledError`` into the engine's terminal persistence.
        """
        task = self._task
        if (
            not self.busy
            or task is None
            or task.done()
            or self._current_run_id != run_id
            or (execution_context is not None and execution_context != self._current_context)
        ):
            return None
        ticket = self._cancel_ticket
        if ticket is not None:
            return ticket if ticket.task is task and ticket.run_id == run_id else None
        ticket = OrchestrationCancellation(
            run_id=run_id,
            task=task,
            context=self._current_context,
        )
        self._cancel_ticket = ticket
        task.cancel()
        return ticket

    def cancel(self, run_id: int, *, execution_context: ExecutionContext | None = None) -> bool:
        """Compatibility wrapper for non-HTTP callers; prefer :meth:`request_cancel`."""
        return self.request_cancel(run_id, execution_context=execution_context) is not None
