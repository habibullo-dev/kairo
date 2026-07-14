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
from contextlib import asynccontextmanager, suppress
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

    def refresh_project_context(self, project: ProjectContext) -> bool:
        """Apply metadata refresh without changing the workspace's project or session binding."""
        if project.project_id != self.project.project_id:
            return False
        self.project = project
        self._bind_project_scope()
        return True

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


class ServerCaptureLease:
    """One exclusive lease over the workstation microphone's physical capture interval."""

    def __init__(
        self, registry: UiWorkspaceRegistry, source: object, *, meeting: bool = False
    ) -> None:
        self._registry = registry
        self._source = source
        self._meeting = meeting
        self._released = False
        self._capture_claimed = False
        self._capture_started = False
        self._capture_finished = False

    async def capture_utterance(self) -> bytes:
        # ``SoundDeviceCapture`` records in ``asyncio.to_thread``. Cancelling its await does not
        # stop that worker or close the InputStream, so never let request cancellation advertise
        # the physical microphone as free while the worker is still recording. Shield and drain
        # the bounded capture (silence or 30 seconds), then propagate cancellation after release.
        if self._capture_claimed:
            raise RuntimeError("capture lease already used")
        self._capture_claimed = True
        capture = None
        cancelled = False
        activated = False
        try:
            # Admission and the privacy state change are synchronous with starting this one
            # bounded source call; WebSocket delivery happens independently and cannot delay it.
            await self._registry._activate_server_capture(self)
            activated = True
            capture = asyncio.create_task(
                self._source.capture_utterance()  # type: ignore[attr-defined]
            )
            while True:
                try:
                    audio = await asyncio.shield(capture)
                    break
                except asyncio.CancelledError:
                    if capture.cancelled():
                        raise
                    cancelled = True
                except Exception:
                    # Once the caller cancels, drain only to keep the physical lease honest.
                    # A later device/provider error must not replace that cancellation outcome.
                    if cancelled:
                        raise asyncio.CancelledError from None
                    raise
            if cancelled:
                raise asyncio.CancelledError
            return audio
        finally:
            if activated:
                self._capture_finished = True
                await asyncio.shield(self.release())
            else:
                # Cancellation while waiting to activate still owns the reservation. A second
                # caller is rejected above and can never release the first caller's lease.
                await asyncio.shield(self.release())

    async def release(self) -> None:
        """Release once; route-level finally blocks may safely call this again."""
        await self._registry._release_server_capture(self)


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
        # The workstation has one physical default microphone even though every browser tab owns
        # an isolated UiVoice/CaptureSource. Admission must therefore be process-wide, not merely
        # per workspace, or two tabs can both try to open the same device.
        self._server_capture_lease: ServerCaptureLease | None = None
        self._server_capture_active = False
        self._meeting_recording_epoch = secrets.token_urlsafe(12)
        self._meeting_recording_revision = 0
        self._meeting_recording_pushes: set[asyncio.Task] = set()
        # The physical microphone may become free while a prior note is still transcribing. Keep
        # the same durable receipt single-flight through persistence so a cross-tab retry cannot
        # record the same logical note twice during that pre-row window.
        self._active_meeting_receipts: set[str] = set()
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

    def drop_owner_session(self, owner_session: str) -> int:
        """Cancel and forget every workspace owned by one revoked browser session."""
        keys = [key for key in self._workspaces if key[0] == owner_session]
        for key in keys:
            workspace = self._workspaces.pop(key)
            workspace.session.cancel()
            if self.on_context_replaced is not None:
                self.on_context_replaced(workspace.context)
        return len(keys)

    def drop_all(self) -> int:
        """Cancel and forget all workspaces after owner-wide credential recovery."""
        owner_sessions = {owner_session for owner_session, _workspace_id in self._workspaces}
        return sum(self.drop_owner_session(owner_session) for owner_session in owner_sessions)

    async def publish_workspace(
        self,
        workspace: UiWorkspace,
        message: dict,
        *,
        context: ExecutionContext | None = None,
    ) -> None:
        """Emit a lifecycle event only to one workspace.

        Callers that schedule delivery after a state hook may provide the immutable context
        captured by that hook. This prevents a queued terminal event from being relabelled if
        the workspace starts a new session before the delivery task runs.
        """
        await self.connections.publish_workspace(
            owner_session=workspace.owner_session,
            workspace_id=workspace.workspace_id,
            context=context or workspace.context,
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

    @property
    def server_capture_active(self) -> bool:
        """Whether any server-side source currently owns the physical microphone interval."""
        return self._server_capture_active

    @property
    def meeting_recording_active(self) -> bool:
        """Whether that physical interval belongs to a meeting-note capture."""
        lease = self._server_capture_lease
        return bool(self._server_capture_active and lease is not None and lease._meeting)

    @property
    def meeting_recording_revision(self) -> int:
        return self._meeting_recording_revision

    @property
    def meeting_recording_epoch(self) -> str:
        return self._meeting_recording_epoch

    async def _deliver_meeting_recording(self, active: bool, revision: int) -> None:
        """Best-effort global delivery without letting one stalled socket block the device."""
        sends = {
            asyncio.create_task(
                self.connections.send(
                    connection,
                    {
                        "kind": "meeting_recording",
                        "active": active,
                        "epoch": self._meeting_recording_epoch,
                        "revision": revision,
                    },
                )
            )
            for connection in self.connections.live()
        }
        if not sends:
            return
        done, pending = await asyncio.wait(sends, timeout=0.5)
        for task in pending:
            task.cancel()
            task.add_done_callback(self._consume_push_result)
        for task in done:
            with suppress(BaseException):
                task.result()

    @staticmethod
    def _consume_push_result(task: asyncio.Task) -> None:
        with suppress(BaseException):
            task.result()

    def _queue_meeting_recording(self, active: bool, revision: int) -> None:
        task = asyncio.create_task(self._deliver_meeting_recording(active, revision))
        self._meeting_recording_pushes.add(task)
        task.add_done_callback(self._meeting_recording_pushes.discard)

    async def _activate_server_capture(self, lease: ServerCaptureLease) -> None:
        notification = None
        async with self.transition_lock:
            if self._server_capture_lease is not lease or lease._released:
                raise RuntimeError("busy")
            if lease._capture_started or self._server_capture_active:
                raise RuntimeError("capture lease already used")
            lease._capture_started = True
            self._server_capture_active = True
            if lease._meeting:
                self._meeting_recording_revision += 1
                notification = (True, self._meeting_recording_revision)
        if notification is not None:
            self._queue_meeting_recording(*notification)

    async def _release_server_capture(self, lease: ServerCaptureLease) -> None:
        notification = None
        async with self.transition_lock:
            if lease._released:
                return
            # An eager route-level cleanup must not advertise the device as free while the
            # single admitted source call is still draining. Its own finally block releases it.
            if lease._capture_started and not lease._capture_finished:
                return
            lease._released = True
            if self._server_capture_lease is not lease:
                return
            was_meeting = self._server_capture_active and lease._meeting
            self._server_capture_active = False
            self._server_capture_lease = None
            if was_meeting:
                self._meeting_recording_revision += 1
                notification = (False, self._meeting_recording_revision)
        if notification is not None:
            self._queue_meeting_recording(*notification)

    async def reserve_server_capture(
        self, source: object, *, meeting: bool = False
    ) -> ServerCaptureLease:
        """Reserve the physical microphone and return a self-releasing capture wrapper.

        The surrounding request still uses :meth:`voice_activity` to pin its workspace context.
        This separate lease ends as soon as ``capture_utterance`` returns, so transcription,
        embedding, receipt repair, and the subsequent model turn do not block another tab's mic.
        ``meeting`` controls only the global meeting-note privacy indicator; every lease remains
        exclusive over the same physical device.
        """
        async with self.transition_lock:
            if self._server_capture_lease is not None:
                raise RuntimeError("busy")
            lease = ServerCaptureLease(self, source, meeting=meeting)
            self._server_capture_lease = lease
            return lease

    @asynccontextmanager
    async def meeting_receipt_activity(self, receipt_key: str):
        """Keep one scoped meeting receipt single-flight through durable persistence."""
        async with self.transition_lock:
            if receipt_key in self._active_meeting_receipts:
                raise RuntimeError("busy")
            self._active_meeting_receipts.add(receipt_key)
        try:
            yield
        finally:
            async with self.transition_lock:
                self._active_meeting_receipts.discard(receipt_key)
