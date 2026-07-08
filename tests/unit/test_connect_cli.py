"""`jarvis connect` internals (Phase 9 Task 3) — keyless, authorize/send monkeypatched."""

from __future__ import annotations

import datetime as _dt
from pathlib import Path

import httpx
import pytest

from jarvis.cli import connect
from jarvis.config import load_config
from jarvis.connectors.google import GOOGLE_SCOPES
from jarvis.connectors.tokens import TokenState, write_token_state

_FAKE = TokenState(
    provider="google",
    access_token="ACCESS_SENTINEL",
    refresh_token="REFRESH_SENTINEL",
    expires_at="2030-01-01T00:00:00+00:00",
    obtained_at="2026-01-01T00:00:00+00:00",
    scopes=list(GOOGLE_SCOPES),
)


@pytest.fixture(autouse=True)
def _clean(monkeypatch: pytest.MonkeyPatch) -> None:
    for k in (
        "GOOGLE_CLIENT_ID",
        "GOOGLE_CLIENT_SECRET",
        "TELEGRAM_BOT_TOKEN",
        "KAKAO_REST_API_KEY",
    ):
        monkeypatch.delenv(k, raising=False)


async def test_connect_google_writes_token_and_prints_scopes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "gid")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "topsecret")
    cfg = load_config(root=tmp_path, env_file=None)

    async def fake_authorize(provider, **kw):
        return _FAKE

    monkeypatch.setattr(connect, "authorize", fake_authorize)
    lines: list[str] = []
    rc = await connect.connect_google(cfg, emit=lines.append)

    assert rc == 0
    path = connect.token_path(cfg, "google")
    assert path.exists()
    text = path.read_text(encoding="utf-8")
    assert "ACCESS_SENTINEL" in text  # token stored
    assert "topsecret" not in text  # client secret never written to the token file
    assert any("calendar.readonly" in ln for ln in lines)  # scopes printed


async def test_connect_google_missing_keys_returns_1(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = load_config(root=tmp_path, env_file=None)
    lines: list[str] = []
    rc = await connect.connect_google(cfg, emit=lines.append)
    assert rc == 1
    assert any("GOOGLE_CLIENT_ID" in ln for ln in lines)


async def test_connect_kakao_threads_configured_redirect_uri(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The EXACT path-bearing redirect URI (loaded from env) is passed to authorize AND surfaced
    # for the user to register — so the console registration and the OAuth request agree.
    monkeypatch.setenv("KAKAO_REST_API_KEY", "kkey")
    monkeypatch.setenv("KAKAO_REDIRECT_URI", "http://127.0.0.1:8788/oauth/kakao/callback")
    cfg = load_config(root=tmp_path, env_file=None)
    cfg.connectors.kakao.redirect_port = 8788
    captured: dict = {}

    async def fake_authorize(provider, **kw):
        captured.update(kw)
        return _FAKE

    monkeypatch.setattr(connect, "authorize", fake_authorize)
    lines: list[str] = []
    rc = await connect.connect_kakao(cfg, emit=lines.append)

    assert rc == 0
    assert captured["redirect_uri"] == "http://127.0.0.1:8788/oauth/kakao/callback"
    assert any("oauth/kakao/callback" in ln for ln in lines)  # register-this-URI hint printed


def test_status_reports_presence_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = load_config(root=tmp_path, env_file=None)
    lines: list[str] = []
    connect.show_status(cfg, emit=lines.append)
    joined = "\n".join(lines)
    assert "google: not connected" in joined
    assert "telegram: not configured" in joined

    write_token_state(connect.token_path(cfg, "google"), _FAKE)
    lines2: list[str] = []
    connect.show_status(cfg, emit=lines2.append)
    joined2 = "\n".join(lines2)
    assert "google: connected" in joined2
    # never prints a token value (access or refresh)
    assert "ACCESS_SENTINEL" not in joined2 and "REFRESH_SENTINEL" not in joined2


async def test_connect_telegram_test_sends(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "bt")
    cfg = load_config(root=tmp_path, env_file=None)
    cfg.connectors.telegram.chat_id = "12345"
    sent: dict = {}

    async def fake_send(*, bot_token, chat_id, text, http=None):
        sent.update(bot_token=bot_token, chat_id=chat_id, text=text)

    monkeypatch.setattr(connect, "send_telegram_message", fake_send)
    rc = await connect.connect_telegram(cfg, test=True, emit=lambda _ln: None)
    assert rc == 0
    assert sent["chat_id"] == "12345" and sent["bot_token"] == "bt"


async def test_connect_telegram_missing_token_returns_1(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = load_config(root=tmp_path, env_file=None)
    rc = await connect.connect_telegram(cfg, test=True, emit=lambda _ln: None)
    assert rc == 1


# --- kakao connect --test (Phase 9 Task 12.5) ------------------------------


def _kakao_client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _seed_kakao(cfg, *, expires_in_hours: int = 1, access: str = "at", refresh: str = "rt") -> None:
    exp = (_dt.datetime.now(_dt.UTC) + _dt.timedelta(hours=expires_in_hours)).isoformat()
    write_token_state(
        connect.token_path(cfg, "kakao"),
        TokenState(
            provider="kakao",
            access_token=access,
            refresh_token=refresh,
            expires_at=exp,
            obtained_at="2026-01-01T00:00:00+00:00",
            scopes=["talk_message"],
        ),
    )


async def test_connect_kakao_test_sends_memo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KAKAO_REST_API_KEY", "rk")
    cfg = load_config(root=tmp_path, env_file=None)
    _seed_kakao(cfg)  # fresh token
    hits = {"memo": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        if "memo/default/send" in str(req.url):
            hits["memo"] += 1
            return httpx.Response(200, json={"result_code": 0})
        return httpx.Response(200, json={})

    lines: list[str] = []
    async with _kakao_client(handler) as http:
        rc = await connect.connect_kakao(cfg, test=True, http=http, emit=lines.append)
    assert rc == 0 and hits["memo"] == 1
    assert any("Sent a test memo to Kakao" in ln for ln in lines)


async def test_connect_kakao_test_refreshes_expired_access(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KAKAO_REST_API_KEY", "rk")
    cfg = load_config(root=tmp_path, env_file=None)
    _seed_kakao(cfg, expires_in_hours=-1)  # expired, but has a refresh token
    hits = {"token": 0, "memo": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        if "oauth/token" in url:  # the single-flight refresh through TokenStore
            hits["token"] += 1
            return httpx.Response(200, json={"access_token": "new", "expires_in": 3600})
        if "memo/default/send" in url:
            hits["memo"] += 1
            return httpx.Response(200, json={"result_code": 0})
        return httpx.Response(200, json={})

    lines: list[str] = []
    async with _kakao_client(handler) as http:
        rc = await connect.connect_kakao(cfg, test=True, http=http, emit=lines.append)
    assert rc == 0 and hits == {"token": 1, "memo": 1}  # refreshed, then sent


async def test_connect_kakao_test_expired_shows_reconnect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KAKAO_REST_API_KEY", "rk")
    cfg = load_config(root=tmp_path, env_file=None)
    _seed_kakao(cfg, expires_in_hours=-1)

    def handler(req: httpx.Request) -> httpx.Response:
        if "oauth/token" in str(req.url):
            return httpx.Response(400, json={"error": "invalid_grant", "detail": "LEAK"})
        return httpx.Response(200, json={})

    lines: list[str] = []
    async with _kakao_client(handler) as http:
        rc = await connect.connect_kakao(cfg, test=True, http=http, emit=lines.append)
    assert rc == 1
    joined = "\n".join(lines)
    assert "Kakao needs reconnect: run jarvis connect kakao" in joined
    assert "LEAK" not in joined and "invalid_grant" not in joined  # provider body never shown


async def test_connect_kakao_test_without_token_shows_reconnect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KAKAO_REST_API_KEY", "rk")
    cfg = load_config(root=tmp_path, env_file=None)  # no token file seeded
    lines: list[str] = []
    rc = await connect.connect_kakao(cfg, test=True, emit=lines.append)  # no network reached
    assert rc == 1
    assert any("Kakao needs reconnect: run jarvis connect kakao" in ln for ln in lines)


async def test_connect_kakao_test_missing_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = load_config(root=tmp_path, env_file=None)  # no KAKAO_REST_API_KEY
    lines: list[str] = []
    rc = await connect.connect_kakao(cfg, test=True, emit=lines.append)
    assert rc == 1 and any("KAKAO_REST_API_KEY" in ln for ln in lines)


async def test_connect_kakao_test_leaks_no_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KAKAO_REST_API_KEY", "REST-CANARY")
    cfg = load_config(root=tmp_path, env_file=None)
    _seed_kakao(cfg, access="ACCESS-CANARY", refresh="REFRESH-CANARY")

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"result_code": 0, "leak": "PROVIDER-CANARY"})

    lines: list[str] = []
    async with _kakao_client(handler) as http:
        await connect.connect_kakao(cfg, test=True, http=http, emit=lines.append)
    blob = "\n".join(lines)
    for canary in ("ACCESS-CANARY", "REFRESH-CANARY", "REST-CANARY", "PROVIDER-CANARY"):
        assert canary not in blob
