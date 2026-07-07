"""OAuth 2.0 authorization-code + PKCE, over a one-shot loopback listener.

Shared by Google and Kakao. The flow is the deliberate terminal ritual `jarvis connect
<provider>`: PKCE (S256) + a random ``state`` (mismatch aborts), a redirect server bound to
``127.0.0.1`` only that handles exactly one request, then a code→token exchange. No secret ever
reaches a browser; the loopback listener is single-use and short-lived.

Provider differences are data (:class:`OAuthProvider`), not code: Google uses an ephemeral
loopback port with ``access_type=offline&prompt=consent``; Kakao uses a fixed pre-registered
port. Token endpoints are constants — this module never accepts a caller-supplied URL.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import datetime as _dt
import hashlib
import http.server
import secrets as _secrets
import webbrowser
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx

from jarvis.connectors.base import ConnectorAuthError
from jarvis.connectors.tokens import TokenState, _utcnow
from jarvis.observability import get_logger

_log = get_logger("jarvis.connectors.oauth")


@dataclass(frozen=True)
class OAuthProvider:
    """The provider-specific knobs of an installed-app OAuth flow."""

    name: str
    auth_url: str
    token_url: str
    scopes: tuple[str, ...]
    #: Fixed loopback port for the redirect (Kakao requires a pre-registered URI). 0 =
    #: ephemeral (Google Desktop apps accept any loopback port).
    redirect_port: int = 0
    #: Extra auth-URL query params (Google: access_type=offline, prompt=consent).
    extra_auth_params: tuple[tuple[str, str], ...] = ()


# --- PKCE ------------------------------------------------------------------


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def generate_pkce() -> tuple[str, str]:
    """Return ``(verifier, challenge)`` where challenge = base64url(sha256(verifier))."""
    verifier = _b64url(_secrets.token_bytes(32))
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    return verifier, challenge


def random_state() -> str:
    return _secrets.token_urlsafe(24)


def build_auth_url(
    provider: OAuthProvider, *, client_id: str, redirect_uri: str, state: str, challenge: str
) -> str:
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(provider.scopes),
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        **dict(provider.extra_auth_params),
    }
    return f"{provider.auth_url}?{urlencode(params)}"


# --- token endpoint --------------------------------------------------------


async def _post_token(provider: OAuthProvider, data: dict, *, http: Any = None) -> dict:
    """POST form data to the provider's token endpoint. Non-200 ⇒ ConnectorAuthError (the
    provider's error body is logged at warning but never surfaced)."""
    if http is not None:
        resp = await http.post(provider.token_url, data=data)
    else:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(provider.token_url, data=data)
    if resp.status_code != 200:
        _log.warning("oauth_token_error", provider=provider.name, status=resp.status_code)
        raise ConnectorAuthError(provider.name)
    return resp.json()


def _to_state(
    provider: OAuthProvider,
    payload: dict,
    *,
    now: Callable[[], _dt.datetime] | None,
    prior_refresh: str,
) -> TokenState:
    now = now or _utcnow
    obtained = now()
    expires_at = obtained + _dt.timedelta(seconds=int(payload.get("expires_in", 3600)))
    scope = payload.get("scope")
    scopes = scope.split() if isinstance(scope, str) and scope else list(provider.scopes)
    return TokenState(
        provider=provider.name,
        access_token=payload["access_token"],
        # Google omits refresh_token on refresh responses; keep the prior one.
        refresh_token=payload.get("refresh_token") or prior_refresh,
        expires_at=expires_at.isoformat(),
        obtained_at=obtained.isoformat(),
        scopes=scopes,
        token_type=payload.get("token_type", "Bearer"),
    )


async def exchange_code(
    provider: OAuthProvider,
    *,
    client_id: str,
    client_secret: str,
    code: str,
    verifier: str,
    redirect_uri: str,
    http: Any = None,
    now: Callable[[], _dt.datetime] | None = None,
) -> TokenState:
    payload = await _post_token(
        provider,
        {
            "grant_type": "authorization_code",
            "code": code,
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": redirect_uri,
            "code_verifier": verifier,
        },
        http=http,
    )
    return _to_state(provider, payload, now=now, prior_refresh="")


async def refresh_token_grant(
    provider: OAuthProvider,
    *,
    client_id: str,
    client_secret: str,
    refresh_token: str,
    http: Any = None,
    now: Callable[[], _dt.datetime] | None = None,
) -> TokenState:
    payload = await _post_token(
        provider,
        {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": client_id,
            "client_secret": client_secret,
        },
        http=http,
    )
    return _to_state(provider, payload, now=now, prior_refresh=refresh_token)


# --- loopback listener -----------------------------------------------------


class _RedirectHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 (http.server API)
        query = parse_qs(urlsplit(self.path).query)
        self.server.captured_code = (query.get("code") or [None])[0]  # type: ignore[attr-defined]
        self.server.captured_state = (query.get("state") or [None])[0]  # type: ignore[attr-defined]
        body = (
            b"<html><body style='font-family:sans-serif'>"
            b"<h3>Kairo is connected.</h3><p>You can close this tab.</p></body></html>"
        )
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: Any) -> None:  # silence the default stderr logging
        pass


@contextlib.contextmanager
def loopback_server(port: int):
    """A one-shot redirect server on ``127.0.0.1:port`` (0 = ephemeral). Yields
    ``(server, redirect_uri)``."""
    server = http.server.HTTPServer(("127.0.0.1", port), _RedirectHandler)
    server.captured_code = None  # type: ignore[attr-defined]
    server.captured_state = None  # type: ignore[attr-defined]
    try:
        yield server, f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.server_close()


def _serve_one(
    server: http.server.HTTPServer, timeout_seconds: float
) -> tuple[str | None, str | None]:
    server.timeout = timeout_seconds
    server.handle_request()  # blocks for one request or until timeout
    return server.captured_code, server.captured_state  # type: ignore[attr-defined]


async def authorize(
    provider: OAuthProvider,
    *,
    client_id: str,
    client_secret: str,
    http: Any = None,
    open_browser: bool = True,
    timeout_seconds: float = 300.0,
    now: Callable[[], _dt.datetime] | None = None,
    emit: Callable[[str], None] = print,
) -> TokenState:
    """Run the installed-app flow to completion and return the granted :class:`TokenState`."""
    verifier, challenge = generate_pkce()
    state = random_state()
    try:
        with loopback_server(provider.redirect_port) as (server, redirect_uri):
            url = build_auth_url(
                provider,
                client_id=client_id,
                redirect_uri=redirect_uri,
                state=state,
                challenge=challenge,
            )
            emit(f"Authorize {provider.name} in your browser. If it doesn't open, visit:\n{url}")
            if open_browser:
                with contextlib.suppress(Exception):
                    webbrowser.open(url)
            code, got_state = await asyncio.to_thread(_serve_one, server, timeout_seconds)
    except OSError as exc:
        raise ConnectorAuthError(
            provider.name,
            user_message=(
                f"Could not start the loopback listener for {provider.name} "
                f"(port {provider.redirect_port}): {exc}"
            ),
        ) from exc
    if not code:
        raise ConnectorAuthError(
            provider.name,
            user_message=f"{provider.name} authorization timed out or was cancelled.",
        )
    if got_state != state:
        raise ConnectorAuthError(
            provider.name, user_message=f"{provider.name} authorization state mismatch — aborted."
        )
    return await exchange_code(
        provider,
        client_id=client_id,
        client_secret=client_secret,
        code=code,
        verifier=verifier,
        redirect_uri=redirect_uri,
        http=http,
        now=now,
    )
