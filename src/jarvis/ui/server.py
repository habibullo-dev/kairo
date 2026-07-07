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

from pathlib import Path
from typing import TYPE_CHECKING

from fastapi import FastAPI, Request, WebSocket, status
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from starlette.websockets import WebSocketDisconnect

from jarvis.observability import get_logger
from jarvis.permissions import PermissionGate, load_policy
from jarvis.permissions.modes import Mode
from jarvis.tools import Permission
from jarvis.ui.approver import ApprovalManager, UIApprover, UIScreenApprover
from jarvis.ui.auth import SESSION_COOKIE, AuthManager, host_allowed, origin_allowed
from jarvis.ui.connections import Connection, ConnectionManager
from jarvis.ui.gate_api import policy_snapshot, read_today_audit
from jarvis.ui.readmodels import (
    UiServices,
    costs_overview,
    daily_overview,
    hub_status,
    lab_overview,
    list_agent_runs,
    list_memories,
    list_sessions_view,
    list_tasks,
    model_routes_status,
    orchestration_run_detail,
    orchestration_runs_view,
    projects_view,
    services_status,
    session_transcript,
    task_runs,
    teams_catalog,
    vault_lint,
    vault_overview,
    workflows_catalog,
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


def _runner_status(runner: object | None, session: object | None) -> dict:
    """The status-bar view: is the background runner firing, what job is in flight, and is
    an interactive turn running. Read-only; the emergency stop toggles the first two."""
    return {
        "runner_running": bool(runner is not None and runner.is_running),
        "in_flight": getattr(runner, "in_flight", None) if runner is not None else None,
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
    app.state.orchestrator = None  # an OrchestrationController, set by build_ui_app; None ⇒ 503
    # The UI is voice's fail-closed "screen": a VoiceApprover wired to this UIScreenApprover
    # resolves risky voice actions on the authenticated, live, watching Gate surface — or
    # denies. Composed here so the CLI host (Task 9) injects it into the voice VoiceApprover.
    app.state.ui_screen = UIScreenApprover(approvals, connections)
    app.state.config = config

    @app.middleware("http")
    async def guard(request: Request, call_next):  # noqa: ANN001,ANN202 - framework signature
        # 1. Host allowlist FIRST — anti DNS-rebinding (a rebound name still sends its Host).
        if not host_allowed(request.headers.get("host", "")):
            return _deny(status.HTTP_400_BAD_REQUEST, "bad host")
        # 2. Origin check on mutations — anti-CSRF (a cross-site POST carries a foreign Origin).
        if request.method in _MUTATING and not origin_allowed(request.headers.get("origin", "")):
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
            return FileResponse(index)
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
        return FileResponse(target)

    # --- Gate: approvals (the crown jewels) + read-only policy/audit views -------------

    @app.get("/api/approvals")
    async def list_approvals() -> dict:
        return {"pending": [p.to_public() for p in approvals.pending()]}

    @app.post("/api/approvals/{decision_id}/resolve")
    async def resolve_approval(decision_id: str, request: Request) -> JSONResponse:
        # Session + loopback-Origin already enforced by the guard (POST is mutating). The
        # nonce (single-use, bound to a live watching client) is the replay-proof credential.
        body = await request.json()
        ok, message = approvals.resolve(
            decision_id, str(body.get("nonce", "")), str(body.get("action", ""))
        )
        return JSONResponse({"ok": ok, "message": message}, status_code=200 if ok else 409)

    @app.get("/api/gate/policy")
    async def gate_policy() -> dict:
        return policy_snapshot(gate)

    @app.get("/api/audit/today")
    async def audit_today() -> dict:
        return {"events": read_today_audit(config.logs_dir)}

    # --- Command: submit / cancel a turn (events stream over the WS) --------------------

    @app.post("/api/turn")
    async def submit_turn(request: Request) -> JSONResponse:
        if app.state.session is None:
            return JSONResponse({"ok": False, "message": "no session"}, status_code=503)
        body = await request.json()
        text = str(body.get("text", "")).strip()
        if not text:
            return JSONResponse({"ok": False, "message": "empty"}, status_code=400)
        started = app.state.session.submit(text)
        # 409 if a turn is already in flight (one interactive turn at a time, like the REPL).
        return JSONResponse({"ok": started}, status_code=200 if started else 409)

    @app.post("/api/turn/cancel")
    async def cancel_turn() -> dict:
        if app.state.session is None:
            return {"cancelled": False}
        return {"cancelled": app.state.session.cancel()}

    # --- emergency stop: existing brakes only (Ctrl-C parity + runner stop) -------------

    @app.get("/api/runner")
    async def runner_status() -> dict:
        # The status-strip feed: runner/turn state + mode + active project + today's spend +
        # pending approvals + cost-ledger health (A5). One calm surface; all read-only.
        status = _runner_status(app.state.runner, app.state.session)
        modes = app.state.modes
        status["mode"] = modes.current().value if modes is not None else "approval"
        projects = app.state.projects
        cur = projects.current() if projects is not None else None
        status["project"] = {"id": cur.project_id, "name": cur.name} if cur is not None else None
        status["pending_approvals"] = len(app.state.approvals.pending())
        budgets = app.state.services.budgets
        status["today_spend_usd"] = (
            (await budgets.period_spend("day"))["cost_usd"] if budgets is not None else None
        )
        ledger = app.state.services.ledger
        status["ledger_degraded"] = ledger.status()["degraded"] if ledger is not None else False
        return status

    @app.post("/api/budgets")
    async def set_budgets(request: Request) -> JSONResponse:
        # Tighten/loosen the live budget limits for this session (durable changes go in
        # settings.yaml / a project's settings). Only known numeric limit fields are accepted.
        budgets = app.state.services.budgets
        if budgets is None:
            return _unavailable("costs")
        body = await request.json()
        allowed = {
            "soft_warn_usd_per_run",
            "hard_stop_usd_per_run",
            "project_monthly_usd",
            "per_role_max_usd",
            "confirm_above_usd",
            "hourly_rate_usd",
        }
        updates = {k: v for k, v in body.items() if k in allowed}
        try:
            budgets.config = budgets.config.model_copy(update=updates)
        except Exception as exc:  # noqa: BLE001 - a bad value is a 400, not a 500
            return JSONResponse({"ok": False, "message": str(exc)}, status_code=400)
        return JSONResponse({"ok": True, "limits": (await budgets.status())["limits"]})

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

    @app.post("/api/runner/pause")
    async def runner_pause() -> dict:
        # Maps to BackgroundRunner.stop(): finish any in-flight job (never a torn write),
        # then stop firing. Also cancels the in-flight interactive turn. No new authority.
        if app.state.session is not None:
            app.state.session.cancel()
        if app.state.runner is not None and app.state.runner.is_running:
            await app.state.runner.stop()
        return _runner_status(app.state.runner, app.state.session)

    @app.post("/api/runner/resume")
    async def runner_resume() -> dict:
        if app.state.runner is not None and not app.state.runner.is_running:
            app.state.runner.start()
        return _runner_status(app.state.runner, app.state.session)

    @app.get("/api/notices")
    async def notices() -> JSONResponse:
        # Background job/reminder lines (Phase 9). Read-only — NOT a mutating route.
        board = app.state.notices
        tail = board.tail(50) if board is not None else []
        return JSONResponse({"notices": tail})

    # --- read models: Hub / Lab (always available) ------------------------------------

    @app.get("/api/hub")
    async def hub() -> dict:
        connectors = app.state.services.connectors
        ledger = app.state.services.ledger
        return hub_status(
            config,
            connectors=connectors.status() if connectors is not None else None,
            ledger_status=ledger.status() if ledger is not None else None,
        )

    @app.get("/api/daily")
    async def daily() -> JSONResponse:
        # Read-only Daily bootstrap (Phase 9). NOT a mutating route.
        pending = len(app.state.approvals.pending())
        return JSONResponse(
            await daily_overview(
                config, app.state.services, notices=app.state.notices, gate_pending=pending
            )
        )

    @app.get("/api/lab")
    async def lab() -> dict:
        return lab_overview(config)

    # --- read models: Memory / Tasks / Vault / Agents (need host services) ------------
    # These return JSONResponse uniformly (data or a 503) so FastAPI treats them as a
    # passthrough — no response-model inference over a Response|data union.

    @app.get("/api/memory")
    async def memory(type: str | None = None, project_id: int | None = None) -> JSONResponse:
        # project_id scopes to "what Kairo knows about this project" (project + global). Absent
        # ⇒ unscoped (every live memory). The ANY sentinel is the readmodel default.
        from jarvis.ui.readmodels import _MEM_ANY_PROJECT

        svc = app.state.services.memory
        if svc is None:
            return _unavailable("memory")
        scope = _MEM_ANY_PROJECT if project_id is None else project_id
        return JSONResponse(await list_memories(svc, type_filter=type, project_id=scope))

    @app.get("/api/tasks")
    async def tasks(project_id: int | None = None) -> JSONResponse:
        # project_id scopes to a project page (P + global); absent ⇒ every task (the global
        # Tasks screen). A project page passes ?project_id= to filter out other projects.
        from jarvis.scheduler.store import ANY_PROJECT

        svc = app.state.services.tasks
        if svc is None:
            return _unavailable("tasks")
        scope = ANY_PROJECT if project_id is None else project_id
        return JSONResponse(await list_tasks(svc, project_id=scope))

    @app.get("/api/tasks/{task_id}/runs")
    async def tasks_runs(task_id: int) -> JSONResponse:
        svc = app.state.services.tasks
        if svc is None:
            return _unavailable("tasks")
        return JSONResponse(await task_runs(svc, task_id))

    @app.get("/api/vault")
    async def vault() -> JSONResponse:
        svc = app.state.services.knowledge
        if svc is None:
            return _unavailable("knowledge")
        return JSONResponse(await vault_overview(svc))

    @app.get("/api/vault/lint")
    async def vault_lint_route() -> JSONResponse:
        svc = app.state.services.knowledge
        if svc is None:
            return _unavailable("knowledge")
        return JSONResponse(await vault_lint(svc))

    @app.get("/api/agents")
    async def agents() -> JSONResponse:
        svc = app.state.services.run_store
        if svc is None:
            return _unavailable("agents")
        return JSONResponse(await list_agent_runs(svc))

    @app.get("/api/sessions")
    async def sessions_list(query: str | None = None, pinned: bool | None = None) -> JSONResponse:
        svc = app.state.services.sessions
        if svc is None:
            return _unavailable("sessions")
        return JSONResponse(await list_sessions_view(svc, query=query, pinned=pinned))

    @app.get("/api/sessions/{session_id}")
    async def sessions_get(session_id: int) -> JSONResponse:
        svc = app.state.services.sessions
        if svc is None:
            return _unavailable("sessions")
        return JSONResponse(await session_transcript(svc, session_id))

    @app.get("/api/projects")
    async def projects_list() -> JSONResponse:
        svc = app.state.projects
        if svc is None:
            return _unavailable("projects")
        return JSONResponse(await projects_view(svc))

    @app.get("/api/costs")
    async def costs(project_id: int | None = None) -> JSONResponse:
        budgets = app.state.services.budgets
        if budgets is None:
            return _unavailable("costs")
        return JSONResponse(await costs_overview(budgets, project_id=project_id))

    # --- Studio (orchestration): catalog + runs + estimate (all read-only) -------------

    @app.get("/api/studio")
    async def studio() -> JSONResponse:
        # The Studio bootstrap: team profiles + workflow templates (code constants) + service
        # availability + model routes — all presence/metadata only, no key value ever. Always
        # available (pure over config + constants); the run mutations are gated separately.
        projects = app.state.projects
        proj_services = None  # per-project narrowing could pass the project's enabled subset
        return JSONResponse(
            {
                "teams": teams_catalog(),
                "workflows": workflows_catalog(),
                "services": services_status(config, project_services=proj_services),
                "model_routes": model_routes_status(config),
                "active_project_id": (
                    projects.current().project_id if projects is not None else None
                ),
                "busy": bool(app.state.orchestrator is not None and app.state.orchestrator.busy),
            }
        )

    @app.get("/api/orchestration")
    async def orchestration_list(project_id: int | None = None) -> JSONResponse:
        store = app.state.services.orchestration
        if store is None:
            return _unavailable("orchestration")
        return JSONResponse(await orchestration_runs_view(store, project_id=project_id))

    @app.get("/api/orchestration/estimate")
    async def orchestration_estimate(
        team: str, workflow: str, task: str = "", budget_usd: float | None = None
    ) -> JSONResponse:
        # A GET (no state change) so the two-step confirm preview never touches the mutation
        # closed-set. Returns cost metadata only.
        orch = app.state.orchestrator
        if orch is None:
            return _unavailable("orchestration")
        result = await orch.estimate(team, workflow, task=task, budget_usd=budget_usd)
        return JSONResponse(result, status_code=200 if result.get("ok") else 400)

    @app.get("/api/orchestration/{run_id}")
    async def orchestration_detail(run_id: int) -> JSONResponse:
        store = app.state.services.orchestration
        if store is None:
            return _unavailable("orchestration")
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
        active_pid = projects.current().project_id if projects is not None else None
        try:
            result = await svc.ingest(
                path=path, url=url, text=text, title=title, created_by="user", project_id=active_pid
            )
        except Exception as exc:
            return JSONResponse({"ok": False, "message": str(exc)}, status_code=400)
        return JSONResponse({"ok": True, "action": result.action, "source_id": result.source_id})

    @app.post("/api/digest/run")
    async def digest_run() -> JSONResponse:
        # "Run digest now" — deterministic collectors + one tool-less summarize, then UI/DB
        # delivery. 503 if not composed; 409 if a turn is in flight (same busy contract as
        # /api/turn, since the digest makes a model call).
        run_now = getattr(app.state, "run_digest_now", None)
        if run_now is None:
            return _unavailable("digest")
        if app.state.session is not None and app.state.session.busy:
            return JSONResponse({"ok": False, "message": "busy"}, status_code=409)
        outcome = await run_now()
        return JSONResponse({"ok": True, "summary": outcome.text})

    @app.post("/api/tasks/{task_id}/cancel")
    async def tasks_cancel(task_id: int) -> JSONResponse:
        svc = app.state.services.tasks
        if svc is None:
            return _unavailable("tasks")
        cancelled = await svc.cancel(task_id)
        return JSONResponse({"ok": cancelled is not None})

    @app.post("/api/memory/{memory_id}/forget")
    async def memory_forget(memory_id: int) -> JSONResponse:
        svc = app.state.services.memory
        if svc is None:
            return _unavailable("memory")
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
        body = await request.json()
        content = str(body.get("content", "")).strip()
        if not content:
            return JSONResponse({"ok": False, "message": "content required"}, status_code=400)
        mem_type = body.get("type") if body.get("type") in _MEMORY_TYPES else "fact"
        projects = app.state.projects
        pid = projects.current().project_id if projects is not None else None
        result = await svc.remember(content, mem_type, source="user", project_id=pid)
        return JSONResponse({"ok": True, "id": result.memory_id, "action": result.action})

    @app.post("/api/tasks/create")
    async def tasks_create(request: Request) -> JSONResponse:
        # Human-authority task/reminder creation (the promote-to-task target). created_by=user.
        svc = app.state.services.tasks
        if svc is None:
            return _unavailable("tasks")
        body = await request.json()
        projects = app.state.projects
        pid = projects.current().project_id if projects is not None else None
        try:
            task = await svc.schedule(
                kind=str(body.get("kind", "reminder")),
                title=str(body.get("title", "")).strip() or "reminder",
                payload=str(body.get("payload", "")),
                schedule_kind=str(body.get("schedule_kind", "once")),
                schedule_spec=str(body.get("schedule_spec", "")),
                created_by="user",
                project_id=pid,  # a promoted task belongs to the active project
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
        body = await request.json()
        ok = await svc.set_pinned(session_id, bool(body.get("pinned", True)))
        return JSONResponse({"ok": ok})

    @app.post("/api/sessions/{session_id}/resume")
    async def sessions_resume(session_id: int) -> JSONResponse:
        # Load a past chat into the live UI session (mirrors REPL --resume). 409 if a turn is
        # in flight (the loop state must not change mid-turn).
        if app.state.session is None:
            return _unavailable("sessions")
        resumed = await app.state.session.resume(session_id)
        return JSONResponse({"ok": resumed}, status_code=200 if resumed else 409)

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
        body = await request.json()
        fields = {k: body[k] for k in ("name", "description", "color", "icon") if k in body}
        try:
            ok = await svc.store.update(
                project_id, repos=body.get("repos"), settings=body.get("settings"), **fields
            )
        except ValueError as exc:
            return JSONResponse({"ok": False, "message": str(exc)}, status_code=400)
        return JSONResponse({"ok": ok})

    @app.post("/api/projects/{project_id}/archive")
    async def projects_archive(project_id: int) -> JSONResponse:
        svc = app.state.projects
        if svc is None:
            return _unavailable("projects")
        archived = await svc.store.archive(project_id)
        # If the active project was archived, drop back to global scope + fresh chat.
        if archived and svc.current().project_id == project_id:
            await svc.activate(None)
            if app.state.session is not None:
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

    @app.post("/api/orchestration/run")
    async def orchestration_run(request: Request) -> JSONResponse:
        # Launch a team+workflow orchestration run. The click authorizes the fan-out; the engine
        # re-checks the budget reservation itself. Returns 202 on launch, 200 + needs_confirmation
        # when the worst case crosses the confirm threshold, 409 if a run is already in flight.
        orch = app.state.orchestrator
        if orch is None:
            return _unavailable("orchestration")
        body = await request.json()
        result, code = await orch.start(
            team_id=str(body.get("team", "")),
            workflow_id=str(body.get("workflow", "")),
            task=str(body.get("task", "")),
            budget_usd=body.get("budget_usd"),
            confirmed=bool(body.get("confirmed", False)),
        )
        return JSONResponse(result, status_code=code)

    @app.post("/api/orchestration/{run_id}/cancel")
    async def orchestration_cancel(run_id: int) -> JSONResponse:
        orch = app.state.orchestrator
        if orch is None:
            return _unavailable("orchestration")
        return JSONResponse({"cancelled": orch.cancel(run_id)})

    # --- voice: status + push-to-talk + meeting capture (unreviewed source) ------------

    @app.get("/api/voice/status")
    async def voice_status() -> dict:
        return app.state.voice.status() if app.state.voice is not None else {"enabled": False}

    @app.post("/api/voice/listen")
    async def voice_listen() -> JSONResponse:
        v = app.state.voice
        if v is None or v.listener is None:
            return _unavailable("voice")
        heard = await v.listen_once()  # one push-to-talk utterance → one turn
        return JSONResponse({"ok": True, "heard": heard})

    @app.post("/api/voice/meeting")
    async def voice_meeting(request: Request) -> JSONResponse:
        v = app.state.voice
        if v is None or v.meeting is None:
            return _unavailable("voice")
        body = await request.json()
        result = await v.capture_meeting(title=body.get("title"))
        # A meeting is untrusted content: it lands UNREVIEWED, never an auto-action.
        status = getattr(result, "review_status", None) if result is not None else None
        return JSONResponse({"ok": result is not None, "review_status": status})

    @app.websocket("/ws")
    async def ws(websocket: WebSocket) -> None:
        # HTTP middleware does not run for WS, so authenticate the handshake here: Host
        # (anti-rebinding), Origin (anti-CSRF), and a valid session cookie — else refuse.
        if (
            not host_allowed(websocket.headers.get("host", ""))
            or not origin_allowed(websocket.headers.get("origin", ""))
            or not auth.is_valid_session(websocket.cookies.get(SESSION_COOKIE))
        ):
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
            return
        await websocket.accept()
        conn = connections.register(websocket)
        try:
            await websocket.send_json(
                {"type": "hello", "heartbeat_seconds": connections.heartbeat_seconds}
            )
            while True:
                try:
                    msg = await websocket.receive_json()
                except ValueError:
                    continue  # skip a malformed frame; don't tear down the socket
                # approval_shown: the client proves the modal is on screen ⇒ mint a
                # single-use nonce bound to THIS live connection (amendment 3/4).
                if msg.get("type") == "approval_shown":
                    did = msg.get("decision_id")
                    nonce = await approvals.mint_nonce(did, conn) if did else None
                    if nonce is not None:
                        await websocket.send_json(
                            {"type": "approval_nonce", "decision_id": did, "nonce": nonce}
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
