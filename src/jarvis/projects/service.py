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

from jarvis.projects.context import GLOBAL, ProjectContext, build_project_context
from jarvis.projects.store import ProjectStore


class ProjectService:
    def __init__(self, store: ProjectStore) -> None:
        self.store = store
        self._current: ProjectContext = GLOBAL

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

    def current(self) -> ProjectContext:
        """The active context — the callable the AgentLoop reads once per turn."""
        return self._current
