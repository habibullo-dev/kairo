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
from typing import TYPE_CHECKING

from jarvis.models.registry import RouteError
from jarvis.observability import get_logger
from jarvis.orchestration import (
    WORKFLOWS,
    ContextBundle,
    RunEstimate,
    TeamProfile,
    resolve_team,
)
from jarvis.orchestration.context import ContextItem, Provenance
from jarvis.orchestration.engine import ProviderContextError

if TYPE_CHECKING:
    from jarvis.orchestration import OrchestrationEngine
    from jarvis.projects import ProjectService
    from jarvis.ui.connections import ConnectionManager


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
        self._task: asyncio.Task | None = None
        self._current_run_id: int | None = None
        self.log = get_logger("jarvis.ui.orchestration")

    @property
    def busy(self) -> bool:
        return self._task is not None and not self._task.done()

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

    def _build_context(self, task: str) -> ContextBundle:
        items = [
            ContextItem(
                kind="task_brief",
                ref="brief",
                provenance=Provenance.PROJECT_NON_PRIVATE,
                text=task.strip(),
            )
        ]
        cur = self.projects.current() if self.projects is not None else None
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
        self, team_id: str, workflow_id: str, *, task: str = "", budget_usd: float | None = None
    ) -> dict:
        """Dry-run the worst-case estimate for the Studio preview (no run row, no spawns)."""
        if workflow_id not in WORKFLOWS:
            return {"ok": False, "message": f"unknown workflow {workflow_id!r}"}
        pid = await self._active_project_id()
        team = await self._resolve(team_id, pid)
        est = self.engine.estimate(
            team, WORKFLOWS[workflow_id], self._build_context(task), budget_usd=budget_usd
        )
        return {"ok": True, "estimate": serialize_estimate(est)}

    async def start(
        self,
        *,
        team_id: str,
        workflow_id: str,
        task: str,
        budget_usd: float | None = None,
        confirmed: bool = False,
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
        pid = await self._active_project_id()
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
        context = self._build_context(task)

        # Provider privacy + fail-closed routing (Phase 10C): refuse a PRIVATE bundle bound for a
        # non-trusted provider, and surface an unavailable/invalid route as a 400 — before any run
        # row or spawn (never a 500, never a silent launch that immediately dies).
        try:
            self.engine.check_provider_context(team, context)
            est = self.engine.estimate(team, workflow, context, budget_usd=budget_usd)
        except (ProviderContextError, RouteError) as exc:
            return {"ok": False, "message": str(exc)}, 400

        # Two-step confirm: if the worst case needs confirmation and the caller hasn't confirmed,
        # return the estimate WITHOUT launching (the engine would raise; we pre-empt it cleanly).
        if est is not None and est.decision == "confirm" and not confirmed:
            body = {"ok": False, "needs_confirmation": True, "estimate": serialize_estimate(est)}
            return body, 200

        title = f"{team.name} · {workflow.title}"  # code constants only — never the raw brief
        self._current_run_id = None
        self._task = asyncio.create_task(self._run(pid, team, workflow, context, title, budget_usd))
        return {"ok": True, "started": True, "estimate": serialize_estimate(est)}, 202

    async def _run(self, project_id, team, workflow, context, title, budget_usd) -> None:
        try:
            await self.engine.run(
                project_id=project_id,
                team=team,
                workflow=workflow,
                context=context,
                title=title,
                budget_usd=budget_usd,
                confirmed=True,  # start() already cleared the confirm gate
                on_event=self._sink,
            )
        except asyncio.CancelledError:
            pass  # the engine already recorded 'cancelled' (shielded) before re-raising
        except Exception:  # noqa: BLE001 - a background run must not crash the server
            self.log.exception("orchestration_run_failed")
        finally:
            self._current_run_id = None

    async def _sink(self, payload: dict) -> None:
        """Capture the run id from the start event (so cancel can target it), then broadcast."""
        if payload.get("kind") == "orchestration_started":
            self._current_run_id = payload.get("run_id")
        await self.connections.broadcast(payload)

    def cancel(self, run_id: int) -> bool:
        """Cancel the in-flight run if it matches ``run_id`` (one run at a time). The engine's
        shielded handler records 'cancelled'; the orphan sweep is the backstop."""
        if self.busy and (self._current_run_id is None or run_id == self._current_run_id):
            self._task.cancel()  # type: ignore[union-attr]
            return True
        return False
