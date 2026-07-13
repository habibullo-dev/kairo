"""FastAPI app for the workstation UI (Phase 8) — the safety core.

Every response carries hardening headers; the token-exchange route mints a session and
redirects to a CLEAN url (no token in history); the WebSocket authenticates in-endpoint
(HTTP middleware does not run for WS). There is deliberately **no CORS middleware** — no
route ever emits an ``Access-Control-Allow-*`` header (ADR-0008 §2).

Task 2 ships the auth/transport core: token exchange, session enforcement, Host/Origin
guards, headers, ``/api/health``, and the WS hello/heartbeat/surface lifecycle. Turns,
approvals, read models, and the frontend land in later tasks against this floor.
"""

from __future__ import annotations

import contextlib
from pathlib import Path
from typing import TYPE_CHECKING

from fastapi import FastAPI, Request, WebSocket, status
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from starlette.websockets import WebSocketDisconnect

from jarvis.core.execution import ExecutionContext, bind_execution_context
from jarvis.graph.builder import rebuild as rebuild_graph
from jarvis.graph.index import CostAwareEmbedder
from jarvis.graph.review import approve as graph_approve
from jarvis.graph.review import reject as graph_reject
from jarvis.graph.search import unified_search
from jarvis.graph.service import node_card, subgraph, suggestions_view
from jarvis.observability import get_logger
from jarvis.observability.cost import load_pricing
from jarvis.permissions import PermissionGate, load_policy
from jarvis.permissions.modes import Mode
from jarvis.persistence.artifacts import ArtifactPathError
from jarvis.routing import RoutingMode
from jarvis.scheduler.verification import VerificationContract
from jarvis.search import search as _federated_search
from jarvis.tools import Permission
from jarvis.ui.approver import (
    ApprovalManager,
    ParkedTaskApprovalManager,
    UIApprover,
    UIScreenApprover,
)
from jarvis.ui.auth import SESSION_COOKIE, AuthManager, host_allowed, origin_allowed
from jarvis.ui.connections import Connection, ConnectionManager
from jarvis.ui.gate_api import policy_snapshot, read_today_audit
from jarvis.ui.readmodels import (
    UiServices,
    activity_feed,
    artifacts_list,
    capability_truth,
    connector_write_history,
    costs_overview,
    daily_overview,
    hub_status,
    interactive_models,
    lab_overview,
    list_agent_runs,
    list_memories,
    list_sessions_view,
    list_tasks,
    model_routes_status,
    office_overview,
    orchestration_estimate_accuracy,
    orchestration_outcome_accounting,
    orchestration_roi,
    orchestration_run_detail,
    orchestration_runs_view,
    projects_overview,
    projects_view,
    providers_status,
    serialize_artifact,
    serialize_chat_file,
    services_status,
    session_transcript,
    settings_overview,
    task_runs,
    teams_catalog,
    vault_lint,
    vault_overview,
    workflows_catalog,
    workspace_overview,
)

if TYPE_CHECKING:
    from jarvis.config import Config

#: Methods that mutate state — Origin-checked (anti-CSRF). GETs are session-gated instead.
_MUTATING = frozenset({"POST", "PUT", "PATCH", "DELETE"})

#: Valid memory types for the human-authority remember route (matches the store CHECK).
_MEMORY_TYPES = frozenset({"fact", "preference", "project", "episode"})

#: Paths reachable WITHOUT a session. The exchange mints the session; health is safe.
#: Everything else — including static app assets and data GETs — requires the session cookie
#: (the authenticated browser has it after the exchange; an anonymous fetch gets 401).
_OPEN_PATHS = frozenset({"/api/health"})

#: Hand-written frontend assets (no build step, no CDN) served from here.
STATIC_DIR = Path(__file__).parent / "static"

#: Browser-provided only as an opaque routing handle.  The server resolves it against the
#: authenticated cookie and a currently-live WebSocket before it can select a UI workspace.
WORKSPACE_HEADER = "x-kairo-workspace-id"

#: Media types the artifact content route will serve — TEXT + IMAGES ONLY. No html/svg/js
#: (script-injection surface); anything else is refused (415). nosniff + CSP are applied on the
#: way out, so a served body can never be sniffed into an executable type.
_ARTIFACT_MEDIA: dict[str, str] = {
    ".md": "text/markdown; charset=utf-8",
    ".markdown": "text/markdown; charset=utf-8",
    ".txt": "text/plain; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}


def _security_headers() -> dict[str, str]:
    """Hardening headers applied to every response. Strict CSP (self-only, no inline/eval),
    no-referrer, no framing — and, by omission, NO CORS headers ever."""
    return {
        "Content-Security-Policy": (
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
            "connect-src 'self'; object-src 'none'; base-uri 'self'; form-action 'self'; "
            "frame-ancestors 'none'"
        ),
        "Referrer-Policy": "no-referrer",
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY",
        "Cross-Origin-Opener-Policy": "same-origin",
    }


def _secure(resp: Response, *, no_store: bool = False) -> Response:
    for key, value in _security_headers().items():
        resp.headers.setdefault(key, value)
    if no_store:
        resp.headers["Cache-Control"] = "no-store"
    return resp


def _deny(code: int, message: str) -> Response:
    """A hardened plain-text error (headers applied so the sweep covers error paths too)."""
    return _secure(Response(content=message, status_code=code, media_type="text/plain"))


def _unavailable(service: str) -> JSONResponse:
    """503 for a screen whose backing service wasn't composed (e.g. memory off, or a bare
    app in a test). The auth core still serves; the screen renders 'unavailable'."""
    return JSONResponse({"ok": False, "message": f"{service} unavailable"}, status_code=503)


def _workspace_required() -> JSONResponse:
    """Refuse a context-sensitive request before its authenticated socket is bound."""
    return JSONResponse(
        {"ok": False, "message": "browser workspace is not connected"}, status_code=409
    )


def _runner_status(
    runner: object | None, session: object | None, *, reveal_in_flight: bool = True
) -> dict:
    """The status-bar view: is the background runner firing, what job is in flight, and is
    an interactive turn running. Read-only; the emergency stop toggles the first two."""
    return {
        "runner_running": bool(runner is not None and runner.is_running),
        "in_flight": (
            getattr(runner, "in_flight", None) if runner is not None and reveal_in_flight else None
        ),
        "turn_busy": bool(session is not None and session.busy),
    }


def create_app(
    config: Config,
    *,
    auth: AuthManager | None = None,
    connections: ConnectionManager | None = None,
    gate: PermissionGate | None = None,
    session: object | None = None,
    runner: object | None = None,
    services: UiServices | None = None,
    voice: object | None = None,
) -> FastAPI:
    """Build the workstation app. ``auth``/``connections``/``gate`` are injectable so tests can
    supply a known token / fake clock and the CLI host (Task 9) can share the REPL's gate;
    ``session`` (a ``UiSession``), ``runner`` (a ``BackgroundRunner``), and ``services`` (the
    read/mutate bundle) are composed by the host — when absent, the dependent routes report
    503 (the auth core still serves)."""
    auth = auth or AuthManager()
    connections = connections or ConnectionManager(heartbeat_seconds=config.ui.heartbeat_seconds)
    approvals = ApprovalManager(connections)
    # One gate — shared by the policy read model, the UIApprover's narrow-persist, and the
    # AgentLoop — so a UI "always" and a later turn (and a child) see the same rules.
    policy_path = config.root / "config" / "permissions.yaml"
    gate = gate or PermissionGate(load_policy(policy_path), config.root, source_path=policy_path)
    log = get_logger("jarvis.ui")
    app = FastAPI(title="Kairo Workstation", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.auth = auth
    app.state.connections = connections
    app.state.approvals = approvals
    # Durable parked runs are still resumed only by a host-composed callback.  This manager
    # contains the private local-UI projection plus live-WebSocket nonces; it never claims a
    # task-run row or executes a tool itself.
    app.state.parked_task_approvals = ParkedTaskApprovalManager(connections)
    app.state.resume_parked = None  # async (run_id: int, resolution: str) -> bool; host-wired
    app.state.gate = gate
    app.state.ui_approver = UIApprover(approvals, gate, config)
    app.state.session = session
    app.state.runner = runner
    app.state.services = services or UiServices()
    app.state.voice = voice
    app.state.notices = None  # a NoticeBoard, set by the CLI host (run_ui); None ⇒ empty tail
    app.state.run_digest_now = None  # async () -> DigestOutcome, set by run_ui; None ⇒ 503
    app.state.projects = None  # a ProjectService, set by build_ui_app; None ⇒ projects 503
    app.state.modes = None  # a ModeState, set by build_ui_app; None ⇒ mode reads 'approval'
    # Phase 15.5: the interactive model selector state, set by build_ui_app; None ⇒ the config
    # default model + a picker whose current is that default (no runtime override).
    app.state.interactive_models = None
    # Set by ``build_ui_app``.  Bare ``create_app`` tests retain the legacy injectable session
    # path; production UI routes use only a live, server-owned workspace from this registry.
    app.state.workspaces = None
    app.state.orchestrator = None  # an OrchestrationController, set by build_ui_app; None ⇒ 503
    # The UI is voice's fail-closed "screen": a VoiceApprover wired to this UIScreenApprover
    # resolves risky voice actions on the authenticated, live, watching Gate surface — or
    # denies. Composed here so the CLI host (Task 9) injects it into the voice VoiceApprover.
    app.state.ui_screen = UIScreenApprover(approvals, connections)
    app.state.config = config

    def _workspace_for(request: Request):
        registry = app.state.workspaces
        if registry is None:
            return None
        return registry.resolve(
            owner_session=request.cookies.get(SESSION_COOKIE),
            workspace_id=request.headers.get(WORKSPACE_HEADER),
        )

    def _session_in_workspace(meta, workspace) -> bool:
        """A session metadata mutation/read stays inside its live workspace project.

        Resume is intentionally handled separately because it performs the atomic project+session
        transition. Every other session endpoint is a same-context operation.
        """
        return meta is not None and (
            workspace is None or meta.project_id == workspace.context.project_id
        )

    def _workspace_can_access_project(project_id: int | None, workspace) -> bool:
        """Whether a row belongs to the caller's live workspace view.

        A project workspace may use only its own project rows. The global workspace intentionally
        remains the aggregate administrative view, matching the existing search and attention
        projections; it is still selected only through a live, authenticated workspace handle.
        """
        return (
            workspace is None
            or workspace.context.project_id is None
            or project_id is None
            or project_id == workspace.context.project_id
        )

    def _chat_scope(request: Request) -> ExecutionContext | None:
        """Return the one exact context allowed to populate a chat's context shelf.

        Browser workspaces are authoritative.  The legacy single-session composition remains
        usable for focused tests and the CLI host, but it still has to name a real session before
        chat-scoped files can be read.
        """
        workspace = _workspace_for(request)
        if workspace is not None:
            return workspace.context
        if app.state.workspaces is not None:
            return None
        session = app.state.session
        if session is None or session.session_id is None:
            return None
        return ExecutionContext(session_id=session.session_id, project_id=session.project_id)

    def _chat_output_download(store, artifact) -> Response:
        """Serve an already-registered output as an attachment after exact scope checking.

        This is intentionally separate from the preview-only artifact content route.  It permits
        common document output types as *downloads*, never rendered HTML/SVG/JS, and keeps all
        path confinement, sensitivity, and size checks in the ArtifactStore boundary.
        """
        if artifact is None or artifact.sensitivity == "quarantined":
            return _deny(status.HTTP_404_NOT_FOUND, "not found")
        try:
            path = store.content_path(artifact)
        except ArtifactPathError:
            return _deny(status.HTTP_404_NOT_FOUND, "not found")
        if path is None or not path.is_file():
            return _deny(status.HTTP_404_NOT_FOUND, "not found")
        media_type = {
            **_ARTIFACT_MEDIA,
            ".pdf": "application/pdf",
            ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
            ".csv": "text/csv; charset=utf-8",
        }.get(path.suffix.lower())
        if media_type is None:
            return _deny(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, "unsupported type")
        if path.stat().st_size > config.limits.max_read_bytes:
            return _deny(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "too large")
        suffix = path.suffix.lower()
        if artifact.title.endswith(suffix):
            safe_name = artifact.title
        else:
            safe_name = f"{artifact.title}{suffix}"
        return FileResponse(path, media_type=media_type, filename=safe_name)

    @app.middleware("http")
    async def guard(request: Request, call_next):  # noqa: ANN001,ANN202 - framework signature
        # 1. Host allowlist FIRST — anti DNS-rebinding (a rebound name still sends its Host).
        if not host_allowed(request.headers.get("host", "")):
            return _deny(status.HTTP_400_BAD_REQUEST, "bad host")
        # 2. Origin check on mutations — anti-CSRF (a cross-site POST carries a foreign Origin).
        if request.method in _MUTATING and not origin_allowed(
            request.headers.get("origin", ""),
            host_header=request.headers.get("host", ""),
            scheme=request.url.scheme,
        ):
            return _deny(status.HTTP_403_FORBIDDEN, "bad origin")
        # 3. Token exchange at `/?token=…`: mint a session, redirect CLEAN (no token in the
        #    served url / history), no-store. The token is never echoed back.
        token = request.query_params.get("token")
        if token is not None and request.url.path == "/":
            if not auth.check_token(token):
                return _deny(status.HTTP_401_UNAUTHORIZED, "bad token")
            sid = auth.mint_session()
            resp = RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)
            # secure=False: loopback is http, so a Secure cookie would never be sent back.
            resp.set_cookie(SESSION_COOKIE, sid, httponly=True, samesite="strict", secure=False)
            log.info("ui_session_minted")  # note: the token is NOT logged
            return _secure(resp, no_store=True)
        # 4. Session required everywhere except the open paths.
        if request.url.path not in _OPEN_PATHS and not auth.is_valid_session(
            request.cookies.get(SESSION_COOKIE)
        ):
            return _deny(status.HTTP_401_UNAUTHORIZED, "authentication required")
        resp = await call_next(request)
        return _secure(resp, no_store=request.url.path.startswith("/api"))

    @app.get("/api/health")
    async def health() -> dict:
        return {"status": "ok", "app": "kairo"}

    @app.get("/")
    async def root() -> Response:
        # The workstation shell (guard already enforced the session). Falls back to a note if
        # assets are somehow missing, rather than 500.
        index = STATIC_DIR / "index.html"
        if index.is_file():
            # no-cache: the browser MUST revalidate (ETag) so a JS/HTML update is picked up on the
            # next load rather than served stale — a local app has no CDN, revalidation is cheap.
            return FileResponse(index, headers={"Cache-Control": "no-cache"})
        return Response("Kairo Workstation — assets missing.", media_type="text/plain")

    @app.get("/static/{path:path}")
    async def static_asset(path: str) -> Response:
        # Serve a hand-written asset, guarding against path traversal (must resolve inside
        # STATIC_DIR). Session already enforced by the guard; CSP added on the way out.
        target = (STATIC_DIR / path).resolve()
        if target != STATIC_DIR and STATIC_DIR not in target.parents:
            return _deny(status.HTTP_404_NOT_FOUND, "not found")
        if not target.is_file():
            return _deny(status.HTTP_404_NOT_FOUND, "not found")
        # no-cache (revalidate via ETag) so an updated module is never served stale from disk cache.
        return FileResponse(target, headers={"Cache-Control": "no-cache"})

    # --- Gate: approvals (the crown jewels) + read-only policy/audit views -------------

    @app.get("/api/approvals")
    async def list_approvals(request: Request) -> JSONResponse:
        workspace = _workspace_for(request)
        if app.state.workspaces is not None and workspace is None:
            return _workspace_required()
        if workspace is None:
            # Bare test/legacy composition is intentionally empty rather than exposing every
            # pending payload without a server-bound execution context.
            return JSONResponse({"pending": []})
        return JSONResponse(
            {"pending": [p.to_public() for p in approvals.pending_for(workspace.context)]}
        )

    @app.post("/api/approvals/{decision_id}/resolve")
    async def resolve_approval(decision_id: str, request: Request) -> JSONResponse:
        # Session + loopback-Origin already enforced by the guard (POST is mutating). The
        # nonce (single-use, bound to a live watching client) is the replay-proof credential.
        workspace = _workspace_for(request)
        if app.state.workspaces is not None and workspace is None:
            return _workspace_required()
        body = await request.json()
        ok, message = approvals.resolve(
            decision_id,
            str(body.get("nonce", "")),
            str(body.get("action", "")),
            context=workspace.context if workspace is not None else None,
        )
        return JSONResponse({"ok": ok, "message": message}, status_code=200 if ok else 409)

    @app.post("/api/parked-task-approvals/{run_id}/resolve")
    async def resolve_parked_task_approval(run_id: int, request: Request) -> JSONResponse:
        """Send one visible local review to the host-owned parked-run resume seam.

        The browser cannot call ``TaskStore.claim_parked_approval``.  Before a nonce is consumed,
        re-read the exact task/run and compare its verified continuation to the manager's private
        projection.  This makes an observed numeric run id neither a cross-project read nor a
        replay capability.
        """
        workspace = _workspace_for(request)
        if app.state.workspaces is None or workspace is None:
            # Parked work has no attended session provenance.  It is reviewable only through a
            # live server-owned browser workspace, never the legacy unscoped test/session path.
            return _workspace_required()
        resume_parked = app.state.resume_parked
        if not callable(resume_parked):
            return _unavailable("parked task approval")
        svc = app.state.services.tasks
        if svc is None:
            return _unavailable("tasks")
        body = await request.json()
        pending = app.state.parked_task_approvals.visible_to(run_id, workspace.context)
        if pending is None:
            return _deny(status.HTTP_404_NOT_FOUND, "not found")
        task = await svc.store.get(pending.task_id)
        if task is None or not _workspace_can_access_project(task.project_id, workspace):
            return _deny(status.HTTP_404_NOT_FOUND, "not found")
        # ``runs_for`` is ordered newest first.  A parked run remains running/inert, so it is
        # normally the newest row; the larger bound avoids treating a valid long-lived task as a
        # wildcard while still keeping a browser-triggered verification bounded.
        rows = await svc.store.runs_for(task.id, limit=200)
        run = next((row for row in rows if row.id == run_id), None)
        if (
            run is None
            or run.status != "running"
            or run.approval_state != "pending"
            or run.continuation is None
            or run.continuation.tool_id != pending.tool_id
            or run.continuation.tool_name != pending.tool_name
            or run.continuation.tool_input != pending.tool_input
            or run.continuation.tool_input_hash != pending.tool_input_hash
            or run.continuation.decision_reason != pending.decision_reason
        ):
            return JSONResponse(
                {"ok": False, "message": "parked task is no longer awaiting this review"},
                status_code=status.HTTP_409_CONFLICT,
            )
        reserved, message = app.state.parked_task_approvals.reserve(
            run_id,
            str(body.get("nonce", "")),
            str(body.get("action", "")),
            context=workspace.context,
        )
        if reserved is None:
            return JSONResponse({"ok": False, "message": message}, status_code=409)
        try:
            # This callback owns the durable one-time claim and the explicit resume/reject
            # handling.  ``True`` is its only success signal; no truthy object can accidentally
            # turn an approval into a committed transition.
            committed = (await resume_parked(run_id, str(body.get("action", "")))) is True
        except Exception:  # noqa: BLE001 - never leak task payload/provider details to the browser
            log.warning("parked_task_resume_failed", run_id=run_id)
            committed = False
        app.state.parked_task_approvals.complete(run_id, committed=committed)
        if not committed:
            return JSONResponse(
                {
                    "ok": False,
                    "message": "parked task could not be resolved; review it again before retrying",
                    "retry": True,
                },
                status_code=status.HTTP_409_CONFLICT,
            )
        return JSONResponse({"ok": True, "message": "parked task resolution accepted"})

    @app.get("/api/gate/policy")
    async def gate_policy() -> dict:
        return policy_snapshot(gate)

    @app.get("/api/audit/today")
    async def audit_today() -> dict:
        return {"events": read_today_audit(config.logs_dir)}

    # --- Command: submit / cancel a turn (events stream over the WS) --------------------

    @app.post("/api/turn")
    async def submit_turn(request: Request) -> JSONResponse:
        workspace = _workspace_for(request)
        if app.state.workspaces is not None and workspace is None:
            return _workspace_required()
        sess = workspace.session if workspace is not None else app.state.session
        if sess is None:
            return JSONResponse({"ok": False, "message": "no session"}, status_code=503)
        body = await request.json()
        text = str(body.get("text", "")).strip()
        if not text:
            return JSONResponse({"ok": False, "message": "empty"}, status_code=400)
        # A workspace always allocates before scheduling, so every emitted event has a real
        # session id.  The legacy injectable session retains its historical lazy behavior.
        if workspace is not None:
            async with app.state.workspaces.transition_lock:
                if workspace.voice_active:
                    started = False
                else:
                    await sess.ensure_session()
                    started = sess.submit(text)
        else:
            started = sess.submit(text)
        # 409 if a turn is already in flight (one interactive turn at a time, like the REPL).
        return JSONResponse({"ok": started}, status_code=200 if started else 409)

    @app.post("/api/turn/cancel")
    async def cancel_turn(request: Request) -> JSONResponse:
        workspace = _workspace_for(request)
        if app.state.workspaces is not None and workspace is None:
            return _workspace_required()
        sess = workspace.session if workspace is not None else app.state.session
        if sess is None:
            return JSONResponse({"cancelled": False})
        return JSONResponse({"cancelled": sess.cancel()})

    # --- emergency stop: existing brakes only (Ctrl-C parity + runner stop) -------------

    @app.get("/api/runner")
    async def runner_status(request: Request) -> JSONResponse:
        # The status-strip feed: runner/turn state + mode + active project + today's spend +
        # pending approvals + cost-ledger health (A5). One calm surface; all read-only.
        workspace = _workspace_for(request)
        if app.state.workspaces is not None and workspace is None:
            return _workspace_required()
        sess = workspace.session if workspace is not None else app.state.session
        status = _runner_status(app.state.runner, sess, reveal_in_flight=workspace is None)
        modes = app.state.modes
        status["mode"] = modes.current().value if modes is not None else "approval"
        projects = app.state.projects
        cur = (
            workspace.project
            if workspace is not None
            else (projects.current() if projects is not None else None)
        )
        status["project"] = {"id": cur.project_id, "name": cur.name} if cur is not None else None
        status["pending_approvals"] = (
            len(app.state.approvals.pending_for(workspace.context)) if workspace is not None else 0
        )
        budgets = app.state.services.budgets
        status["today_spend_usd"] = (
            (await budgets.period_spend("day"))["cost_usd"] if budgets is not None else None
        )
        # The global Cost Center total remains available above, while the attended workspace can
        # show its own project context without borrowing another project's spend.
        status["project_today_spend_usd"] = None
        status["project_month_spend_usd"] = None
        status["project_month_budget_usd"] = None
        if budgets is not None and cur is not None:
            status["project_today_spend_usd"] = (
                await budgets.period_spend("day", project_id=cur.project_id)
            )["cost_usd"]
            status["project_month_spend_usd"] = (
                await budgets.period_spend("month", project_id=cur.project_id)
            )["cost_usd"]
            status["project_month_budget_usd"] = budgets.config.project_monthly_usd
        ledger = app.state.services.ledger
        status["ledger_degraded"] = ledger.status()["degraded"] if ledger is not None else False
        # Phase 15.5 conversation truth: the active session + the interactive model/effort, so the
        # client can rehydrate the transcript it is IN (fixes the "No messages yet" reload) and
        # render honest composer chips. session_title is looked up fresh (a rename must show).
        sstore = app.state.services.sessions
        status["session_id"] = sess.session_id if sess is not None else None
        status["session_title"] = None
        status["session_save_state"] = getattr(sess, "persistence_state", "new")
        status["session_created_at"] = None
        status["session_updated_at"] = None
        status["session_pinned"] = False
        status["chat_turn_budget_usd"] = getattr(
            sess, "turn_budget_usd", config.chat.hard_stop_usd_per_turn
        )
        status["last_turn_cost_usd"] = getattr(sess, "last_turn_cost_usd", None)
        status["last_turn_model"] = getattr(sess, "last_turn_model", None)
        status["last_turn_provider"] = getattr(sess, "last_turn_provider", None)
        if sess is not None and sess.session_id is not None and sstore is not None:
            meta = await sstore.get_meta(sess.session_id)
            if meta is not None:
                status["session_title"] = meta.title
                status["session_created_at"] = meta.created_at
                status["session_updated_at"] = meta.updated_at
                status["session_pinned"] = meta.pinned
        # model reflects the interactive override once Task 2 wires it; here it is the config
        # default (byte-identical to today). effort is the loop's output-config effort.
        models = getattr(app.state, "interactive_models", None)
        status["model"] = models.current() if models is not None else config.models.main
        # effort is the CURRENT model's per-model effort (the composer's cost selector); falls back
        # to the config default when the selector isn't wired.
        status["effort"] = models.current_effort() if models is not None else config.limits.effort
        # Phase 15.6: the routing policy (auto|manual) + what Auto last picked, so the header can
        # show "Auto → Sonnet 5" vs a pinned manual model.
        status["routing"] = _routing_policy()
        status["routed"] = _routed_dict()
        status["auto_may_classify"] = status["routing"] == "auto"
        return JSONResponse(status)

    @app.post("/api/mode")
    async def set_mode(request: Request) -> JSONResponse:
        # Set the interactive run mode (plan|approval|auto). Backend-enforced in the loop;
        # this only flips the surface state (and never affects background/voice). Debug is a
        # client-only flag, never a mode here.
        modes = app.state.modes
        if modes is None:
            return _unavailable("mode")
        body = await request.json()
        try:
            new_mode = Mode(str(body.get("mode", "")))
        except ValueError:
            return JSONResponse(
                {"ok": False, "message": "mode must be plan|approval|auto"}, status_code=400
            )
        modes.set(new_mode)
        log.info("mode_changed", mode=new_mode.value)
        # Announce so any open surface updates its chip (Task 9 adds the WS event type).
        await app.state.connections.broadcast({"kind": "mode_changed", "mode": new_mode.value})
        return JSONResponse({"ok": True, "mode": new_mode.value})

    @app.post("/api/model")
    async def set_model(request: Request) -> JSONResponse:
        # Phase 15.5/15.6: choose the interactive routing. "auto" ⇒ cost-aware Auto routing
        # (RoutingState=AUTO). A model id ⇒ MANUAL, pinned to that model — the Anthropic-only
        # allowlist (private-context pin) enforced in InteractiveModelState.set (else 400). UI-state
        # only: the loop reads it next turn; the ledger attributes model + routing_mode. Non-private
        # / text-only providers are NEVER a manual main-chat pick (they 400 via the allowlist).
        ims = app.state.interactive_models
        routing = getattr(app.state, "routing", None)
        if ims is None:
            return _unavailable("model selection")
        body = await request.json()
        choice = str(body.get("model", ""))
        if choice == "auto":
            if routing is not None:
                routing.set(RoutingMode.AUTO)
            log.info("interactive_routing_changed", policy="auto")
            await app.state.connections.broadcast({"kind": "model_changed", "model": "auto"})
            return JSONResponse({"ok": True, "model": "auto", "policy": "auto"})
        try:
            ims.set(choice)
        except ValueError as exc:
            return JSONResponse({"ok": False, "message": str(exc)}, status_code=400)
        if routing is not None:
            routing.set(RoutingMode.MANUAL)  # pinning a model switches OUT of Auto
        log.info("interactive_model_changed", model=ims.current(), policy="manual")
        await app.state.connections.broadcast({"kind": "model_changed", "model": ims.current()})
        return JSONResponse({"ok": True, "model": ims.current(), "policy": "manual"})

    @app.post("/api/effort")
    async def set_effort(request: Request) -> JSONResponse:
        # Phase 15.5: choose the output-config effort for a model (cost control). Lower effort ⇒
        # fewer output tokens ⇒ lower cost; higher ⇒ more thorough. Validated against VALID_EFFORTS
        # in InteractiveModelState.set_effort (a bad level/model is a 400). UI-state only: the loop
        # reads it next turn (frozen per turn) and the ledger records the requested effort. Effort
        # is remembered per model; omit `model` to set the current model's. No tool/executor/Gate.
        ims = app.state.interactive_models
        if ims is None:
            return _unavailable("effort selection")
        body = await request.json()
        model_id = body.get("model")
        try:
            ims.set_effort(
                str(body.get("effort", "")), model_id=str(model_id) if model_id else None
            )
        except ValueError as exc:
            return JSONResponse({"ok": False, "message": str(exc)}, status_code=400)
        log.info("interactive_effort_changed", model=ims.current(), effort=ims.current_effort())
        await app.state.connections.broadcast(
            {"kind": "effort_changed", "model": ims.current(), "effort": ims.current_effort()}
        )
        return JSONResponse({"ok": True, "effort": ims.current_effort(), "efforts": ims.efforts()})

    @app.post("/api/runner/pause")
    async def runner_pause(request: Request) -> JSONResponse:
        # Maps to BackgroundRunner.stop(): finish any in-flight job (never a torn write),
        # then stop firing. Also cancels the in-flight interactive turn. No new authority.
        workspace = _workspace_for(request)
        if app.state.workspaces is not None and workspace is None:
            return _workspace_required()
        if app.state.workspaces is not None:
            app.state.workspaces.cancel_all()
        elif app.state.session is not None:
            app.state.session.cancel()
        if app.state.runner is not None and app.state.runner.is_running:
            await app.state.runner.stop()
        sess = workspace.session if workspace is not None else app.state.session
        return JSONResponse(
            _runner_status(app.state.runner, sess, reveal_in_flight=workspace is None)
        )

    @app.post("/api/runner/resume")
    async def runner_resume(request: Request) -> JSONResponse:
        workspace = _workspace_for(request)
        if app.state.workspaces is not None and workspace is None:
            return _workspace_required()
        if app.state.runner is not None and not app.state.runner.is_running:
            app.state.runner.start()
        sess = workspace.session if workspace is not None else app.state.session
        return JSONResponse(
            _runner_status(app.state.runner, sess, reveal_in_flight=workspace is None)
        )

    @app.get("/api/notices")
    async def notices(request: Request) -> JSONResponse:
        # Background activity is process-local (not durable history), and its task payload/error
        # text belongs only to the matching server-owned workspace project.
        board = app.state.notices
        workspace = _workspace_for(request)
        if app.state.workspaces is not None and workspace is None:
            return _workspace_required()
        tail = (
            board.tail(50, project_id=workspace.context.project_id)
            if board is not None and workspace is not None
            else (board.tail(50) if board is not None else [])
        )
        return JSONResponse({"notices": tail})

    # --- read models: Hub / Lab (always available) ------------------------------------

    @app.get("/api/hub")
    async def hub(request: Request) -> JSONResponse:
        # Capability availability is workspace-specific: a connected provider is useful in chat
        # only when this workspace's loop actually registered its tool.  Hub must use the same
        # live workspace as Daily, the header, and the dedicated capabilities route.
        workspace = _workspace_for(request)
        if app.state.workspaces is not None and workspace is None:
            return _workspace_required()
        connectors = app.state.services.connectors
        ledger = app.state.services.ledger
        data = hub_status(
            config,
            connectors=connectors.status() if connectors is not None else None,
            ledger_status=ledger.status() if ledger is not None else None,
        )
        data["capabilities"] = _capabilities(workspace)  # Phase 15.5: the shared truth, embedded
        return JSONResponse(data)

    @app.get("/api/settings")
    async def settings_status(request: Request) -> JSONResponse:
        # Read-only settings policy surface (Phase 13). Presence/state/env-NAMES only — never a
        # key value or a token (covered by the secret-absence sweep). Mutates nothing; global
        # service flags stay YAML-only.
        workspace = _workspace_for(request)
        if app.state.workspaces is not None and workspace is None:
            return _workspace_required()
        connectors = app.state.services.connectors
        ledger = app.state.services.ledger
        data = settings_overview(
            config,
            connectors=connectors.status() if connectors is not None else None,
            ledger_status=ledger.status() if ledger is not None else None,
            policy=gate.policy,
        )
        data["capabilities"] = _capabilities(workspace)  # Phase 15.5: the shared truth, embedded
        return JSONResponse(data)

    @app.get("/api/models")
    async def models_list() -> dict:
        # Phase 15.5: the composer's model picker — selectable Anthropic models + honestly-disabled
        # externals with reasons. Read-only; presence/state only (secret-swept).
        ims = app.state.interactive_models
        return interactive_models(
            config,
            current=ims.current() if ims is not None else None,
            efforts=ims.efforts() if ims is not None else None,
            current_effort=ims.current_effort() if ims is not None else None,
            policy=_routing_policy(),
            routed=_routed_dict(),
        )

    def _routing_policy() -> str:
        # Phase 15.6: the interactive routing mode (auto|manual). Default 'manual' if the router
        # isn't wired (e.g. a bare app) so the surface never errors.
        routing = getattr(app.state, "routing", None)
        return routing.mode().value if routing is not None else "manual"

    def _routed_dict() -> dict | None:
        # The last Auto pick, so the composer can show "Auto → Sonnet 5 (reason)". Safe fields only
        # (model names + a plain reason) — never a secret. None until Auto has routed a turn.
        d = getattr(app.state, "last_route", None)
        if d is None:
            return None
        return {
            "provider": d.provider,
            "model": d.model,
            "tier": d.tier,
            "mode": d.mode,
            "reason": d.reason,
            "tools_enabled": getattr(d, "tools_enabled", True),
        }

    def _capabilities(workspace=None) -> dict:
        # THE one availability truth (Phase 15.5), computed from live state: connectors/providers/
        # services/voice/MCP with exposed_to_chat + plain reasons. Daily, Hub, Settings, and the
        # header all render THIS (embedded in their payloads + the dedicated route), so they can
        # never disagree. exposed_to_chat is exact when the live loop's tools are available.
        connectors = app.state.services.connectors
        active_voice = workspace.voice if workspace is not None else app.state.voice
        voice = active_voice.status() if active_voice is not None else {"enabled": False}
        registered: set[str] | None = None
        sess = workspace.session if workspace is not None else app.state.session
        registry = getattr(getattr(sess, "loop", None), "registry", None)
        names = getattr(registry, "names", None)
        if callable(names):
            try:
                registered = set(names())
            except Exception:  # noqa: BLE001 - capability status must remain a read-only fallback
                registered = None
        return capability_truth(
            config,
            connectors=connectors.status() if connectors is not None else None,
            voice=voice,
            registered_tools=registered,
        )

    @app.get("/api/capabilities")
    async def capabilities(request: Request) -> JSONResponse:
        workspace = _workspace_for(request)
        if app.state.workspaces is not None and workspace is None:
            return _workspace_required()
        return JSONResponse(_capabilities(workspace))

    @app.get("/api/daily")
    async def daily(request: Request) -> JSONResponse:
        # Read-only Daily bootstrap (Phase 9). NOT a mutating route.
        workspace = _workspace_for(request)
        if app.state.workspaces is not None and workspace is None:
            return _workspace_required()
        pending = len(app.state.approvals.pending_for(workspace.context)) if workspace else 0
        data = await daily_overview(
            config,
            app.state.services,
            notices=app.state.notices,
            notice_project_id=workspace.context.project_id if workspace is not None else None,
            scope_notices=workspace is not None,
            gate_pending=pending,
        )
        data["capabilities"] = _capabilities(workspace)  # Phase 15.5 shared connector truth
        return JSONResponse(data)

    @app.get("/api/lab")
    async def lab() -> dict:
        return await lab_overview(config, artifacts=app.state.services.artifacts)

    # --- read models: Memory / Tasks / Vault / Agents (need host services) ------------
    # These return JSONResponse uniformly (data or a 503) so FastAPI treats them as a
    # passthrough — no response-model inference over a Response|data union.

    @app.get("/api/memory")
    async def memory(
        request: Request, type: str | None = None, project_id: int | None = None
    ) -> JSONResponse:
        # A project workspace always sees its own + global memories. The optional query parameter
        # is a compatibility echo, never a browser-controlled cross-project selector.
        from jarvis.ui.readmodels import _MEM_ANY_PROJECT

        svc = app.state.services.memory
        if svc is None:
            return _unavailable("memory")
        workspace = _workspace_for(request)
        if app.state.workspaces is not None and workspace is None:
            return _workspace_required()
        if workspace is not None:
            active_project_id = workspace.context.project_id
            if project_id is not None and project_id != active_project_id:
                return _deny(status.HTTP_404_NOT_FOUND, "not found")
            scope = active_project_id if active_project_id is not None else _MEM_ANY_PROJECT
        else:
            scope = _MEM_ANY_PROJECT if project_id is None else project_id
        return JSONResponse(await list_memories(svc, type_filter=type, project_id=scope))

    @app.get("/api/tasks")
    async def tasks(request: Request, project_id: int | None = None) -> JSONResponse:
        # A project workspace sees its project + global tasks. The global workspace retains the
        # aggregate screen, while an optional query id is only an active-project compatibility echo.
        from jarvis.scheduler.store import ANY_PROJECT

        svc = app.state.services.tasks
        if svc is None:
            return _unavailable("tasks")
        workspace = _workspace_for(request)
        if app.state.workspaces is not None and workspace is None:
            return _workspace_required()
        if workspace is not None:
            active_project_id = workspace.context.project_id
            if project_id is not None and project_id != active_project_id:
                return _deny(status.HTTP_404_NOT_FOUND, "not found")
            scope = active_project_id if active_project_id is not None else ANY_PROJECT
        else:
            scope = ANY_PROJECT if project_id is None else project_id
        return JSONResponse(await list_tasks(svc, project_id=scope))

    @app.get("/api/tasks/{task_id}/runs")
    async def tasks_runs(task_id: int, request: Request) -> JSONResponse:
        svc = app.state.services.tasks
        if svc is None:
            return _unavailable("tasks")
        workspace = _workspace_for(request)
        if app.state.workspaces is not None and workspace is None:
            return _workspace_required()
        task = await svc.store.get(task_id)
        if task is None:
            return _deny(status.HTTP_404_NOT_FOUND, "not found")
        if workspace is not None and not _workspace_can_access_project(task.project_id, workspace):
            # A workspace may read its own task history plus explicitly global tasks, matching
            # the project task-list contract. The global workspace retains its documented
            # aggregate view; a project workspace never gains another project's history from an
            # observed numeric id.
            return _deny(status.HTTP_404_NOT_FOUND, "not found")
        rows = await task_runs(svc, task_id)
        stored_by_id = {item.id: item for item in await svc.store.runs_for(task_id, limit=200)}
        # A full continuation is deliberately returned only from this already scope-checked
        # history read.  Register the verified projection so the browser can then prove its
        # exact local review over the live socket and receive a nonce.
        for run in rows:
            continuation = run.get("continuation")
            if run.get("approval_state") == "pending" and continuation is not None:
                # ``task_runs`` serializes the same ``TaskRun`` object the store parsed and
                # verified.  Re-read the concrete run below rather than trusting the JSON view.
                stored = stored_by_id.get(run["id"])
                if stored is not None and stored.continuation is not None:
                    app.state.parked_task_approvals.register(
                        run_id=stored.id, task=task, continuation=stored.continuation
                    )
        return JSONResponse(rows)

    @app.get("/api/vault")
    async def vault(request: Request, project_id: int | None = None) -> JSONResponse:
        svc = app.state.services.knowledge
        if svc is None:
            return _unavailable("knowledge")
        workspace = _workspace_for(request)
        if app.state.workspaces is not None and workspace is None:
            return _workspace_required()
        if workspace is not None:
            # A query parameter must not become a cross-project read capability.  The active
            # authenticated workspace owns this view; a different id is a not-found response.
            if project_id is not None and project_id != workspace.context.project_id:
                return _deny(status.HTTP_404_NOT_FOUND, "not found")
            project_id = workspace.context.project_id
        return JSONResponse(
            await vault_overview(svc, project_id=project_id, graph=app.state.services.graph)
        )

    @app.get("/api/vault/lint")
    async def vault_lint_route() -> JSONResponse:
        svc = app.state.services.knowledge
        if svc is None:
            return _unavailable("knowledge")
        return JSONResponse(await vault_lint(svc))

    @app.get("/api/chat/files")
    async def chat_files(request: Request) -> JSONResponse:
        """Metadata for documents explicitly attached to this exact active chat only."""
        svc = app.state.services.knowledge
        if svc is None:
            return _unavailable("knowledge")
        scope = _chat_scope(request)
        if app.state.workspaces is not None and scope is None:
            return _workspace_required()
        if scope is None:
            # A fresh legacy chat has no durable session yet, so it cannot have chat-owned files.
            return JSONResponse({"files": []})
        rows = await svc.store.list_sources(
            status="live", source_session_id=scope.session_id, project_id=scope.project_id
        )
        return JSONResponse({"files": [serialize_chat_file(source) for source in rows]})

    @app.get("/api/chat/outputs")
    async def chat_outputs(request: Request) -> JSONResponse:
        """Current project's registered outputs for the chat context shelf.

        Artifacts do not yet retain a source-session foreign key, so this deliberately returns
        only the exact project (or Global), never claims that every item was created by this chat.
        """
        store = app.state.services.artifacts
        if store is None:
            return _unavailable("artifacts")
        scope = _chat_scope(request)
        if app.state.workspaces is not None and scope is None:
            return _workspace_required()
        project_id = scope.project_id if scope is not None else None
        rows = [
            artifact
            for artifact in await store.list(project_id=project_id, include_global=False, limit=50)
            if artifact.sensitivity != "quarantined"
        ]
        return JSONResponse({"artifacts": [serialize_artifact(artifact) for artifact in rows]})

    @app.get("/api/chat/outputs/{artifact_id}/content")
    async def chat_output_content(artifact_id: int, request: Request) -> Response:
        """Download a project output through the same authenticated workspace handle."""
        store = app.state.services.artifacts
        if store is None:
            return _unavailable("artifacts")
        scope = _chat_scope(request)
        if app.state.workspaces is not None and scope is None:
            return _workspace_required()
        artifact = await store.get(artifact_id)
        project_id = scope.project_id if scope is not None else None
        if artifact is None or artifact.project_id != project_id:
            return _deny(status.HTTP_404_NOT_FOUND, "not found")
        return _chat_output_download(store, artifact)

    @app.get("/api/chat/knowledge")
    async def chat_knowledge(request: Request, project_id: int | None = None) -> JSONResponse:
        """A compact, project-bound knowledge shelf for the active chat.

        This is deliberately metadata-only: project sources and a small derived graph preview.
        A Workspace tab may repeat its project id to make its request explicit, but that id is
        only accepted when it exactly matches the authenticated live workspace/chat context. It
        is never a cross-project selector; source bodies, local paths, and graph selectors remain
        server-controlled.
        """
        knowledge = app.state.services.knowledge
        if knowledge is None:
            return _unavailable("knowledge")
        scope = _chat_scope(request)
        if app.state.workspaces is not None and scope is None:
            return _workspace_required()
        active_project_id = scope.project_id if scope is not None else None
        if project_id is not None and project_id != active_project_id:
            return _deny(status.HTTP_404_NOT_FOUND, "not found")
        project_id = active_project_id
        empty_graph = {
            "available": app.state.services.graph is not None,
            "nodes": [],
            "edge_count": 0,
            "truncated": False,
        }
        # Global chats are intentionally not a back door into every global source.  Choose a
        # project first, then Kairo can retrieve and visualize only that project's knowledge.
        if project_id is None:
            return JSONResponse(
                {
                    "project_id": None,
                    "source_count": 0,
                    "sources": [],
                    "folder_imports": [],
                    "graph": empty_graph,
                }
            )

        sources = await knowledge.store.list_sources(status="live", project_id=project_id)
        folder_prefix = f"chat-upload:{project_id}:"
        folder_counts: dict[str, int] = {}
        for source in sources:
            if not source.origin.startswith(folder_prefix):
                continue
            relative = source.origin[len(folder_prefix):]
            root = relative.split("/", 1)[0]
            if root and "/" in relative:
                folder_counts[root] = folder_counts.get(root, 0) + 1
        graph = empty_graph
        graph_store = app.state.services.graph
        if graph_store is not None:
            preview = await subgraph(graph_store, project_id, depth=2, limit=24)
            graph = {
                "available": True,
                # Preserve the graph service's bodies-free cards, but only send fields needed by
                # the compact shelf.  Source content and managed paths never cross this boundary.
                "nodes": [
                    {
                        "id": node["id"],
                        "kind": node["kind"],
                        "label": node["label"],
                        "degree": node["degree"],
                        "trust_class": node["trust_class"],
                    }
                    for node in preview["nodes"][:8]
                ],
                "edge_count": len(preview["edges"]),
                "truncated": preview["truncated"],
            }
        return JSONResponse(
            {
                "project_id": project_id,
                "source_count": len(sources),
                # Titles are the logical paths selected by the user, not managed storage paths.
                # Cap the browser tree so a giant repository cannot turn this read model into a
                # corpus dump; all source bodies and origins remain server-side.
                "sources": [serialize_chat_file(source) for source in sources[:300]],
                "sources_truncated": len(sources) > 300,
                "folder_imports": [
                    {"root": root, "source_count": count}
                    for root, count in sorted(
                        folder_counts.items(), key=lambda item: item[0].casefold()
                    )
                ],
                "graph": graph,
            }
        )

    @app.post("/api/chat/knowledge/detach")
    async def detach_chat_knowledge_folder(request: Request) -> JSONResponse:
        """Detach one explicitly imported folder from the active project, audit-preserving.

        This is a local user lifecycle action, equivalent to rejecting KB sources—not an executor,
        tool call, or external write.  The browser can name only a displayed logical folder root;
        project scope still comes exclusively from the authenticated workspace.
        """
        knowledge = app.state.services.knowledge
        if knowledge is None:
            return _unavailable("knowledge")
        workspace = _workspace_for(request)
        if app.state.workspaces is not None and workspace is None:
            return _workspace_required()
        projects = app.state.projects
        project_id = (
            workspace.context.project_id
            if workspace is not None
            else (projects.current().project_id if projects is not None else None)
        )
        body = await request.json()
        root = body.get("root")
        if (
            project_id is None
            or not isinstance(root, str)
            or not root.strip()
            or len(root) > 160
            or root in {".", ".."}
            or "/" in root
            or "\\" in root
        ):
            return JSONResponse({"ok": False, "message": "invalid folder"}, status_code=400)
        root = root.strip()
        detached = await knowledge.store.reject_project_folder_import(
            project_id=project_id, root=root
        )
        if not detached.sources_rejected and not detached.chunks_cleared:
            return _deny(status.HTTP_404_NOT_FOUND, "not found")
        if app.state.services.graph is not None:
            await rebuild_graph(app.state.services.graph)
        return JSONResponse(
            {
                "ok": True,
                "detached_sources": detached.sources_rejected,
                "cleared_chunks": detached.chunks_cleared,
            }
        )

    @app.get("/api/agents")
    async def agents(request: Request) -> JSONResponse:
        svc = app.state.services.run_store
        if svc is None:
            return _unavailable("agents")
        workspace = _workspace_for(request)
        if app.state.workspaces is not None and workspace is None:
            return _workspace_required()
        # The project is server-selected from the live workspace. A global workspace is the
        # deliberate administrative aggregate; a project workspace cannot enumerate another
        # project's delegated history by adding a query string or guessing a run id.
        project_id = workspace.context.project_id if workspace is not None else None
        return JSONResponse(await list_agent_runs(svc, project_id=project_id))

    @app.get("/api/sessions")
    async def sessions_list(
        request: Request,
        query: str | None = None,
        pinned: bool | None = None,
        project_id: int | None = None,
        limit: int = 50,
    ) -> JSONResponse:
        svc = app.state.services.sessions
        if svc is None:
            return _unavailable("sessions")
        workspace = _workspace_for(request)
        if app.state.workspaces is not None and workspace is None:
            return _workspace_required()
        # A history list is as private as a transcript: never let a live workspace enumerate
        # another project's chat titles. Cross-project resume remains the explicit lifecycle path.
        if workspace is not None:
            if project_id is not None and project_id != workspace.context.project_id:
                return JSONResponse(
                    {"ok": False, "message": "wrong project scope"}, status_code=404
                )
            project_id = workspace.context.project_id
        return JSONResponse(
            await list_sessions_view(
                svc,
                query=query,
                pinned=pinned,
                project_id=project_id,
                scope_project=workspace is not None,
                limit=max(1, min(limit, 200)),
            )
        )

    @app.get("/api/sessions/{session_id}")
    async def sessions_get(session_id: int, request: Request) -> JSONResponse:
        svc = app.state.services.sessions
        if svc is None:
            return _unavailable("sessions")
        workspace = _workspace_for(request)
        if app.state.workspaces is not None and workspace is None:
            return _workspace_required()
        meta = await svc.get_meta(session_id)
        if meta is None:
            return JSONResponse({"ok": False, "message": "no such session"}, status_code=404)
        # A transcript is private to its project context. Resume handles the deliberate
        # cross-project transition atomically; arbitrary GETs do not get that privilege.
        if workspace is not None and meta.project_id != workspace.context.project_id:
            return JSONResponse({"ok": False, "message": "wrong project scope"}, status_code=404)
        return JSONResponse(
            await session_transcript(svc, session_id, run_store=app.state.services.run_store)
        )

    @app.get("/api/projects")
    async def projects_list(request: Request) -> JSONResponse:
        svc = app.state.projects
        if svc is None:
            return _unavailable("projects")
        workspace = _workspace_for(request)
        if app.state.workspaces is not None and workspace is None:
            return _workspace_required()
        view = await projects_view(svc)
        if workspace is not None:
            view["active_project_id"] = workspace.project.project_id
        return JSONResponse(view)

    @app.get("/api/costs")
    async def costs(project_id: int | None = None) -> JSONResponse:
        budgets = app.state.services.budgets
        if budgets is None:
            return _unavailable("costs")
        return JSONResponse(
            await costs_overview(
                budgets,
                project_id=project_id,
                projects=app.state.projects,
                ledger=app.state.services.ledger,
            )
        )

    @app.get("/api/roi")
    async def roi(project_id: int | None = None) -> JSONResponse:
        # Outcome-gated per-run ROI plus terminal model-cost accounting for the Cost Center.
        # Read-only metadata only; service estimates are intentionally outside actual model cost.
        store = app.state.services.orchestration
        budgets = app.state.services.budgets
        if store is None or budgets is None:
            return _unavailable("roi")
        runs = await orchestration_roi(store, budgets, project_id=project_id)
        return JSONResponse(
            {
                "roi": runs,
                "outcome_accounting": orchestration_outcome_accounting(runs),
                "estimate_accuracy": await orchestration_estimate_accuracy(
                    store, project_id=project_id
                ),
            }
        )

    # --- Studio (orchestration): catalog + runs + estimate (all read-only) -------------

    @app.get("/api/studio")
    async def studio(request: Request) -> JSONResponse:
        # The Studio bootstrap: team profiles + workflow templates (code constants) + service
        # availability + model routes — all presence/metadata only, no key value ever. Always
        # available (pure over config + constants); the run mutations are gated separately.
        workspace = _workspace_for(request)
        if app.state.workspaces is not None and workspace is None:
            return _workspace_required()
        projects = app.state.projects
        active = (
            workspace.project
            if workspace is not None
            else (projects.current() if projects is not None else None)
        )
        # Per-project narrowing (Phase 13): show the project's subset when it narrows, else the
        # full global availability. A project can only narrow (the write route enforces it).
        proj_services = (
            list(active.services) if (active is not None and active.services is not None) else None
        )
        return JSONResponse(
            {
                "teams": teams_catalog(),
                "workflows": workflows_catalog(),
                "services": services_status(config, project_services=proj_services),
                "model_routes": model_routes_status(config),
                "providers": providers_status(config),
                "active_project_id": active.project_id if active is not None else None,
                "busy": bool(
                    app.state.orchestrator is not None
                    and app.state.orchestrator.busy_for(
                        workspace.context if workspace is not None else None
                    )
                ),
            }
        )

    @app.get("/api/orchestration")
    async def orchestration_list(request: Request, project_id: int | None = None) -> JSONResponse:
        store = app.state.services.orchestration
        if store is None:
            return _unavailable("orchestration")
        workspace = _workspace_for(request)
        if app.state.workspaces is not None and workspace is None:
            return _workspace_required()
        if workspace is not None:
            if project_id is not None and project_id != workspace.context.project_id:
                return JSONResponse(
                    {"ok": False, "message": "wrong project scope"}, status_code=404
                )
            project_id = workspace.context.project_id
        return JSONResponse(await orchestration_runs_view(store, project_id=project_id))

    @app.get("/api/orchestration/estimate")
    async def orchestration_estimate(
        request: Request, team: str, workflow: str, task: str = "", budget_usd: float | None = None
    ) -> JSONResponse:
        # A GET (no state change) so the two-step confirm preview never touches the mutation
        # closed-set. Returns cost metadata only.
        orch = app.state.orchestrator
        if orch is None:
            return _unavailable("orchestration")
        workspace = _workspace_for(request)
        if app.state.workspaces is not None and workspace is None:
            return _workspace_required()
        result = await orch.estimate(
            team,
            workflow,
            task=task,
            budget_usd=budget_usd,
            execution_context=workspace.context if workspace is not None else None,
            project=workspace.project if workspace is not None else None,
        )
        return JSONResponse(result, status_code=200 if result.get("ok") else 400)

    @app.get("/api/orchestration/{run_id}")
    async def orchestration_detail(run_id: int, request: Request) -> JSONResponse:
        store = app.state.services.orchestration
        if store is None:
            return _unavailable("orchestration")
        workspace = _workspace_for(request)
        if app.state.workspaces is not None and workspace is None:
            return _workspace_required()
        if workspace is not None:
            run = await store.get(run_id)
            if run is None or run.project_id != workspace.context.project_id:
                return JSONResponse(
                    {"ok": False, "message": "no such orchestration run"}, status_code=404
                )
        return JSONResponse(
            await orchestration_run_detail(
                store,
                app.state.services.run_store,
                run_id,
                budgets=app.state.services.budgets,
            )
        )

    # --- mutations: the enumerated human-authority set (D5, route-closed-set pin) ------

    @app.post("/api/vault/sources/{source_id}/approve")
    async def vault_approve(source_id: int) -> JSONResponse:
        svc = app.state.services.knowledge
        if svc is None:
            return _unavailable("knowledge")
        await svc.approve_source(source_id)  # = `kb review` approve
        return JSONResponse({"ok": True})

    @app.post("/api/vault/sources/{source_id}/reject")
    async def vault_reject(source_id: int) -> JSONResponse:
        svc = app.state.services.knowledge
        if svc is None:
            return _unavailable("knowledge")
        rejected = await svc.reject_source(source_id)
        return JSONResponse({"ok": bool(rejected)})

    @app.post("/api/vault/ingest")
    async def vault_ingest(request: Request) -> JSONResponse:
        # Human-initiated ingest (the click IS the approval, like vault approve). A file path
        # runs the SAME sensitive-path floor as the ingest_source tool (DENY ⇒ 403); a url
        # keeps the KnowledgeService's SSRF-guarded fetch. Lands 'reviewed' (created_by=user).
        svc = app.state.services.knowledge
        if svc is None:
            return _unavailable("knowledge")
        workspace = _workspace_for(request)
        if app.state.workspaces is not None and workspace is None:
            return _workspace_required()
        body = await request.json()
        path, url, text = body.get("path"), body.get("url"), body.get("text")
        title = body.get("title")
        given = [v for v in (path, url, text) if v]
        if len(given) != 1:
            return JSONResponse(
                {"ok": False, "message": "give exactly one of path / url / text"}, status_code=400
            )
        if path:
            decision = app.state.gate.check("ingest_source", {"path": path})
            if decision.permission is Permission.DENY:
                return JSONResponse({"ok": False, "message": decision.reason}, status_code=403)
        # Tag the ingest with the active project (Phase 10 A1) so it's retrievable in that
        # scope and never leaked to another project; None (global) when no project is active.
        projects = app.state.projects
        active_pid = (
            workspace.context.project_id
            if workspace is not None
            else (projects.current().project_id if projects is not None else None)
        )
        try:
            scope = (
                bind_execution_context(workspace.context)
                if workspace is not None
                else contextlib.nullcontext()
            )
            with scope:
                result = await svc.ingest(
                    path=path,
                    url=url,
                    text=text,
                    title=title,
                    created_by="user",
                    project_id=active_pid,
                )
        except Exception as exc:
            return JSONResponse({"ok": False, "message": str(exc)}, status_code=400)
        return JSONResponse({"ok": True, "action": result.action, "source_id": result.source_id})

    @app.post("/api/chat/attachments")
    async def chat_attachment(request: Request) -> JSONResponse:
        """Persist one browser-selected document into the current chat/project knowledge scope.

        This is an explicit local-user action, like the existing Vault ingest click. The upload is
        byte-capped before conversion, staged only under the knowledge jail, and immediately
        removed after the existing sandboxed ingest pipeline stores its immutable artifact.
        """
        svc = app.state.services.knowledge
        if svc is None:
            return _unavailable("knowledge")
        workspace = _workspace_for(request)
        if app.state.workspaces is not None and workspace is None:
            return _workspace_required()
        try:
            form = await request.form(
                max_files=1,
                max_fields=6,
                max_part_size=svc.config.max_ingest_bytes + 1,
            )
            upload = form.get("file")
            relative_path = form.get("relative_path")
            projects = app.state.projects
            project_id = (
                workspace.context.project_id
                if workspace is not None
                else (projects.current().project_id if projects is not None else None)
            )
            # The folder UI finalizes separately, so one rejected last file cannot leave the
            # already-indexed project without its derived source edges.
            if form.get("finalize") == "true" and upload is None:
                if project_id is None or app.state.services.graph is None:
                    return JSONResponse({"ok": True, "graph_rebuilt": False})
                await rebuild_graph(app.state.services.graph)
                return JSONResponse({"ok": True, "graph_rebuilt": True})
            filename = getattr(upload, "filename", None)
            reader = getattr(upload, "read", None)
            if not filename or not callable(reader):
                return JSONResponse({"ok": False, "message": "choose one file"}, status_code=400)
            cap = svc.config.max_ingest_bytes
            raw = bytearray()
            closer = getattr(upload, "close", None)
            try:
                while chunk := await reader(64 * 1024):
                    raw.extend(chunk)
                    if len(raw) > cap:
                        return JSONResponse(
                            {"ok": False, "message": "file exceeds Kairo's upload size limit"},
                            status_code=413,
                        )
            finally:
                if callable(closer):
                    await closer()
            source_session_id = workspace.context.session_id if workspace is not None else None
            if source_session_id is None:
                # The legacy single-session host has the same invariant as workspaces: an upload
                # belongs to one durable chat.  Allocate the lazy row before ingestion when the
                # live UI session can do so; bare test/utility compositions remain harmlessly
                # unbound rather than guessing a session id.
                ui_session = app.state.session
                ensure_session = getattr(ui_session, "ensure_session", None)
                if callable(ensure_session):
                    source_session_id = await ensure_session()
                else:
                    source_session_id = getattr(ui_session, "session_id", None)
            result = await svc.ingest_uploaded(
                filename,
                bytes(raw),
                created_by="user",
                source_session_id=source_session_id,
                project_id=project_id,
                relative_path=str(relative_path) if relative_path else None,
            )
        except Exception:  # conversion errors can contain unhelpful local parser details
            log.warning("chat_attachment_ingest_failed", exc_info=True)
            return JSONResponse(
                {
                    "ok": False,
                    "message": (
                        "Kairo couldn't add that file. "
                        "Use a supported document under the upload limit."
                    ),
                },
                status_code=400,
            )
        return JSONResponse(
            {
                "ok": True,
                "action": result.action,
                "source_id": result.source_id,
                "title": result.title or filename,
                "chunks": result.chunks,
                "review_status": result.review_status,
            }
        )

    @app.post("/api/digest/run")
    async def digest_run(request: Request) -> JSONResponse:
        # "Run digest now" — deterministic collectors + one tool-less summarize, then UI/DB
        # delivery. The current digest is deliberately global: project tabs must not make a
        # global collector/model call or receive its result under a project heading.
        workspace = _workspace_for(request)
        if app.state.workspaces is not None and workspace is None:
            return _workspace_required()
        if workspace is not None and workspace.context.project_id is not None:
            return JSONResponse(
                {"ok": False, "message": "digest is available from the global workspace"},
                status_code=status.HTTP_409_CONFLICT,
            )
        run_now = getattr(app.state, "run_digest_now", None)
        if run_now is None:
            return _unavailable("digest")
        session = workspace.session if workspace is not None else app.state.session
        if session is not None and session.busy:
            return JSONResponse({"ok": False, "message": "busy"}, status_code=409)
        outcome = await run_now()
        return JSONResponse({"ok": True, "summary": outcome.text})

    @app.post("/api/tasks/{task_id}/cancel")
    async def tasks_cancel(task_id: int, request: Request) -> JSONResponse:
        svc = app.state.services.tasks
        if svc is None:
            return _unavailable("tasks")
        workspace = _workspace_for(request)
        if app.state.workspaces is not None and workspace is None:
            return _workspace_required()
        if workspace is not None:
            # Match the task list/history contract: a project workspace may cancel only its own
            # task or a global task. The global workspace retains its administrative aggregate;
            # a guessed foreign id is never a capability from a project workspace.
            task = await svc.store.get(task_id)
            if task is None or (
                workspace.context.project_id is not None
                and task.project_id not in (None, workspace.context.project_id)
            ):
                return _deny(status.HTTP_404_NOT_FOUND, "not found")
        cancelled = await svc.cancel(task_id)
        return JSONResponse({"ok": cancelled is not None})

    @app.post("/api/memory/{memory_id}/forget")
    async def memory_forget(memory_id: int, request: Request) -> JSONResponse:
        svc = app.state.services.memory
        if svc is None:
            return _unavailable("memory")
        workspace = _workspace_for(request)
        if app.state.workspaces is not None and workspace is None:
            return _workspace_required()
        if workspace is not None:
            # A memory id is not authority. Project workspaces may forget their own memories or
            # deliberately shared global memories, never an id guessed from another project.
            memory = await svc.store.get(memory_id)
            if memory is None or (
                memory.project_id is not None and memory.project_id != workspace.context.project_id
            ):
                return _deny(status.HTTP_404_NOT_FOUND, "not found")
        forgotten = await svc.store.forget(memory_id)  # status flip, never DELETE
        return JSONResponse({"ok": bool(forgotten)})

    @app.post("/api/memory/remember")
    async def memory_remember(request: Request) -> JSONResponse:
        # Human-authority remember (the promote-to-memory target): the click IS the authority,
        # like vault ingest. Stored source='user', scoped to the ACTIVE project (never a body-
        # supplied project_id — a promote can't cross-scope). Content is the user-selected text.
        svc = app.state.services.memory
        if svc is None:
            return _unavailable("memory")
        workspace = _workspace_for(request)
        if app.state.workspaces is not None and workspace is None:
            return _workspace_required()
        try:
            body = await request.json()
        except ValueError:
            return JSONResponse({"ok": False, "message": "invalid JSON body"}, status_code=400)
        if not isinstance(body, dict):
            return JSONResponse(
                {"ok": False, "message": "memory request must be an object"}, status_code=400
            )
        raw_content = body.get("content")
        if not isinstance(raw_content, str):
            return JSONResponse(
                {"ok": False, "message": "content must be text"}, status_code=400
            )
        content = raw_content.strip()
        if not content:
            return JSONResponse({"ok": False, "message": "content required"}, status_code=400)
        if len(content) > 4000:
            return JSONResponse(
                {"ok": False, "message": "content must be at most 4000 characters"}, status_code=400
            )
        mem_type = body.get("type", "fact")
        if not isinstance(mem_type, str) or mem_type not in _MEMORY_TYPES:
            return JSONResponse(
                {"ok": False, "message": "invalid memory type"}, status_code=400
            )
        checked_context: ExecutionContext | None = None
        if workspace is not None:
            expected = body.get("expected_context")
            # This is a freshness check, never a browser-selected scope: capture the one live
            # workspace context under the same lock that project/session transitions use, and
            # reject a draft that was reviewed before another duplicate tab switched context.
            async with app.state.workspaces.transition_lock:
                checked_context = workspace.context
                if (
                    not isinstance(expected, dict)
                    or expected.get("session_id") != checked_context.session_id
                    or expected.get("project_id") != checked_context.project_id
                ):
                    return JSONResponse(
                        {
                            "ok": False,
                            "message": "workspace context changed; review the memory again",
                        },
                        status_code=status.HTTP_409_CONFLICT,
                    )
        projects = app.state.projects
        pid = (
            checked_context.project_id
            if checked_context is not None
            else (projects.current().project_id if projects is not None else None)
        )
        scope = (
            bind_execution_context(checked_context)
            if checked_context is not None
            else contextlib.nullcontext()
        )
        with scope:
            result = await svc.remember(content, mem_type, source="user", project_id=pid)
        return JSONResponse({"ok": True, "id": result.memory_id, "action": result.action})

    @app.post("/api/tasks/create")
    async def tasks_create(request: Request) -> JSONResponse:
        # Human-authority task/reminder creation (the promote-to-task target). created_by=user.
        svc = app.state.services.tasks
        if svc is None:
            return _unavailable("tasks")
        workspace = _workspace_for(request)
        if app.state.workspaces is not None and workspace is None:
            return _workspace_required()
        try:
            body = await request.json()
        except ValueError:
            return JSONResponse({"ok": False, "message": "invalid JSON body"}, status_code=400)
        if not isinstance(body, dict):
            return JSONResponse(
                {"ok": False, "message": "task request must be an object"}, status_code=400
            )
        checked_context: ExecutionContext | None = None
        if workspace is not None:
            expected = body.get("expected_context")
            # This is a freshness check, never a browser-selected scope: capture the one live
            # workspace context under the same lock that project/session transitions use, and
            # reject a task draft reviewed before another duplicate tab switched context.
            async with app.state.workspaces.transition_lock:
                checked_context = workspace.context
                if (
                    not isinstance(expected, dict)
                    or expected.get("session_id") != checked_context.session_id
                    or expected.get("project_id") != checked_context.project_id
                ):
                    return JSONResponse(
                        {
                            "ok": False,
                            "message": "workspace context changed; review the task again",
                        },
                        status_code=status.HTTP_409_CONFLICT,
                    )
        projects = app.state.projects
        pid = (
            checked_context.project_id
            if checked_context is not None
            else (projects.current().project_id if projects is not None else None)
        )
        try:
            raw_verification = body.get("verify_contains")
            verification = (
                None
                if raw_verification is None
                else VerificationContract.contains_all(raw_verification)
            )
            scope = (
                bind_execution_context(checked_context)
                if checked_context is not None
                else contextlib.nullcontext()
            )
            with scope:
                task = await svc.schedule(
                    kind=str(body.get("kind", "reminder")),
                    title=str(body.get("title", "")).strip() or "reminder",
                    payload=str(body.get("payload", "")),
                    schedule_kind=str(body.get("schedule_kind", "once")),
                    schedule_spec=str(body.get("schedule_spec", "")),
                    created_by="user",
                    project_id=pid,  # a promoted task belongs to the active project
                    verification=verification,
                )
        except Exception as exc:  # noqa: BLE001 - a bad schedule is a 400, not a 500
            return JSONResponse({"ok": False, "message": str(exc)}, status_code=400)
        return JSONResponse({"ok": True, "id": task.id})

    @app.post("/api/sessions/{session_id}/pin")
    async def sessions_pin(session_id: int, request: Request) -> JSONResponse:
        # Pin/unpin a chat (body {"pinned": bool}) — a display preference, no new authority.
        svc = app.state.services.sessions
        if svc is None:
            return _unavailable("sessions")
        workspace = _workspace_for(request)
        if app.state.workspaces is not None and workspace is None:
            return _workspace_required()
        meta = await svc.get_meta(session_id)
        if not _session_in_workspace(meta, workspace):
            return JSONResponse({"ok": False, "message": "wrong project scope"}, status_code=404)
        body = await request.json()
        ok = await svc.set_pinned(session_id, bool(body.get("pinned", True)))
        return JSONResponse({"ok": ok})

    @app.post("/api/sessions/{session_id}/resume")
    async def sessions_resume(session_id: int, request: Request) -> JSONResponse:
        # Load a past chat into the live UI session (mirrors REPL --resume). 409 if a turn is
        # in flight (the loop state must not change mid-turn).
        workspace = _workspace_for(request)
        if app.state.workspaces is not None and workspace is None:
            return _workspace_required()
        sess = workspace.session if workspace is not None else app.state.session
        if sess is None:
            return _unavailable("sessions")
        if workspace is not None:
            async with app.state.workspaces.transition_lock:
                resumed = await workspace.resume(session_id)
                if resumed:
                    app.state.workspaces.refresh_context(workspace)
                    await app.state.workspaces.publish_workspace(
                        workspace,
                        {
                            "kind": "session_resumed",
                            "name": workspace.project.name,
                        },
                    )
        else:
            resumed = await sess.resume(session_id)
        return JSONResponse({"ok": resumed}, status_code=200 if resumed else 409)

    @app.post("/api/sessions/new")
    async def sessions_new(request: Request) -> JSONResponse:
        # Phase 15.5: start a FRESH conversation under the current project scope (exposes the
        # existing UiSession.start_new_session). 409 while a turn is in flight — the loop state
        # must not change mid-turn. No new authority: a new session is created lazily on its first
        # turn, exactly like the REPL.
        workspace = _workspace_for(request)
        if app.state.workspaces is not None and workspace is None:
            return _workspace_required()
        sess = workspace.session if workspace is not None else app.state.session
        if sess is None:
            return _unavailable("sessions")
        if sess.busy:
            return JSONResponse({"ok": False, "message": "busy"}, status_code=409)
        if workspace is not None:
            async with app.state.workspaces.transition_lock:
                await workspace.start_new_session()
                app.state.workspaces.refresh_context(workspace)
                await app.state.workspaces.publish_workspace(
                    workspace,
                    {"kind": "session_new", "name": workspace.project.name},
                )
        else:
            projects = app.state.projects
            cur = projects.current() if projects is not None else None
            sess.start_new_session(cur.project_id if cur is not None else None)
        return JSONResponse({"ok": True})

    @app.post("/api/sessions/{session_id}/rename")
    async def sessions_rename(session_id: int, request: Request) -> JSONResponse:
        # Rename a chat (metadata only, no reorder). Body {"title": str}; empty/blank is a 400.
        svc = app.state.services.sessions
        if svc is None:
            return _unavailable("sessions")
        workspace = _workspace_for(request)
        if app.state.workspaces is not None and workspace is None:
            return _workspace_required()
        meta = await svc.get_meta(session_id)
        if not _session_in_workspace(meta, workspace):
            return JSONResponse({"ok": False, "message": "wrong project scope"}, status_code=404)
        title = str((await request.json()).get("title", "")).strip()
        if not title:
            return JSONResponse({"ok": False, "message": "title required"}, status_code=400)
        ok = await svc.set_title(session_id, title)
        return JSONResponse({"ok": ok})

    @app.post("/api/sessions/{session_id}/archive")
    async def sessions_archive(session_id: int, request: Request) -> JSONResponse:
        # Archive/unarchive a chat — a status flip (never a delete); body {"archived": bool}.
        svc = app.state.services.sessions
        if svc is None:
            return _unavailable("sessions")
        workspace = _workspace_for(request)
        if app.state.workspaces is not None and workspace is None:
            return _workspace_required()
        meta = await svc.get_meta(session_id)
        if not _session_in_workspace(meta, workspace):
            return JSONResponse({"ok": False, "message": "wrong project scope"}, status_code=404)
        archived = bool((await request.json()).get("archived", True))
        if app.state.workspaces is not None:
            async with app.state.workspaces.transition_lock:
                affected = app.state.workspaces.for_session(session_id)
                orchestrator = app.state.orchestrator
                if archived and (
                    any(item.attended_busy for item in affected)
                    or any(
                        orchestrator is not None and orchestrator.busy_for(item.context)
                        for item in affected
                    )
                ):
                    return JSONResponse({"ok": False, "message": "busy"}, status_code=409)
                ok = await svc.set_archived(session_id, archived)
                if ok and archived:
                    for item in affected:
                        await item.start_new_session()
                        app.state.workspaces.refresh_context(item)
                        await app.state.workspaces.publish_workspace(
                            item,
                            {"kind": "session_new", "name": item.project.name},
                        )
        else:
            ok = await svc.set_archived(session_id, archived)
        return JSONResponse({"ok": ok})

    @app.post("/api/projects")
    async def projects_create(request: Request) -> JSONResponse:
        svc = app.state.projects
        if svc is None:
            return _unavailable("projects")
        body = await request.json()
        name = str(body.get("name", "")).strip()
        if not name:
            return JSONResponse({"ok": False, "message": "name required"}, status_code=400)
        pid = await svc.store.create(
            name=name,
            description=body.get("description"),
            color=body.get("color"),
            icon=body.get("icon"),
            repos=body.get("repos"),
        )
        return JSONResponse({"ok": True, "id": pid})

    @app.post("/api/projects/{project_id}/update")
    async def projects_update(project_id: int, request: Request) -> JSONResponse:
        svc = app.state.projects
        if svc is None:
            return _unavailable("projects")
        workspace = _workspace_for(request)
        if app.state.workspaces is not None and workspace is None:
            return _workspace_required()
        if not _workspace_can_access_project(project_id, workspace):
            return _deny(status.HTTP_404_NOT_FOUND, "not found")
        body = await request.json()
        if not isinstance(body, dict):
            return JSONResponse(
                {"ok": False, "message": "project update must be an object"}, status_code=400
            )
        repos = body.get("repos")
        if "settings" in body:
            return JSONResponse(
                {"ok": False, "message": "project services use the dedicated settings route"},
                status_code=400,
            )
        if repos is not None and (
            not isinstance(repos, list) or not all(isinstance(item, str) for item in repos)
        ):
            return JSONResponse(
                {"ok": False, "message": "repos must be a list of strings"}, status_code=400
            )
        fields = {k: body[k] for k in ("name", "description", "color", "icon") if k in body}
        if any(value is not None and not isinstance(value, str) for value in fields.values()):
            return JSONResponse(
                {"ok": False, "message": "project fields must be strings"}, status_code=400
            )
        if "name" in fields and not str(fields["name"] or "").strip():
            return JSONResponse({"ok": False, "message": "name required"}, status_code=400)
        try:
            if app.state.workspaces is None:
                ok = await svc.store.update(project_id, repos=repos, **fields)
                if ok:
                    await svc.refresh_project_context(project_id)
            else:
                async with app.state.workspaces.transition_lock:
                    ok = await svc.store.update(
                        project_id, repos=repos, **fields
                    )
                    if ok:
                        refreshed = await svc.refresh_project_context(project_id)
                        for item in app.state.workspaces.for_project(project_id):
                            item.refresh_project_context(refreshed)
        except ValueError as exc:
            return JSONResponse({"ok": False, "message": str(exc)}, status_code=400)
        return JSONResponse({"ok": ok})

    @app.post("/api/projects/{project_id}/archive")
    async def projects_archive(project_id: int, request: Request) -> JSONResponse:
        svc = app.state.projects
        if svc is None:
            return _unavailable("projects")
        workspace = _workspace_for(request)
        if app.state.workspaces is not None and workspace is None:
            return _workspace_required()
        if app.state.workspaces is not None:
            async with app.state.workspaces.transition_lock:
                affected = app.state.workspaces.for_project(project_id)
                orchestrator = app.state.orchestrator
                if any(item.attended_busy for item in affected) or (
                    orchestrator is not None and orchestrator.busy_project(project_id)
                ):
                    return JSONResponse({"ok": False, "message": "busy"}, status_code=409)
                archived = await svc.store.archive(project_id)
                if archived:
                    for item in affected:
                        await item.start_new_session(None)
                        app.state.workspaces.refresh_context(item)
                        await app.state.workspaces.publish_workspace(
                            item,
                            {"kind": "project_changed", "name": item.project.name},
                        )
        else:
            archived = await svc.store.archive(project_id)
        # If the active project was archived, drop back to global scope + fresh chat.
        if archived and svc.current().project_id == project_id:
            await svc.activate(None)
            if app.state.workspaces is None and app.state.session is not None:
                app.state.session.start_new_session(None)
        return JSONResponse({"ok": archived})

    @app.post("/api/projects/select")
    async def projects_select(request: Request) -> JSONResponse:
        # Set the active project (body {"project_id": id|null}). Starts a FRESH conversation —
        # a session is bound to one project for life, so switching never re-tags the current
        # transcript. The loop reads the new scope on its next turn (shared provider).
        svc = app.state.projects
        if svc is None:
            return _unavailable("projects")
        body = await request.json()
        pid = body.get("project_id")
        workspace = _workspace_for(request)
        if app.state.workspaces is not None and workspace is None:
            return _workspace_required()
        if workspace is not None:
            try:
                async with app.state.workspaces.transition_lock:
                    ctx = await workspace.select_project(pid)
                    app.state.workspaces.refresh_context(workspace)
                    await app.state.workspaces.publish_workspace(
                        workspace,
                        {"kind": "project_changed", "name": workspace.project.name},
                    )
            except RuntimeError as exc:
                return JSONResponse({"ok": False, "message": str(exc)}, status_code=409)
            except KeyError as exc:
                return JSONResponse({"ok": False, "message": str(exc)}, status_code=404)
            return JSONResponse({"ok": True, "active_project_id": ctx.project_id})
        try:
            ctx = await svc.activate(pid)
        except KeyError as exc:
            return JSONResponse({"ok": False, "message": str(exc)}, status_code=404)
        if app.state.session is not None:
            app.state.session.start_new_session(ctx.project_id)
        await app.state.connections.broadcast(
            {"kind": "project_changed", "project_id": ctx.project_id, "name": ctx.name}
        )
        return JSONResponse({"ok": True, "active_project_id": ctx.project_id})

    # --- Phase 11: projects pin + artifacts + global search + workspace ------------------------
    # All reads are scoped in SQL (search/artifacts); the mutations are metadata-only pin/label
    # actions, mirroring sessions/pin — no new authority. The palette calls the GETs only.
    @app.get("/api/projects/overview")
    async def projects_overview_route() -> JSONResponse:
        # The Projects grid: active projects + per-project health chips + archived list. Read-only.
        return JSONResponse(await projects_overview(app.state.services))

    @app.post("/api/projects/{project_id}/pin")
    async def projects_pin(project_id: int, request: Request) -> JSONResponse:
        svc = app.state.projects
        if svc is None:
            return _unavailable("projects")
        body = await request.json()
        ok = await svc.store.set_pinned(project_id, bool(body.get("pinned", True)))
        return JSONResponse({"ok": ok})

    @app.post("/api/projects/{project_id}/label")
    async def projects_label(project_id: int, request: Request) -> JSONResponse:
        # Set/clear the project's category chip WITHIN settings_json (merge-safe — never clobbers
        # model/budget/roster overrides). Metadata only; no new authority.
        svc = app.state.projects
        if svc is None:
            return _unavailable("projects")
        body = await request.json()
        label = body.get("label")
        if label is not None:
            label = str(label).strip()[:40] or None
        ok = await svc.store.set_label(project_id, label)
        return JSONResponse({"ok": ok})

    @app.post("/api/projects/{project_id}/services")
    async def projects_services(project_id: int, request: Request) -> JSONResponse:
        # Phase 13: NARROW-ONLY per-project service selection. Every name must be in the global
        # services.enabled set — a project can only SUBSET it, never widen (fail-closed 400 on any
        # non-enabled name). `services: null` clears the narrowing (project uses the full global
        # set). Merge-safe write of settings_json; the tools enforce the narrowing at run time.
        svc = app.state.projects
        if svc is None:
            return _unavailable("projects")
        body = await request.json()
        names = body.get("services")
        if names is None:  # clear narrowing → full global set for this project
            ok = await svc.store.set_services(project_id, None)
            return JSONResponse({"ok": ok, "services": None})
        if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
            return JSONResponse(
                {"ok": False, "message": "services must be a list of names or null"},
                status_code=400,
            )
        enabled = set(config.services.enabled)
        invalid = sorted(n for n in names if n not in enabled)
        if invalid:  # narrow-only: a project can never widen beyond the global set
            return JSONResponse(
                {"ok": False, "message": f"not globally enabled (cannot widen): {invalid}"},
                status_code=400,
            )
        ok = await svc.store.set_services(project_id, sorted(set(names)))
        return JSONResponse({"ok": ok, "services": sorted(set(names))})

    @app.get("/api/artifacts")
    async def artifacts_index(
        project_id: int | None = None,
        kind: str | None = None,
        pinned: bool | None = None,
        limit: int = 50,
    ) -> JSONResponse:
        store = app.state.services.artifacts
        if store is None:
            return _unavailable("artifacts")
        return JSONResponse(
            await artifacts_list(
                store,
                project_id=project_id,
                kind=kind,
                pinned=pinned,
                limit=max(1, min(limit, 200)),  # floored: LIMIT -1 in SQLite means "no limit"
            )
        )

    @app.get("/api/artifacts/{artifact_id}")
    async def artifact_detail(artifact_id: int) -> JSONResponse:
        from jarvis.ui.readmodels import serialize_artifact

        store = app.state.services.artifacts
        if store is None:
            return _unavailable("artifacts")
        art = await store.get(artifact_id)
        if art is None:
            return _deny(status.HTTP_404_NOT_FOUND, "not found")
        return JSONResponse(serialize_artifact(art))

    @app.get("/api/artifacts/{artifact_id}/content")
    async def artifact_content(artifact_id: int) -> Response:
        # STRICT: registered id only; ArtifactStore.content_path re-confines to a managed root +
        # refuses sensitive paths; quarantined artifacts + non-local (external-uri) artifacts are
        # never served; text/image allowlist only; size-capped. (Adversarially tested.)
        store = app.state.services.artifacts
        if store is None:
            return _unavailable("artifacts")
        art = await store.get(artifact_id)
        if art is None or art.sensitivity == "quarantined":
            return _deny(status.HTTP_404_NOT_FOUND, "not found")
        try:
            path = store.content_path(art)
        except ArtifactPathError:
            return _deny(status.HTTP_404_NOT_FOUND, "not found")
        if path is None or not path.is_file():
            return _deny(status.HTTP_404_NOT_FOUND, "not found")
        media_type = _ARTIFACT_MEDIA.get(path.suffix.lower())
        if media_type is None:
            return _deny(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, "unsupported type")
        if path.stat().st_size > config.limits.max_read_bytes:
            return _deny(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "too large")
        return FileResponse(path, media_type=media_type)

    @app.post("/api/artifacts/{artifact_id}/pin")
    async def artifacts_pin(artifact_id: int, request: Request) -> JSONResponse:
        store = app.state.services.artifacts
        if store is None:
            return _unavailable("artifacts")
        body = await request.json()
        ok = await store.set_pinned(artifact_id, bool(body.get("pinned", True)))
        return JSONResponse({"ok": ok})

    @app.post("/api/artifacts/{artifact_id}/label")
    async def artifacts_label(artifact_id: int, request: Request) -> JSONResponse:
        store = app.state.services.artifacts
        if store is None:
            return _unavailable("artifacts")
        body = await request.json()
        labels = body.get("labels", [])
        if not isinstance(labels, list):
            return JSONResponse({"ok": False, "message": "labels must be a list"}, status_code=400)
        ok = await store.set_labels(artifact_id, [str(x) for x in labels])
        return JSONResponse({"ok": ok})

    # --- Phase 12: the outward-write approval queue + journal + execute ----------------------
    # The write tools only PROPOSE (persist a previewed intent). These human-only routes are the
    # ONLY path that executes an outward write — reached from the authenticated, loopback, Origin-
    # checked UI, never from a model tool / Auto / unattended run. Execute runs the STORED request.

    @app.get("/api/intents")
    async def intents_index(
        request: Request, project_id: int | None = None, limit: int = 50
    ) -> JSONResponse:
        from jarvis.ui.readmodels import intents_queue

        store = app.state.services.intents
        if store is None:
            return _unavailable("intents")
        workspace = _workspace_for(request)
        if app.state.workspaces is not None and workspace is None:
            return _workspace_required()
        if workspace is not None:
            if project_id is not None and project_id != workspace.context.project_id:
                return _deny(status.HTTP_404_NOT_FOUND, "not found")
            project_id = workspace.context.project_id
        return JSONResponse(await intents_queue(store, project_id=project_id, limit=limit))

    @app.get("/api/connector-writes")
    async def connector_writes(request: Request, limit: int = 50) -> JSONResponse:
        """Read-only, metadata-only evidence of completed outward connector writes.

        Under the live-workspace composition, the browser never chooses a project id: a project
        workspace sees only its journal rows, while the global workspace retains the global audit
        view. The legacy single-session composition keeps its existing unscoped read behavior.
        """
        journal = app.state.services.write_journal
        if journal is None:
            return _unavailable("connector writes")
        workspace = _workspace_for(request)
        if app.state.workspaces is not None and workspace is None:
            return _workspace_required()
        project_id = workspace.context.project_id if workspace is not None else None
        return JSONResponse(
            await connector_write_history(journal, project_id=project_id, limit=limit)
        )

    @app.get("/api/intents/{intent_id}")
    async def intent_detail(intent_id: int, request: Request) -> JSONResponse:
        from jarvis.ui.readmodels import serialize_intent

        store = app.state.services.intents
        if store is None:
            return _unavailable("intents")
        workspace = _workspace_for(request)
        if app.state.workspaces is not None and workspace is None:
            return _workspace_required()
        intent = await store.get(intent_id)
        if intent is None or not _workspace_can_access_project(intent.project_id, workspace):
            return _deny(status.HTTP_404_NOT_FOUND, "not found")
        return JSONResponse(serialize_intent(intent))

    @app.post("/api/intents/{intent_id}/approve")
    async def intent_approve(intent_id: int, request: Request) -> JSONResponse:
        from jarvis.actions.executor import WriteExecutor
        from jarvis.actions.intents import IntentState

        svc = app.state.services
        if svc.intents is None or svc.write_journal is None:
            return _unavailable("intents")
        workspace = _workspace_for(request)
        if app.state.workspaces is not None and workspace is None:
            return _workspace_required()
        intent = await svc.intents.get(intent_id)
        if intent is None or not _workspace_can_access_project(intent.project_id, workspace):
            return _deny(status.HTTP_404_NOT_FOUND, "not found")
        if intent.state is not IntentState.PREVIEWED:
            return JSONResponse(
                {"ok": False, "message": f"intent is {intent.state.value}, not pending"},
                status_code=status.HTTP_409_CONFLICT,
            )
        await svc.intents.approve(intent_id)
        client = svc.connectors.google if svc.connectors is not None else None
        executor = WriteExecutor(client, svc.intents, svc.write_journal, artifacts=svc.artifacts)
        result = await executor.execute(intent_id)
        return JSONResponse(
            {
                "ok": result.state.value == "executed",
                "state": result.state.value,
                "error": result.error,
                "link": (result.result or {}).get("link"),
            }
        )

    @app.post("/api/intents/{intent_id}/reject")
    async def intent_reject(intent_id: int, request: Request) -> JSONResponse:
        from jarvis.actions.intents import IntentState

        svc = app.state.services
        if svc.intents is None:
            return _unavailable("intents")
        workspace = _workspace_for(request)
        if app.state.workspaces is not None and workspace is None:
            return _workspace_required()
        intent = await svc.intents.get(intent_id)
        if intent is None or not _workspace_can_access_project(intent.project_id, workspace):
            return _deny(status.HTTP_404_NOT_FOUND, "not found")
        if intent.state not in (IntentState.DRAFT, IntentState.PREVIEWED):
            return JSONResponse(
                {"ok": False, "message": f"intent is {intent.state.value}"},
                status_code=status.HTTP_409_CONFLICT,
            )
        await svc.intents.reject(intent_id)
        return JSONResponse({"ok": True})

    @app.post("/api/intents/{intent_id}/undo")
    async def intent_undo(intent_id: int, request: Request) -> JSONResponse:
        from jarvis.actions.executor import WriteExecutor
        from jarvis.connectors.base import ConnectorError

        svc = app.state.services
        if svc.intents is None or svc.write_journal is None:
            return _unavailable("intents")
        workspace = _workspace_for(request)
        if app.state.workspaces is not None and workspace is None:
            return _workspace_required()
        intent = await svc.intents.get(intent_id)
        if intent is None or not _workspace_can_access_project(intent.project_id, workspace):
            return _deny(status.HTTP_404_NOT_FOUND, "not found")
        client = svc.connectors.google if svc.connectors is not None else None
        executor = WriteExecutor(client, svc.intents, svc.write_journal, artifacts=svc.artifacts)
        try:
            result = await executor.undo(intent_id)
        except KeyError:
            return _deny(status.HTTP_404_NOT_FOUND, "not found")
        except (ValueError, ConnectorError) as exc:
            return JSONResponse(
                {"ok": False, "message": str(exc)}, status_code=status.HTTP_400_BAD_REQUEST
            )
        return JSONResponse({"ok": True, "state": result.state.value})

    # --- Phase 16: the ONE attention queue (Notification Center) ---------------------------
    @app.get("/api/attention")
    async def attention_index(
        request: Request, project_id: int | None = None, limit: int = 200
    ) -> JSONResponse:
        # The unified open queue over live approvals + write-intents + graph suggestions + durable
        # attention rows. Read-only projection; each item points AT its source's existing route.
        from jarvis.attention.readmodel import attention_queue

        svc = app.state.services
        if svc.attention is None:
            return _unavailable("attention")
        workspace = _workspace_for(request)
        if app.state.workspaces is not None and workspace is None:
            return _workspace_required()
        pid = project_id
        if workspace is not None:
            if pid is not None and pid != workspace.context.project_id:
                return JSONResponse(
                    {"ok": False, "message": "wrong project scope"}, status_code=404
                )
            pid = workspace.context.project_id
        elif pid is None and app.state.projects is not None:
            pid = app.state.projects.current().project_id
        data = await attention_queue(
            attention=svc.attention,
            intents=svc.intents,
            graph=svc.graph,
            approvals=app.state.approvals,
            approval_context=workspace.context if workspace is not None else None,
            project_id=pid,
            limit=limit,
        )
        return JSONResponse(data)

    @app.post("/api/attention/{item_id}/resolve")
    async def attention_resolve(item_id: int, request: Request) -> JSONResponse:
        # Metadata-only state flip on a durable attention row: done | dismiss | snooze | expire.
        # This grants NO new authority — a proposal's ACCEPT path is the human on its source's
        # existing gated route, never here. Only attention_items rows resolve through this route;
        # intents/suggestions/gate items keep their own approve/reject routes.
        svc = app.state.services
        if svc.attention is None:
            return _unavailable("attention")
        workspace = _workspace_for(request)
        if app.state.workspaces is not None and workspace is None:
            return _workspace_required()
        existing = await svc.attention.get(item_id)
        if existing is None or not _workspace_can_access_project(existing.project_id, workspace):
            return _deny(status.HTTP_404_NOT_FOUND, "not found")
        body = await request.json()
        action = str(body.get("action", ""))
        try:
            item = await svc.attention.resolve(item_id, action, until=body.get("until"))
        except KeyError:
            return _deny(status.HTTP_404_NOT_FOUND, "not found")
        except ValueError as exc:  # unknown action / illegal transition / snooze needs until
            return JSONResponse(
                {"ok": False, "message": str(exc)}, status_code=status.HTTP_400_BAD_REQUEST
            )
        return JSONResponse({"ok": True, "state": item.state.value})

    @app.get("/api/search")
    async def global_search(
        request: Request, q: str = "", project_id: int | None = None, limit: int = 40
    ) -> JSONResponse:
        store = app.state.services.artifacts  # shares the app's single connection
        if store is None:
            return _unavailable("search")
        workspace = _workspace_for(request)
        if app.state.workspaces is not None and workspace is None:
            return _workspace_required()
        if workspace is not None:
            # Search is evidence access. The live workspace, never a query parameter, owns the
            # project boundary; global workspaces intentionally retain the aggregate search view.
            if project_id is not None and project_id != workspace.context.project_id:
                return _deny(status.HTTP_404_NOT_FOUND, "not found")
            project_id = workspace.context.project_id
        capped = max(1, min(limit, 100))
        if project_id is None:
            results = await _federated_search(store.db, q, limit=capped)
        else:
            results = await _federated_search(store.db, q, project_id=project_id, limit=capped)
        return JSONResponse({"results": results})

    @app.get("/api/workspace/{project_id}")
    async def workspace(project_id: int, request: Request) -> JSONResponse:
        # Aggregate Overview for one project (metadata only; degrades if a service is absent).
        workspace = _workspace_for(request)
        if app.state.workspaces is not None and workspace is None:
            return _workspace_required()
        if workspace is not None and project_id != workspace.context.project_id:
            return _deny(status.HTTP_404_NOT_FOUND, "not found")
        return JSONResponse(await workspace_overview(app.state.services, project_id))

    @app.get("/api/workspace/{project_id}/activity")
    async def workspace_activity(project_id: int, request: Request) -> JSONResponse:
        # Derived, metadata-only project activity feed (artifacts/runs/chats). Read-only.
        workspace = _workspace_for(request)
        if app.state.workspaces is not None and workspace is None:
            return _workspace_required()
        if workspace is not None and project_id != workspace.context.project_id:
            return _deny(status.HTTP_404_NOT_FOUND, "not found")
        return JSONResponse(await activity_feed(app.state.services, project_id))

    @app.get("/api/workspace/{project_id}/office")
    async def workspace_office(project_id: int, request: Request) -> JSONResponse:
        # Phase 14: the AI Team Office projection (teams→rooms→nodes, head, stages, live run +
        # per-member overlay, recent runs, activity feed). Read-only ASSEMBLER over existing read
        # models — presence/metadata/summaries only, never a body or key value.
        workspace = _workspace_for(request)
        if app.state.workspaces is not None and workspace is None:
            return _workspace_required()
        if workspace is not None and project_id != workspace.context.project_id:
            return JSONResponse({"ok": False, "message": "wrong project scope"}, status_code=404)
        return JSONResponse(await office_overview(config, app.state.services, project_id))

    @app.get("/api/workspace/{project_id}/graph")
    async def workspace_graph(
        project_id: int,
        request: Request,
        focus: str | None = None,
        depth: int = 1,
        kinds: str | None = None,
        trust: str | None = None,
        since: str | None = None,
        limit: int = 300,
        view: str = "structure",
    ) -> JSONResponse:
        # Phase 15: the project-scoped memory-graph subgraph (nodes+edges+counts). READ-ONLY,
        # clamped (depth<=6, limit<=300), bodies-free. Degrades to an empty graph if unavailable.
        workspace = _workspace_for(request)
        if app.state.workspaces is not None and workspace is None:
            return _workspace_required()
        if workspace is not None and project_id != workspace.context.project_id:
            return JSONResponse({"ok": False, "message": "wrong project scope"}, status_code=404)
        svc = app.state.services
        if svc.graph is None:
            return JSONResponse(
                {
                    "project_id": project_id,
                    "nodes": [],
                    "edges": [],
                    "counts": {"by_kind": {}, "by_trust": {}},
                    "truncated": False,
                }
            )
        focus_ep = None
        if focus and ":" in focus:
            fk, fid = focus.split(":", 1)
            focus_ep = (fk, fid)
        return JSONResponse(
            await subgraph(
                svc.graph,
                project_id,
                focus=focus_ep,
                depth=depth,
                kinds=set(kinds.split(",")) if kinds else None,
                trust=set(trust.split(",")) if trust else None,
                since=since,
                limit=limit,
                view="dependencies" if view == "dependencies" else "structure",
            )
        )

    @app.get("/api/graph/node/{kind}/{ref_id:path}")
    async def graph_node(kind: str, ref_id: str, request: Request) -> JSONResponse:
        # One node's card + capped neighbors (ref_id is a path converter so wiki paths work).
        svc = app.state.services
        if svc.graph is None:
            return JSONResponse({"detail": "graph unavailable"}, status_code=404)
        workspace = _workspace_for(request)
        if app.state.workspaces is not None and workspace is None:
            return _workspace_required()
        if workspace is not None:
            # Node ids are not authority.  A card can be inspected only when it participates in
            # the active workspace's project graph; this also covers derived folder endpoints.
            edges = await svc.graph.list_edges(
                project_id=workspace.context.project_id, include_global=False
            )
            if (kind, ref_id) not in {
                (edge.src_kind, edge.src_id) for edge in edges
            } | {
                (edge.dst_kind, edge.dst_id) for edge in edges
            }:
                return _deny(status.HTTP_404_NOT_FOUND, "not found")
        card = await node_card(svc.graph, kind, ref_id)
        if card is None:
            return JSONResponse({"detail": "node not found"}, status_code=404)
        return JSONResponse(card)

    @app.get("/api/graph/suggestions")
    async def graph_suggestions(project_id: int, request: Request) -> JSONResponse:
        # The project's QUARANTINED review queue (bodies-free previews + evidence pointers).
        svc = app.state.services
        if svc.graph is None:
            return JSONResponse(
                {"project_id": project_id, "suggestions": [], "counts": {"by_trust": {}}}
            )
        workspace = _workspace_for(request)
        if app.state.workspaces is not None and workspace is None:
            return _workspace_required()
        if workspace is not None and project_id != workspace.context.project_id:
            return _deny(status.HTTP_404_NOT_FOUND, "not found")
        return JSONResponse(await suggestions_view(svc.graph, project_id))

    @app.post("/api/graph/suggestions/{suggestion_id}/approve")
    async def graph_suggestion_approve(suggestion_id: int, request: Request) -> JSONResponse:
        # The ONLY door from a quarantined proposal to durable graph truth — the Vault review
        # pattern (idempotent claim-then-materialize). New mutation route (pin 35->37).
        svc = app.state.services
        if svc.graph is None:
            return JSONResponse({"ok": False, "reason": "graph unavailable"}, status_code=404)
        workspace = _workspace_for(request)
        if app.state.workspaces is not None and workspace is None:
            return _workspace_required()
        if workspace is not None:
            # A suggestion id is not authority. Match the review queue's P + global scope, so a
            # live browser workspace cannot approve another project's quarantined proposal.
            suggestion = await svc.graph.get_suggestion(suggestion_id)
            if suggestion is None or (
                suggestion.project_id is not None
                and suggestion.project_id != workspace.context.project_id
            ):
                return _deny(status.HTTP_404_NOT_FOUND, "not found")
        return JSONResponse(await graph_approve(svc.graph, suggestion_id, resolved_by="user"))

    @app.post("/api/graph/suggestions/{suggestion_id}/reject")
    async def graph_suggestion_reject(suggestion_id: int, request: Request) -> JSONResponse:
        svc = app.state.services
        if svc.graph is None:
            return JSONResponse({"ok": False, "reason": "graph unavailable"}, status_code=404)
        workspace = _workspace_for(request)
        if app.state.workspaces is not None and workspace is None:
            return _workspace_required()
        if workspace is not None:
            # See approve: the active workspace may resolve only its own or global suggestions.
            suggestion = await svc.graph.get_suggestion(suggestion_id)
            if suggestion is None or (
                suggestion.project_id is not None
                and suggestion.project_id != workspace.context.project_id
            ):
                return _deny(status.HTTP_404_NOT_FOUND, "not found")
        return JSONResponse(await graph_reject(svc.graph, suggestion_id, resolved_by="user"))

    @app.get("/api/graph/search")
    async def graph_search(q: str, project_id: int | None = None, limit: int = 20) -> JSONResponse:
        # Unified semantic + keyword + graph search. Read-only, quarantine-aware. Semantic layer
        # needs the (priced) embedder; absent/unpriced ⇒ it degrades to FTS-only (never errors).
        svc = app.state.services
        if svc.graph is None:
            return JSONResponse({"query": q, "results": [], "count": 0})
        embedder = None
        if svc.embedder is not None:
            with contextlib.suppress(Exception):
                pricing = load_pricing(config.root / "config" / "pricing.yaml")
                embedder = CostAwareEmbedder(svc.embedder, pricing)
        return JSONResponse(
            await unified_search(svc.graph, embedder, q, project_id=project_id, limit=limit)
        )

    @app.post("/api/orchestration/run")
    async def orchestration_run(request: Request) -> JSONResponse:
        # Launch a team+workflow orchestration run. The click authorizes the fan-out; the engine
        # re-checks the budget reservation itself. Returns 202 on launch, 200 + needs_confirmation
        # when the worst case crosses the confirm threshold, 409 if a run is already in flight.
        orch = app.state.orchestrator
        if orch is None:
            return _unavailable("orchestration")
        workspace = _workspace_for(request)
        if app.state.workspaces is not None and workspace is None:
            return _workspace_required()
        body = await request.json()
        if workspace is not None:
            async with app.state.workspaces.transition_lock:
                result, code = await orch.start(
                    team_id=str(body.get("team", "")),
                    workflow_id=str(body.get("workflow", "")),
                    task=str(body.get("task", "")),
                    budget_usd=body.get("budget_usd"),
                    confirmed=bool(body.get("confirmed", False)),
                    execution_context=workspace.context,
                    project=workspace.project,
                )
        else:
            result, code = await orch.start(
                team_id=str(body.get("team", "")),
                workflow_id=str(body.get("workflow", "")),
                task=str(body.get("task", "")),
                budget_usd=body.get("budget_usd"),
                confirmed=bool(body.get("confirmed", False)),
            )
        return JSONResponse(result, status_code=code)

    @app.post("/api/orchestration/{run_id}/cancel")
    async def orchestration_cancel(run_id: int, request: Request) -> JSONResponse:
        orch = app.state.orchestrator
        if orch is None:
            return _unavailable("orchestration")
        workspace = _workspace_for(request)
        if app.state.workspaces is not None and workspace is None:
            return _workspace_required()
        return JSONResponse(
            {
                "cancelled": orch.cancel(
                    run_id, execution_context=workspace.context if workspace else None
                )
            }
        )

    @app.post("/api/orchestration/{run_id}/resume")
    async def orchestration_resume(run_id: int, request: Request) -> JSONResponse:
        # A human explicitly re-enters the original brief before the engine can claim the
        # single pre-execution checkpoint.  It never replays a task body or a partial writer.
        orch = app.state.orchestrator
        if orch is None:
            return _unavailable("orchestration")
        workspace = _workspace_for(request)
        if app.state.workspaces is not None and workspace is None:
            return _workspace_required()
        body = await request.json()
        if workspace is not None:
            async with app.state.workspaces.transition_lock:
                result, code = await orch.resume(
                    run_id,
                    task=str(body.get("task", "")),
                    execution_context=workspace.context,
                    project=workspace.project,
                )
        else:
            result, code = await orch.resume(run_id, task=str(body.get("task", "")))
        return JSONResponse(result, status_code=code)

    # --- voice: status + push-to-talk + meeting capture (unreviewed source) ------------

    @app.get("/api/voice/status")
    async def voice_status(request: Request) -> JSONResponse:
        workspace = _workspace_for(request)
        if app.state.workspaces is not None and workspace is None:
            return _workspace_required()
        v = workspace.voice if workspace is not None else app.state.voice
        if v is not None:
            return JSONResponse(v.status())
        # Off: report WHY in plain language (the Talk button hides + shows this reason).
        return JSONResponse(
            {
                "enabled": False,
                "listening": "idle",
                "meeting": "idle",
                "playback": False,
                "stt": config.voice.stt_provider,
                "tts": config.voice.tts_provider,
                "reason": (
                    "Voice is off — set voice.enabled: true in settings.yaml (and install the "
                    "voice extra)."
                ),
            }
        )

    @app.post("/api/voice/listen")
    async def voice_listen(request: Request) -> JSONResponse:
        workspace = _workspace_for(request)
        if app.state.workspaces is not None and workspace is None:
            return _workspace_required()
        v = workspace.voice if workspace is not None else app.state.voice
        if v is None or v.listener is None:
            return _unavailable("voice")
        if workspace is None:
            heard = await v.listen_once()
        else:
            try:
                async with app.state.workspaces.voice_activity(workspace):
                    with bind_execution_context(workspace.context):
                        heard = await v.listen_once()
            except RuntimeError:
                return JSONResponse({"ok": False, "message": "busy"}, status_code=409)
        # server-mic fallback: one push-to-talk utterance → one turn
        return JSONResponse({"ok": True, "heard": heard})

    @app.post("/api/voice/utterance")
    async def voice_utterance(request: Request) -> JSONResponse:
        # Phase 15.5: a BROWSER-captured utterance (raw audio body) → the SAME voice session the
        # server-mic path uses (STT → framed untrusted turn → safe caption) through the unchanged
        # VoiceApprover. The screen stays the ONLY approval surface; no new authority.
        workspace = _workspace_for(request)
        if app.state.workspaces is not None and workspace is None:
            return _workspace_required()
        v = workspace.voice if workspace is not None else app.state.voice
        if v is None or v.listener is None:
            return _unavailable("voice")
        audio = await request.body()
        if not audio:
            return JSONResponse({"ok": False, "message": "empty audio"}, status_code=400)
        mode = request.query_params.get("mode", "conversation")
        if mode not in {"conversation", "dictation"}:
            return JSONResponse({"ok": False, "message": "invalid voice mode"}, status_code=422)
        if mode == "dictation":
            # Review-first dictation: the same authenticated, scoped audio surface performs STT
            # only. It never creates a model turn, tool call, approval, renderer event, or TTS.
            if workspace is None:
                transcript = await v.transcribe_utterance(audio)
            else:
                try:
                    async with app.state.workspaces.voice_activity(workspace):
                        with bind_execution_context(workspace.context):
                            transcript = await v.transcribe_utterance(audio)
                except RuntimeError:
                    return JSONResponse({"ok": False, "message": "busy"}, status_code=409)
            return JSONResponse({"ok": True, "transcript": transcript})
        if workspace is None:
            ran = await v.handle_utterance(audio)
        else:
            try:
                async with app.state.workspaces.voice_activity(workspace):
                    with bind_execution_context(workspace.context):
                        ran = await v.handle_utterance(audio)
            except RuntimeError:
                return JSONResponse({"ok": False, "message": "busy"}, status_code=409)
        return JSONResponse({"ok": True, "ran": ran})

    @app.post("/api/voice/tts")
    async def voice_tts(request: Request) -> Response:
        # Phase 15.5: synthesize the SAFE caption for browser playback. The text is masked + capped
        # server-side before it reaches TTS, so a raw answer / payload / secret can never be voiced.
        # Local/subtitle TTS ⇒ 204 (no audio; the browser keeps captions as text).
        workspace = _workspace_for(request)
        if app.state.workspaces is not None and workspace is None:
            return _workspace_required()
        v = workspace.voice if workspace is not None else app.state.voice
        if v is None or v.tts is None:
            return _unavailable("voice")
        text = str((await request.json()).get("text", ""))
        audio = await v.synthesize_caption(text)
        if not audio:
            return Response(status_code=204)
        return _secure(Response(content=audio, media_type="audio/mpeg"), no_store=True)

    @app.post("/api/voice/meeting")
    async def voice_meeting(request: Request) -> JSONResponse:
        workspace = _workspace_for(request)
        if app.state.workspaces is not None and workspace is None:
            return _workspace_required()
        v = workspace.voice if workspace is not None else app.state.voice
        if v is None or v.meeting is None:
            return _unavailable("voice")
        body = await request.json()
        if workspace is None:
            result = await v.capture_meeting(title=body.get("title"))
        else:
            try:
                async with app.state.workspaces.voice_activity(workspace):
                    with bind_execution_context(workspace.context):
                        result = await v.capture_meeting(title=body.get("title"))
            except RuntimeError:
                return JSONResponse({"ok": False, "message": "busy"}, status_code=409)
        # A meeting is untrusted content: it lands UNREVIEWED, never an auto-action.
        status = getattr(result, "review_status", None) if result is not None else None
        return JSONResponse({"ok": result is not None, "review_status": status})

    @app.websocket("/ws")
    async def ws(websocket: WebSocket) -> None:
        # HTTP middleware does not run for WS, so authenticate the handshake here: Host
        # (anti-rebinding), Origin (anti-CSRF), and a valid session cookie — else refuse.
        if (
            not host_allowed(websocket.headers.get("host", ""))
            or not origin_allowed(
                websocket.headers.get("origin", ""),
                host_header=websocket.headers.get("host", ""),
                scheme=websocket.url.scheme,
            )
            or not auth.is_valid_session(websocket.cookies.get(SESSION_COOKIE))
        ):
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
            return
        owner_session = websocket.cookies.get(SESSION_COOKIE)
        await websocket.accept()
        conn = connections.register(websocket, owner_session=owner_session)
        try:
            await websocket.send_json(
                {"type": "hello", "heartbeat_seconds": connections.heartbeat_seconds}
            )
            while True:
                try:
                    msg = await websocket.receive_json()
                except ValueError:
                    continue  # skip a malformed frame; don't tear down the socket
                if msg.get("type") == "hello":
                    _handle_ws_message(connections, conn, msg)
                    registry = app.state.workspaces
                    if registry is not None and owner_session is not None:
                        workspace = await registry.attach(
                            conn,
                            owner_session=owner_session,
                            requested_workspace_id=msg.get("workspace_id"),
                        )
                        await websocket.send_json(
                            {
                                "type": "workspace",
                                "workspace_id": workspace.workspace_id,
                                **workspace.context.to_wire(),
                            }
                        )
                    continue
                # approval_shown: the client proves the modal is on screen ⇒ mint a
                # single-use nonce bound to THIS live connection (amendment 3/4).
                if msg.get("type") == "approval_shown":
                    did = msg.get("decision_id")
                    nonce = await approvals.mint_nonce(did, conn) if did else None
                    if nonce is not None:
                        pending = approvals.get(did)
                        await websocket.send_json(
                            {
                                "type": "approval_nonce",
                                "decision_id": did,
                                "nonce": nonce,
                                **(pending.context.to_wire() if pending is not None else {}),
                            }
                        )
                    continue
                # A parked scheduler run has no attended source session.  Its nonce is still
                # minted only after the exact continuation dialog is visible on this *live*
                # authenticated socket, and is scoped to the server-owned workspace project.
                if msg.get("type") == "parked_task_approval_shown":
                    try:
                        run_id = int(msg.get("run_id"))
                    except (TypeError, ValueError):
                        continue
                    nonce = await app.state.parked_task_approvals.mint_nonce(run_id, conn)
                    if nonce is not None:
                        await websocket.send_json(
                            {
                                "type": "parked_task_approval_nonce",
                                "run_id": run_id,
                                "nonce": nonce,
                            }
                        )
                    continue
                _handle_ws_message(connections, conn, msg)
        except WebSocketDisconnect:
            pass
        finally:
            # Drop the connection AND invalidate its nonces — a click from a since-dead or
            # reconnected client can never resolve an approval (replay-proof).
            connections.drop(conn)
            approvals.invalidate_connection(conn)
            app.state.parked_task_approvals.invalidate_connection(conn)

    return app


def _handle_ws_message(connections: ConnectionManager, conn: Connection, msg: dict) -> None:
    """Dispatch one client WS frame: heartbeat (liveness), hello (initial surfaces), or a
    surface mount/unmount. Unknown types are ignored."""
    kind = msg.get("type")
    if kind == "heartbeat":
        connections.touch(conn)
    elif kind == "hello":
        conn.surfaces = set(msg.get("surfaces", []))
        connections.touch(conn)
    elif kind == "surface":
        surface = msg.get("surface")
        if surface:
            connections.set_surface(conn, surface, bool(msg.get("mounted")))
