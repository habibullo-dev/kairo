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
from jarvis.tools import Permission
from jarvis.ui.approver import ApprovalManager, UIApprover, UIScreenApprover
from jarvis.ui.auth import SESSION_COOKIE, AuthManager, host_allowed, origin_allowed
from jarvis.ui.connections import Connection, ConnectionManager
from jarvis.ui.gate_api import policy_snapshot, read_today_audit
from jarvis.ui.readmodels import (
    UiServices,
    daily_overview,
    hub_status,
    lab_overview,
    list_agent_runs,
    list_memories,
    list_tasks,
    task_runs,
    vault_lint,
    vault_overview,
)

if TYPE_CHECKING:
    from jarvis.config import Config

#: Methods that mutate state — Origin-checked (anti-CSRF). GETs are session-gated instead.
_MUTATING = frozenset({"POST", "PUT", "PATCH", "DELETE"})

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
        return _runner_status(app.state.runner, app.state.session)

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
        return hub_status(
            config, connectors=connectors.status() if connectors is not None else None
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
    async def memory(type: str | None = None) -> JSONResponse:
        svc = app.state.services.memory
        if svc is None:
            return _unavailable("memory")
        return JSONResponse(await list_memories(svc, type_filter=type))

    @app.get("/api/tasks")
    async def tasks() -> JSONResponse:
        svc = app.state.services.tasks
        if svc is None:
            return _unavailable("tasks")
        return JSONResponse(await list_tasks(svc))

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
        try:
            result = await svc.ingest(path=path, url=url, text=text, title=title, created_by="user")
        except Exception as exc:
            return JSONResponse({"ok": False, "message": str(exc)}, status_code=400)
        return JSONResponse({"ok": True, "action": result.action, "source_id": result.source_id})

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
