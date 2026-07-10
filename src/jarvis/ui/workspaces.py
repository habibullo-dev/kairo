"""Server-owned browser workspaces for the workstation UI.

The UI used to share one mutable ``UiSession`` and active ``ProjectService`` across every live
socket.  That made a transport-only event filter dishonest: selecting a project in one tab could
retag another tab's queued turn before it emitted.  A workspace is instead keyed by an opaque,
server-issued id bound to the authenticated UI cookie.  It owns its conversation, compaction
state, project context, and persisted :class:`~jarvis.core.execution.ExecutionContext`.

The browser may present a workspace id only as a routing handle.  The registry verifies that the
same authenticated cookie has a live WebSocket bound to it before exposing a workspace; it never
accepts browser-supplied session or project ids as authority.
"""

from __future__ import annotations

import asyncio
import secrets
from collections.abc import Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING

from jarvis.core.execution import ExecutionContext
from jarvis.projects.context import GLOBAL, ProjectContext, build_project_context

if TYPE_CHECKING:
    from jarvis.projects.service import ProjectService
    from jarvis.ui.connections import Connection, ConnectionManager
    from jarvis.ui.session import UiSession


WorkspaceSessionFactory = Callable[["UiWorkspace"], "UiSession"]
WorkspaceVoiceFactory = Callable[["UiWorkspace"], object]
ContextReplacedHook = Callable[[ExecutionContext], None]
ContextBusyCheck = Callable[[ExecutionContext], bool]
_KEEP_PROJECT = object()


@dataclass
class UiWorkspace:
    """One browser tab's isolated attended conversation and project scope."""

    owner_session: str
    workspace_id: str
    make_session: WorkspaceSessionFactory
    projects: ProjectService | None
    make_voice: WorkspaceVoiceFactory | None = None
    on_context_replaced: ContextReplacedHook | None = None
    context_busy: ContextBusyCheck | None = None

    def __post_init__(self) -> None:
        self.project: ProjectContext = GLOBAL
        self.voice_active = 0
        self.session = self.make_session(self)
        # Voice has mutable transcript/state too, so it belongs to the workspace rather than
        # the process.  None is a valid disabled/uncomposed voice surface.
        self.voice = self.make_voice(self) if self.make_voice is not None else None

    @property
    def context(self) -> ExecutionContext:
        session_id = self.session.session_id
        if session_id is None:
            raise RuntimeError("workspace has no persisted interactive session")
        return ExecutionContext(session_id=session_id, project_id=self.project.project_id)

    @property
    def attended_busy(self) -> bool:
        """Whether this workspace has admitted work that forbids a context transition."""
        return bool(
            self.session.busy
            or self.voice_active
            or (self.context_busy is not None and self.context_busy(self.context))
        )

    async def initialize(self) -> None:
        """Allocate the durable session before any socket can receive scoped work."""
        if await self.session.ensure_session() is None:
            raise RuntimeError("a UI workspace requires persistent session storage")
        self._bind_project_scope()

    def _bind_project_scope(self) -> None:
        """Register the workspace project for tools running under this execution context."""
        if self.projects is not None:
            self.projects.bind_execution_context(self.context, self.project)

    async def _project_context(self, project_id: int | None) -> ProjectContext:
        if project_id is None:
            return GLOBAL
        if self.projects is None:
            raise KeyError("projects unavailable")
        project = await self.projects.store.get(project_id)
        if project is None or project.status == "archived":
            raise KeyError(f"no project #{project_id}")
        return build_project_context(project)

    async def start_new_session(
        self, project_id: int | None | object = _KEEP_PROJECT
    ) -> ExecutionContext:
        """Start a fresh persisted chat, optionally under a newly selected project.

        A workspace refuses this transition while its turn is live.  That is intentional: the
        active task holds an immutable execution context, rather than being silently redirected.
        """
        if self.attended_busy:
            raise RuntimeError("busy")
        project = self.project
        if project_id is not _KEEP_PROJECT:
            project = await self._project_context(project_id)  # type: ignore[arg-type]
        old_context = self.context
        if self.on_context_replaced is not None:
            self.on_context_replaced(old_context)
        self.project = project
        self.session.start_new_session(self.project.project_id)
        await self.session.ensure_session()
        self._bind_project_scope()
        return self.context

    async def select_project(self, project_id: int | None) -> ExecutionContext:
        """Switch project by creating a new chat; existing transcripts remain immutable."""
        return await self.start_new_session(project_id)

    async def resume(self, session_id: int) -> bool:
        """Resume a persisted interactive chat and restore its durable project binding."""
        if self.attended_busy or self.session.sessions is None:
            return False
        if self.context_busy is not None and self.context_busy(self.context):
            return False
        meta = await self.session.sessions.get_meta(session_id)
        if meta is None or meta.kind != "interactive" or meta.archived:
            return False
        project = await self._project_context(meta.project_id)
        old_context = self.context
        if not await self.session.resume(session_id):
            return False
        if self.on_context_replaced is not None:
            self.on_context_replaced(old_context)
        self.project = project
        self._bind_project_scope()
        return True


class UiWorkspaceRegistry:
    """Attach live authenticated sockets to isolated :class:`UiWorkspace` instances."""

    def __init__(
        self,
        *,
        connections: ConnectionManager,
        make_session: WorkspaceSessionFactory,
        projects: ProjectService | None,
        make_voice: WorkspaceVoiceFactory | None = None,
        on_context_replaced: ContextReplacedHook | None = None,
        context_busy: ContextBusyCheck | None = None,
    ) -> None:
        self.connections = connections
        self.make_session = make_session
        self.projects = projects
        self.make_voice = make_voice
        self.on_context_replaced = on_context_replaced
        self.context_busy = context_busy
        self.transition_lock = asyncio.Lock()
        self._workspaces: dict[tuple[str, str], UiWorkspace] = {}

    @staticmethod
    def _valid_workspace_id(value: object) -> str | None:
        if not isinstance(value, str):
            return None
        # A server-issued URL-safe token is 32 chars here.  Treat arbitrary client strings as
        # invalid rather than creating attacker-selected handles.
        return value if 24 <= len(value) <= 128 else None

    async def attach(
        self, conn: Connection, *, owner_session: str, requested_workspace_id: object = None
    ) -> UiWorkspace:
        """Bind ``conn`` to an existing or freshly minted workspace for its auth cookie."""
        workspace_id = self._valid_workspace_id(requested_workspace_id)
        key = (owner_session, workspace_id) if workspace_id is not None else None
        workspace = self._workspaces.get(key) if key is not None else None
        if workspace is None:
            workspace_id = secrets.token_urlsafe(24)
            key = (owner_session, workspace_id)
            workspace = UiWorkspace(
                owner_session=owner_session,
                workspace_id=workspace_id,
                make_session=self.make_session,
                projects=self.projects,
                make_voice=self.make_voice,
                on_context_replaced=self.on_context_replaced,
                context_busy=self.context_busy,
            )
            await workspace.initialize()
            self._workspaces[key] = workspace
        self.connections.bind_workspace(
            conn,
            owner_session=owner_session,
            workspace_id=workspace.workspace_id,
            context=workspace.context,
        )
        return workspace

    def resolve(self, *, owner_session: str | None, workspace_id: object) -> UiWorkspace | None:
        """Resolve an HTTP request only when that cookie owns a live bound WebSocket."""
        if not owner_session:
            return None
        workspace_id = self._valid_workspace_id(workspace_id)
        if workspace_id is None:
            return None
        workspace = self._workspaces.get((owner_session, workspace_id))
        if workspace is None or not self.connections.has_live_workspace(
            owner_session=owner_session, workspace_id=workspace_id
        ):
            return None
        return workspace

    def refresh_context(self, workspace: UiWorkspace) -> None:
        """Retarget all current sockets after a new/resumed session or project selection."""
        self.connections.update_workspace_context(
            owner_session=workspace.owner_session,
            workspace_id=workspace.workspace_id,
            context=workspace.context,
        )

    def for_project(self, project_id: int) -> list[UiWorkspace]:
        """Return live registry workspaces currently bound to one project."""
        return [
            workspace
            for workspace in self._workspaces.values()
            if workspace.project.project_id == project_id
        ]

    def for_session(self, session_id: int) -> list[UiWorkspace]:
        """Return workspaces currently bound to one durable interactive session."""
        return [
            workspace
            for workspace in self._workspaces.values()
            if workspace.context.session_id == session_id
        ]

    def cancel_all(self) -> int:
        """Cancel every attended turn for the workstation emergency-stop route."""
        return sum(1 for workspace in self._workspaces.values() if workspace.session.cancel())

    async def publish_workspace(self, workspace: UiWorkspace, message: dict) -> None:
        """Emit a lifecycle event that introduces the workspace's replacement context."""
        await self.connections.publish_workspace(
            owner_session=workspace.owner_session,
            workspace_id=workspace.workspace_id,
            context=workspace.context,
            message=message,
        )

    @asynccontextmanager
    async def voice_activity(self, workspace: UiWorkspace):
        """Mark a voice/meeting command active without holding the transition lock for its run."""
        async with self.transition_lock:
            if workspace.attended_busy:
                raise RuntimeError("busy")
            workspace.voice_active += 1
        try:
            yield
        finally:
            async with self.transition_lock:
                workspace.voice_active -= 1
