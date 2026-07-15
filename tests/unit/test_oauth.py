"""OAuth PKCE loopback flow (Phase 9 Task 3) — keyless, MockTransport + a local loopback GET."""

from __future__ import annotations

import asyncio
import base64
import datetime as _dt
import hashlib
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from jarvis.connectors import oauth
from jarvis.connectors.base import ConnectorAuthError
from jarvis.connectors.oauth import (
    OAuthProvider,
    authorize,
    build_auth_url,
    exchange_code,
    generate_pkce,
    loopback_server,
    refresh_token_grant,
)

FIXED = _dt.datetime(2026, 1, 1, 12, 0, 0, tzinfo=_dt.UTC)

_P = OAuthProvider(
    name="acme",
    auth_url="https://acme.test/authorize",
    token_url="https://acme.test/token",
    scopes=("scope.a", "scope.b"),
    extra_auth_params=(("access_type", "offline"),),
)


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


# --- PKCE + auth URL -------------------------------------------------------


def test_pkce_challenge_is_unpadded_sha256_of_verifier() -> None:
    verifier, challenge = generate_pkce()
    expected = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest())
        .rstrip(b"=")
        .decode("ascii")
    )
    assert challenge == expected
    assert "=" not in verifier and "=" not in challenge  # base64url, no padding


def test_build_auth_url_carries_pkce_state_scopes_and_extras() -> None:
    url = build_auth_url(
        _P, client_id="cid", redirect_uri="http://127.0.0.1:1/", state="st", challenge="ch"
    )
    q = parse_qs(urlsplit(url).query)
    assert q["response_type"] == ["code"]
    assert q["code_challenge_method"] == ["S256"]
    assert q["code_challenge"] == ["ch"] and q["state"] == ["st"]
    assert q["client_id"] == ["cid"]
    assert set(q["scope"][0].split()) == set(_P.scopes)
    assert q["access_type"] == ["offline"]  # provider extra param


# --- token exchange / refresh ----------------------------------------------


async def test_exchange_code_posts_verifier_and_builds_state() -> None:
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = request.content.decode()
        return httpx.Response(
            200,
            json={
                "access_token": "at",
                "refresh_token": "rt",
                "expires_in": 3600,
                "scope": "scope.a scope.b",
                "token_type": "Bearer",
            },
        )

    async with _client(handler) as http:
        state = await exchange_code(
            _P,
            client_id="cid",
            client_secret="sec",
            code="the-code",
            verifier="the-verifier",
            redirect_uri="http://127.0.0.1:9/",
            http=http,
            now=lambda: FIXED,
        )
    assert seen["url"] == _P.token_url
    assert "grant_type=authorization_code" in seen["body"]
    assert "code_verifier=the-verifier" in seen["body"]
    assert state.access_token == "at" and state.refresh_token == "rt"
    assert state.scopes == ["scope.a", "scope.b"]
    assert state.expires_at == (FIXED + _dt.timedelta(seconds=3600)).isoformat()


async def test_refresh_keeps_prior_refresh_token_when_absent() -> None:
    # Google omits refresh_token on refresh responses; the prior one must be preserved.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"access_token": "at2", "expires_in": 3600})

    async with _client(handler) as http:
        state = await refresh_token_grant(
            _P,
            client_id="c",
            client_secret="s",
            refresh_token="keep-me",
            http=http,
            now=lambda: FIXED,
        )
    assert state.access_token == "at2"
    assert state.refresh_token == "keep-me"


async def test_token_endpoint_error_raises_connector_auth_error_without_body() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "invalid_grant", "secret_detail": "LEAK"})

    async with _client(handler) as http:
        with pytest.raises(ConnectorAuthError) as exc:
            await refresh_token_grant(
                _P, client_id="c", client_secret="s", refresh_token="rt", http=http
            )
    assert "LEAK" not in str(exc.value)  # provider body never surfaced
    assert "reconnect" in exc.value.user_message.lower()


# --- loopback + authorize --------------------------------------------------


async def test_loopback_server_captures_code_and_state() -> None:
    # A real one-shot loopback GET (127.0.0.1 only, no external network).
    with loopback_server(0) as (server, redirect_uri):
        serve = asyncio.create_task(asyncio.to_thread(oauth._serve_one, server, 5.0))
        await asyncio.sleep(0.05)
        async with httpx.AsyncClient() as c:
            resp = await c.get(f"{redirect_uri}/?code=abc&state=xyz")
        assert resp.status_code == 200
        assert "Return to Kira to finish connecting." in resp.text
        assert "Kairo" not in resp.text
        assert "is connected" not in resp.text.lower()
        code, state = await serve
    assert code == "abc" and state == "xyz"


async def test_authorize_rejects_state_mismatch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(oauth, "_serve_one", lambda server, t: ("code123", "not-the-real-state"))
    with pytest.raises(ConnectorAuthError):
        await authorize(_P, client_id="c", client_secret="s", open_browser=False)


async def test_authorize_happy_path_exchanges_code(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(oauth, "random_state", lambda: "S")
    monkeypatch.setattr(oauth, "_serve_one", lambda server, t: ("code123", "S"))

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"access_token": "at", "refresh_token": "rt", "expires_in": 3600}
        )

    async with _client(handler) as http:
        state = await authorize(
            _P, client_id="c", client_secret="s", open_browser=False, http=http, now=lambda: FIXED
        )
    assert state.access_token == "at" and state.refresh_token == "rt"


async def test_authorize_times_out_when_no_code(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(oauth, "_serve_one", lambda server, t: (None, None))
    with pytest.raises(ConnectorAuthError) as exc:
        await authorize(_P, client_id="c", client_secret="s", open_browser=False)
    assert "timed out" in exc.value.user_message
