"""ProjectService: holds the active project for a process and hands the loop a context.

One service per process (one process = REPL | UI | voice at a time, so a single active
project is the right model; voice inherits whatever the screen last activated — amendment
A3's global-fallback falls out because the default is :data:`GLOBAL`). ``current`` is the
callable the ``AgentLoop`` reads each turn; ``activate`` changes it (a UI switch or REPL
``project use``). The service does not touch sessions — the surface starts a *new* session
on switch, so a session stays bound to one project for its life (reflection/promotion
attribute to it).
"""

from __future__ import annotations

import asyncio

from jarvis.core.execution import ExecutionContext, current_execution_context
from jarvis.projects.context import GLOBAL, ProjectContext, build_project_context
from jarvis.projects.store import Project, ProjectStore


class ProjectService:
    def __init__(self, store: ProjectStore) -> None:
        self.store = store
        # A short policy-admission fence shared with orchestration. It is held only while a
        # durable service selection and every live immutable context become one atomic state.
        self.service_access_lock = asyncio.Lock()
        self._current: ProjectContext = GLOBAL
        # UI workspaces are simultaneous while the historical REPL surface remains process
        # scoped.  During an attended execution, tools resolve this immutable session binding
        # instead of whichever process-global project was selected most recently.
        self._execution_contexts: dict[int, ProjectContext] = {}

    def bind_execution_context(self, context: ExecutionContext, project: ProjectContext) -> None:
        """Register the project context owned by one persisted attended session."""
        if context.project_id != project.project_id:
            raise ValueError("execution/project context mismatch")
        self._execution_contexts[context.session_id] = project

    async def activate(self, project_id: int | None) -> ProjectContext:
        """Make ``project_id`` the active scope (None == global). Raises ``KeyError`` if the
        project doesn't exist or isn't active — a switch must never silently fall back to a
        different scope than asked for."""
        if project_id is None:
            self._current = GLOBAL
            return self._current
        project = await self.store.get(project_id)
        if project is None:
            raise KeyError(f"no project #{project_id}")
        if project.status == "archived":
            raise KeyError(f"project #{project_id} is archived")
        self._current = build_project_context(project)
        return self._current

    async def refresh_project_context(self, project_id: int) -> ProjectContext:
        """Rebuild every live context for a metadata-only project update.

        Unlike a scope switch, this preserves session ids and pending transcript state: the new
        name/description/repos apply on the next turn while the project id remains unchanged.
        """
        project = await self.store.get(project_id)
        if project is None:
            raise KeyError(f"no project #{project_id}")
        return self.apply_project_context(project)

    def apply_project_context(self, project: Project) -> ProjectContext:
        """Synchronously publish one already-loaded durable project snapshot to all caches."""
        refreshed = build_project_context(project)
        if self._current.project_id == project.id:
            self._current = refreshed
        for session_id, context in tuple(self._execution_contexts.items()):
            if context.project_id == project.id:
                self._execution_contexts[session_id] = refreshed
        return refreshed

    def project_services_are_current(
        self, project_id: int, services: list[str] | None
    ) -> bool:
        """Whether every cached context for ``project_id`` already has this policy.

        A lost-response retry may observe that the durable value is unchanged.  It is only safe
        to bypass the execution barrier in that case when no live process/session cache still
        carries the pre-commit policy; otherwise refreshing here could change tool authority in
        the middle of work that was admitted against the old context.
        """
        expected = None if services is None else tuple(sorted(set(services)))
        contexts = [self._current, *self._execution_contexts.values()]
        return all(
            context.services == expected
            for context in contexts
            if context.project_id == project_id
        )

    def current(self) -> ProjectContext:
        """The execution-local project when bound, otherwise the REPL's process scope."""
        execution = current_execution_context()
        if execution is not None:
            scoped = self._execution_contexts.get(execution.session_id)
            # Fail closed if a buggy caller binds a mismatched session: a global fallback could
            # make a tool query/write another workspace's active project.
            if scoped is not None and scoped.project_id == execution.project_id:
                return scoped
            return GLOBAL
        return self._current
