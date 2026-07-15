"""Server-owned browser workspaces for the workstation UI.

The UI used to share one mutable ``UiSession`` and active ``ProjectService`` across every live
socket.  That made a transport-only event filter dishonest: selecting a project in one tab could
retag another tab's queued turn before it emitted.  A workspace is instead keyed by an opaque,
server-issued id bound to the authenticated UI cookie.  It owns its conversation, compaction
state, project context, and persisted :class:`~kira.core.execution.ExecutionContext`.

The browser may present a workspace id only as a routing handle.  The registry verifies that the
same authenticated cookie has a live WebSocket bound to it before exposing a workspace; it never
accepts browser-supplied session or project ids as authority.
"""

from __future__ import annotations

import asyncio
import secrets
from collections.abc import Callable
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from kira.core.execution import ExecutionContext
from kira.projects.context import GLOBAL, ProjectContext, build_project_context

if TYPE_CHECKING:
    from kira.projects.service import ProjectService
    from kira.ui.connections import Connection, ConnectionManager
    from kira.ui.session import PreparedUiSessionNew, PreparedUiSessionResume, UiSession


WorkspaceSessionFactory = Callable[["UiWorkspace"], "UiSession"]
WorkspaceVoiceFactory = Callable[["UiWorkspace"], object]
ContextReplacedHook = Callable[[ExecutionContext], None]
ContextBusyCheck = Callable[[ExecutionContext], bool]
_KEEP_PROJECT = object()


@dataclass(frozen=True)
class PreparedWorkspaceNewSession:
    """A fresh durable row plus the workspace authority snapshot that requested it."""

    source_context: ExecutionContext
    source_revision: int
    project: ProjectContext
    session_id: int


@dataclass(frozen=True)
class PreparedWorkspaceResume:
    """A fully loaded resume target that has not yet replaced live workspace state."""

    source_context: ExecutionContext
    source_revision: int
    project: ProjectContext
    session: PreparedUiSessionResume


@dataclass(frozen=True)
class PreparedWorkspaceContextReplacement:
    """Fallible context cleanup completed while old workspace authority remains live."""

    source_context: ExecutionContext
    source_revision: int
    session: PreparedUiSessionNew


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
        # Monotonic identity for the *selection epoch*, not just its session/project pair. A
        # workspace can move A -> B -> resume A; delayed A requests must not become current again.
        self.context_revision = 1
        self.revoked = False
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

    def _source_matches(self, context: ExecutionContext, revision: int) -> bool:
        return bool(
            not self.revoked and self.context == context and self.context_revision == revision
        )

    def _attended_busy_now(self) -> bool:
        """Re-read attended state across awaits; another request may admit work meanwhile."""
        return self.attended_busy

    async def prepare_new_session(
        self, project_id: int | None | object = _KEEP_PROJECT
    ) -> PreparedWorkspaceNewSession:
        """Resolve the target and allocate its row without changing browser-visible authority."""
        if self.revoked:
            raise RuntimeError("workspace_revoked")
        if self._attended_busy_now():
            raise RuntimeError("busy")
        source_context = self.context
        source_revision = self.context_revision
        project = self.project
        if project_id is not _KEEP_PROJECT:
            project = await self._project_context(project_id)  # type: ignore[arg-type]
        if not self._source_matches(source_context, source_revision):
            raise RuntimeError("context_changed")
        if self._attended_busy_now():
            raise RuntimeError("busy")
        session_id = await self.session.allocate_session(project.project_id)
        if session_id is None:
            raise RuntimeError("a UI workspace requires persistent session storage")
        if not self._source_matches(source_context, source_revision):
            raise RuntimeError("context_changed")
        if self._attended_busy_now():
            raise RuntimeError("busy")
        return PreparedWorkspaceNewSession(
            source_context=source_context,
            source_revision=source_revision,
            project=project,
            session_id=session_id,
        )

    async def refresh_prepared_new_session(
        self, prepared: PreparedWorkspaceNewSession
    ) -> PreparedWorkspaceNewSession:
        """Reload a prepared target immediately before its serialized commit.

        Preparation deliberately performs fallible I/O outside the registry transition lock.
        A project policy edit can therefore land while the durable session row is being
        allocated.  Callers holding that lock use this final reload so the delayed transition
        cannot reinstall the old immutable :class:`ProjectContext` snapshot.
        """
        if not self._source_matches(prepared.source_context, prepared.source_revision):
            raise RuntimeError("context_changed")
        project = await self._project_context(prepared.project.project_id)
        if not self._source_matches(prepared.source_context, prepared.source_revision):
            raise RuntimeError("context_changed")
        if self._attended_busy_now():
            raise RuntimeError("busy")
        return replace(prepared, project=project)

    def commit_new_session(self, prepared: PreparedWorkspaceNewSession) -> ExecutionContext:
        """Publish one prepared fresh-session transition as a non-yielding state change."""
        if not self._source_matches(prepared.source_context, prepared.source_revision):
            raise RuntimeError("context_changed")
        if self._attended_busy_now():
            raise RuntimeError("busy")
        replacement = self.prepare_context_replacement()
        if (
            replacement.source_context != prepared.source_context
            or replacement.source_revision != prepared.source_revision
        ):
            self.rollback_context_replacement(replacement)
            raise RuntimeError("context_changed")
        try:
            return self.commit_preallocated_new_session(
                replacement,
                project=prepared.project,
                session_id=prepared.session_id,
            )
        except Exception:
            self.rollback_context_replacement(replacement)
            raise

    def prepare_context_replacement(self) -> PreparedWorkspaceContextReplacement:
        """Preflight a fresh context without awaiting, allocating, or changing its identity."""
        if self.revoked:
            raise RuntimeError("workspace_revoked")
        if self._attended_busy_now():
            raise RuntimeError("busy")
        source_context = self.context
        source_revision = self.context_revision
        on_context_replaced = self.on_context_replaced
        prepared_session = self.session.prepare_new_session_commit(
            before_commit=(
                (lambda: on_context_replaced(source_context))
                if on_context_replaced is not None
                else None
            ),
        )
        return PreparedWorkspaceContextReplacement(
            source_context=source_context,
            source_revision=source_revision,
            session=prepared_session,
        )

    def rollback_context_replacement(self, prepared: PreparedWorkspaceContextReplacement) -> None:
        """Restore in-memory compaction after a failed batch preflight/database transaction."""
        self.session.rollback_prepared_new_session(prepared.session)

    def commit_preallocated_new_session(
        self,
        prepared: PreparedWorkspaceContextReplacement,
        *,
        project: ProjectContext,
        session_id: int,
    ) -> ExecutionContext:
        """Commit a preflighted externally allocated session without any fallible await."""
        if not self._source_matches(prepared.source_context, prepared.source_revision):
            raise RuntimeError("context_changed")
        if self._attended_busy_now():
            raise RuntimeError("busy")
        new_context = ExecutionContext(session_id=session_id, project_id=project.project_id)
        if self.projects is not None:
            # Bind first: if a custom ProjectService rejects the context, no live session state has
            # changed and the caller can roll back the prepared compaction token.
            self.projects.bind_execution_context(new_context, project)
        self.session.commit_prepared_new_session(
            prepared.session,
            project_id=project.project_id,
            session_id=session_id,
        )
        self.project = project
        self.context_revision += 1
        return self.context

    async def start_new_session(
        self, project_id: int | None | object = _KEEP_PROJECT
    ) -> ExecutionContext:
        """Start a fresh persisted chat, optionally under a newly selected project.

        A workspace refuses this transition while its turn is live.  That is intentional: the
        active task holds an immutable execution context, rather than being silently redirected.
        """
        prepared = await self.prepare_new_session(project_id)
        return self.commit_new_session(prepared)

    async def select_project(self, project_id: int | None) -> ExecutionContext:
        """Switch project by creating a new chat; existing transcripts remain immutable."""
        return await self.start_new_session(project_id)

    async def prepare_resume(self, session_id: int) -> PreparedWorkspaceResume | None:
        """Load a resume target while leaving the current workspace context untouched."""
        if self.revoked or self._attended_busy_now() or self.session.sessions is None:
            return None
        source_context = self.context
        source_revision = self.context_revision
        prepared_session = await self.session.prepare_resume(session_id)
        if prepared_session is None:
            return None
        if not self._source_matches(source_context, source_revision):
            raise RuntimeError("context_changed")
        if self._attended_busy_now():
            return None
        project = await self._project_context(prepared_session.project_id)
        if not self._source_matches(source_context, source_revision):
            raise RuntimeError("context_changed")
        if self._attended_busy_now():
            return None
        return PreparedWorkspaceResume(
            source_context=source_context,
            source_revision=source_revision,
            project=project,
            session=prepared_session,
        )

    async def refresh_prepared_resume(
        self, prepared: PreparedWorkspaceResume
    ) -> PreparedWorkspaceResume:
        """Reload a resume target under the registry transition lock before commit."""
        if not self._source_matches(prepared.source_context, prepared.source_revision):
            raise RuntimeError("context_changed")
        project = await self._project_context(prepared.project.project_id)
        if not self._source_matches(prepared.source_context, prepared.source_revision):
            raise RuntimeError("context_changed")
        if self._attended_busy_now():
            raise RuntimeError("busy")
        return replace(prepared, project=project)

    def commit_resume(self, prepared: PreparedWorkspaceResume) -> bool:
        """Publish a prepared resume as one non-yielding session/project transition."""
        if not self._source_matches(prepared.source_context, prepared.source_revision):
            raise RuntimeError("context_changed")
        if self._attended_busy_now():
            return False
        on_context_replaced = self.on_context_replaced
        if not self.session.commit_resume(
            prepared.session,
            before_commit=(
                (lambda: on_context_replaced(prepared.source_context))
                if on_context_replaced is not None
                else None
            ),
        ):
            return False
        self.project = prepared.project
        self._bind_project_scope()
        self.context_revision += 1
        return True

    async def resume(self, session_id: int) -> bool:
        """Resume a persisted interactive chat and restore its durable project binding."""
        prepared = await self.prepare_resume(session_id)
        return prepared is not None and self.commit_resume(prepared)


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
        async with self.transition_lock:
            workspace_id = self._valid_workspace_id(requested_workspace_id)
            key = (owner_session, workspace_id) if workspace_id is not None else None
            workspace = self._workspaces.get(key) if key is not None else None
            if workspace is not None:
                self.connections.bind_workspace(
                    conn,
                    owner_session=owner_session,
                    workspace_id=workspace.workspace_id,
                    context=workspace.context,
                    context_revision=workspace.context_revision,
                )
                return workspace

        # Initializing a new workspace allocates through the process-wide turn lock.  Keep that
        # wait outside ``transition_lock`` so another workspace can resolve/cancel a Gate-paused
        # turn.  The candidate is invisible until the final non-yielding registration below.
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
        async with self.transition_lock:
            if key in self._workspaces:
                raise RuntimeError("workspace id collision")
            self.connections.bind_workspace(
                conn,
                owner_session=owner_session,
                workspace_id=workspace.workspace_id,
                context=workspace.context,
                context_revision=workspace.context_revision,
            )
            self._workspaces[key] = workspace
            return workspace

    def resolve(self, *, owner_session: str | None, workspace_id: object) -> UiWorkspace | None:
        """Resolve an HTTP request only when that cookie owns a live bound WebSocket."""
        if not owner_session:
            return None
        workspace_id = self._valid_workspace_id(workspace_id)
        if workspace_id is None:
            return None
        workspace = self._workspaces.get((owner_session, workspace_id))
        if workspace is None or not self.is_live(workspace):
            return None
        return workspace

    def is_live(self, workspace: UiWorkspace) -> bool:
        """Whether this exact object is still registered and watched by an authenticated socket."""
        return bool(
            not workspace.revoked
            and self._workspaces.get((workspace.owner_session, workspace.workspace_id)) is workspace
            and self.connections.has_live_workspace(
                owner_session=workspace.owner_session, workspace_id=workspace.workspace_id
            )
        )

    def claim_matches(
        self, workspace: UiWorkspace, context: ExecutionContext, context_revision: int
    ) -> bool:
        """Validate one immutable client freshness claim against live server authority."""
        return bool(
            self.is_live(workspace)
            and workspace.context == context
            and workspace.context_revision == context_revision
        )

    def refresh_context(self, workspace: UiWorkspace) -> None:
        """Retarget all current sockets after a new/resumed session or project selection."""
        self.connections.update_workspace_context(
            owner_session=workspace.owner_session,
            workspace_id=workspace.workspace_id,
            context=workspace.context,
            context_revision=workspace.context_revision,
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

    async def cancel_all_and_wait(self) -> int:
        """Cancel every exact live turn first, then drain their durable settlement together."""
        targets: list[tuple[UiSession, asyncio.Task]] = []
        for workspace in self._workspaces.values():
            task = workspace.session.current_task
            if task is not None:
                targets.append((workspace.session, task))
        cancelled_turns = sum(1 for session, _task in targets if session.cancel())
        if targets:
            # A disconnected HTTP waiter must not become a second cancellation source for the
            # exact turn tasks whose durable cleanup this drain owns. Await every snapshot,
            # including a turn whose non-cancellable terminal save had already begun.
            await asyncio.shield(
                asyncio.gather(*(task for _session, task in targets), return_exceptions=True)
            )
        return cancelled_turns

    @property
    def global_turn_busy(self) -> bool:
        """Whether any registered browser workspace still owns an attended turn task."""
        return any(workspace.session.busy for workspace in self._workspaces.values())

    def drop_owner_session(self, owner_session: str) -> int:
        """Cancel and forget every workspace owned by one revoked browser session."""
        keys = [key for key in self._workspaces if key[0] == owner_session]
        for key in keys:
            workspace = self._workspaces.pop(key)
            workspace.revoked = True
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
        context_revision: int | None = None,
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
            message={
                **message,
                "context_revision": (
                    context_revision if context_revision is not None else workspace.context_revision
                ),
            },
        )

    @asynccontextmanager
    async def voice_activity(
        self,
        workspace: UiWorkspace,
        *,
        expected_context: ExecutionContext | None = None,
        expected_revision: int | None = None,
    ):
        """Mark a voice/meeting command active without holding the transition lock for its run."""
        async with self.transition_lock:
            if expected_context is not None and (
                expected_revision is None
                or not self.claim_matches(workspace, expected_context, expected_revision)
            ):
                raise RuntimeError("context_changed")
            if expected_context is None and not self.is_live(workspace):
                raise RuntimeError("context_changed")
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
