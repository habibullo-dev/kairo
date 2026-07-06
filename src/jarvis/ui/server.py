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

from typing import TYPE_CHECKING

from fastapi import FastAPI, Request, WebSocket, status
from fastapi.responses import RedirectResponse, Response
from starlette.websockets import WebSocketDisconnect

from jarvis.observability import get_logger
from jarvis.ui.auth import SESSION_COOKIE, AuthManager, host_allowed, origin_allowed
from jarvis.ui.connections import Connection, ConnectionManager

if TYPE_CHECKING:
    from jarvis.config import Config

#: Methods that mutate state — Origin-checked (anti-CSRF). GETs are session-gated instead.
_MUTATING = frozenset({"POST", "PUT", "PATCH", "DELETE"})

#: Paths reachable WITHOUT a session. The exchange mints the session; health is safe.
#: Static app assets are added in Task 7. Everything else requires the session cookie.
_OPEN_PATHS = frozenset({"/api/health"})


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


def create_app(
    config: Config,
    *,
    auth: AuthManager | None = None,
    connections: ConnectionManager | None = None,
) -> FastAPI:
    """Build the workstation app. ``auth``/``connections`` are injectable so tests can
    supply a known token and a fake clock."""
    auth = auth or AuthManager()
    connections = connections or ConnectionManager(heartbeat_seconds=config.ui.heartbeat_seconds)
    log = get_logger("jarvis.ui")
    app = FastAPI(title="Kairo Workstation", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.auth = auth
    app.state.connections = connections
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
        # Placeholder until the frontend lands (Task 7); the guard already enforced the session.
        return Response("Kairo Workstation — UI assets land in Task 7.", media_type="text/plain")

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
                _handle_ws_message(connections, conn, msg)
        except WebSocketDisconnect:
            pass
        finally:
            connections.drop(conn)

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
