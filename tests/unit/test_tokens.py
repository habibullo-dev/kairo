"""TokenStore + TokenState (Phase 9 Task 3): custody, atomic write, single-flight refresh."""

from __future__ import annotations

import asyncio
import datetime as _dt
from pathlib import Path

import httpx
import pytest

from kira.connectors.base import ConnectorAuthError
from kira.connectors.oauth import OAuthProvider
from kira.connectors.tokens import (
    TokenState,
    TokenStore,
    read_token_state,
    write_token_state,
)

FIXED = _dt.datetime(2026, 1, 1, 12, 0, 0, tzinfo=_dt.UTC)
_P = OAuthProvider(name="acme", auth_url="", token_url="https://acme.test/token", scopes=("a",))


def _state(expires_in: int, *, access: str = "old", refresh: str = "rt") -> TokenState:
    exp = (FIXED + _dt.timedelta(seconds=expires_in)).isoformat()
    return TokenState(
        provider="acme",
        access_token=access,
        refresh_token=refresh,
        expires_at=exp,
        obtained_at=FIXED.isoformat(),
        scopes=["a"],
    )


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _store(path: Path, http, **kw) -> TokenStore:
    return TokenStore(
        path,
        provider=_P,
        client_id="cid",
        client_secret="topsecret",
        http=http,
        now=lambda: FIXED,
        **kw,
    )


# --- persistence -----------------------------------------------------------


def test_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "connectors" / "acme_token.json"
    st = _state(3600)
    write_token_state(path, st)
    assert read_token_state(path) == st


def test_read_missing_or_malformed_is_none(tmp_path: Path) -> None:
    assert read_token_state(tmp_path / "nope.json") is None
    bad = tmp_path / "bad.json"
    bad.write_text("{ not json", encoding="utf-8")
    assert read_token_state(bad) is None


def test_saved_file_never_contains_client_secret(tmp_path: Path) -> None:
    # TokenState has no client_secret field; prove the secret used to build the store is absent.
    path = tmp_path / "acme_token.json"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"access_token": "new", "expires_in": 3600})

    async def run() -> None:
        async with _client(handler) as http:
            store = _store(path, http)
            store.save(_state(-10))  # expired ⇒ next access refreshes
            await store.access_token()

    asyncio.run(run())
    text = path.read_text(encoding="utf-8")
    assert "topsecret" not in text
    assert "client_secret" not in text


def test_atomic_write_replaces_prior(tmp_path: Path) -> None:
    path = tmp_path / "acme_token.json"
    write_token_state(path, _state(3600, access="first"))
    write_token_state(path, _state(3600, access="second"))
    assert read_token_state(path).access_token == "second"
    # no leftover temp files in the dir
    assert [p.name for p in path.parent.iterdir()] == ["acme_token.json"]


# --- refresh ---------------------------------------------------------------


async def test_fresh_token_is_returned_without_refresh(tmp_path: Path) -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(200, json={"access_token": "new", "expires_in": 3600})

    async with _client(handler) as http:
        store = _store(tmp_path / "t.json", http)
        store.save(_state(3600))  # valid
        assert await store.access_token() == "old"
    assert calls == []  # no refresh POST


async def test_expiry_skew_boundary(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"access_token": "refreshed", "expires_in": 3600})

    # 121s left (> 120s skew) ⇒ still valid, no refresh.
    async with _client(handler) as http:
        store = _store(tmp_path / "a.json", http)
        store.save(_state(121))
        assert await store.access_token() == "old"

    # 60s left (< 120s skew) ⇒ refresh.
    async with _client(handler) as http:
        store = _store(tmp_path / "b.json", http)
        store.save(_state(60))
        assert await store.access_token() == "refreshed"


async def test_single_flight_refresh(tmp_path: Path) -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(200, json={"access_token": "new", "expires_in": 3600})

    async with _client(handler) as http:
        store = _store(tmp_path / "t.json", http)
        store.save(_state(-10))  # expired
        a, b = await asyncio.gather(store.access_token(), store.access_token())
    assert a == b == "new"
    assert len(calls) == 1  # two concurrent callers ⇒ exactly one refresh POST


async def test_invalid_grant_raises_friendly(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "invalid_grant"})

    async with _client(handler) as http:
        store = _store(tmp_path / "t.json", http)
        store.save(_state(-10))
        with pytest.raises(ConnectorAuthError) as exc:
            await store.access_token()
    assert exc.value.user_message == "Acme needs reconnect — use `uv run kira connect acme`."


async def test_access_token_without_state_raises(tmp_path: Path) -> None:
    async with _client(lambda r: httpx.Response(200, json={})) as http:
        store = _store(tmp_path / "missing.json", http)
        with pytest.raises(ConnectorAuthError):
            await store.access_token()


def test_status_presence_only(tmp_path: Path) -> None:
    store = _store(tmp_path / "t.json", None)
    assert store.status() == {"connected": False, "needs_reconnect": True}
    store.save(_state(3600))
    s = store.status()
    assert s["connected"] is True and s["scopes"] == ["a"] and s["needs_reconnect"] is False
