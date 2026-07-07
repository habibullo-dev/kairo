"""Notifiers (Phase 9 Task 5): Telegram + Kakao send-only, Demo no-egress. MockTransport."""

from __future__ import annotations

import datetime as _dt
import json
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest

from jarvis.connectors.base import ConnectorError
from jarvis.connectors.demo import DemoNotifier
from jarvis.connectors.kakao import KakaoNotifier, kakao_provider
from jarvis.connectors.telegram import TelegramNotifier
from jarvis.connectors.tokens import TokenState, TokenStore

FIXED = _dt.datetime(2026, 1, 1, 12, 0, 0, tzinfo=_dt.UTC)


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


# --- Telegram --------------------------------------------------------------


async def test_telegram_send_is_plain_no_markup_no_preview() -> None:
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["url"] = str(req.url)
        captured["form"] = parse_qs(req.content.decode())
        return httpx.Response(200, json={"ok": True})

    async with _client(handler) as http:
        n = TelegramNotifier(bot_token="BOT", chat_id="123", http=http)
        await n.send("hello & <b>world</b>")

    assert "/botBOT/sendMessage" in captured["url"]
    form = captured["form"]
    assert form["chat_id"] == ["123"]
    assert "parse_mode" not in form  # untrusted content never interpreted as markup
    assert form["disable_web_page_preview"] == ["true"]  # httpx encodes bool as lowercase


async def test_telegram_send_failure_is_friendly() -> None:
    async with _client(lambda r: httpx.Response(400, json={"description": "bad"})) as http:
        n = TelegramNotifier(bot_token="BOT", chat_id="123", http=http)
        with pytest.raises(ConnectorError) as exc:
            await n.send("hi")
    assert "bad" not in str(exc.value)  # provider body never surfaced
    assert "TELEGRAM_BOT_TOKEN" in exc.value.user_message


def test_telegram_status_is_presence_only() -> None:
    n = TelegramNotifier(bot_token="BOT", chat_id="123")
    assert n.status() == {"configured": True, "chat_id_set": True}
    assert TelegramNotifier(bot_token="", chat_id="").status() == {
        "configured": False,
        "chat_id_set": False,
    }


# --- Kakao -----------------------------------------------------------------


def _kakao_store(tmp_path: Path, http) -> TokenStore:
    store = TokenStore(
        tmp_path / "kakao.json",
        provider=kakao_provider(8788),
        client_id="restkey",
        client_secret="",
        http=http,
        now=lambda: FIXED,
    )
    store.save(
        TokenState(
            provider="kakao",
            access_token="katok",
            refresh_token="karefresh",
            expires_at=(FIXED + _dt.timedelta(hours=1)).isoformat(),
            obtained_at=FIXED.isoformat(),
            scopes=["talk_message"],
        )
    )
    return store


async def test_kakao_send_posts_memo_and_truncates(tmp_path: Path) -> None:
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["url"] = str(req.url)
        captured["auth"] = req.headers.get("authorization")
        captured["form"] = parse_qs(req.content.decode())
        return httpx.Response(200, json={"result_code": 0})

    async with _client(handler) as http:
        n = KakaoNotifier(_kakao_store(tmp_path, http), http=http)
        await n.send("k" * 500)

    assert "talk/memo/default/send" in captured["url"]
    assert captured["auth"] == "Bearer katok"
    template = json.loads(captured["form"]["template_object"][0])
    assert template["object_type"] == "text"
    assert template["text"] == "k" * 200  # truncated to Kakao's 200-char limit


async def test_kakao_send_failure_is_reconnect_message(tmp_path: Path) -> None:
    async with _client(lambda r: httpx.Response(401, json={"msg": "expired"})) as http:
        n = KakaoNotifier(_kakao_store(tmp_path, http), http=http)
        with pytest.raises(ConnectorError) as exc:
            await n.send("hi")
    assert exc.value.user_message == "Kakao needs reconnect: run jarvis connect kakao"
    assert "expired" not in str(exc.value)


# --- Demo ------------------------------------------------------------------


async def test_demo_notifier_ships_nothing_but_records() -> None:
    n = DemoNotifier(name="telegram")
    await n.send("demo message")
    assert n.sent == ["demo message"]
    assert n.status() == {"configured": True, "demo": True}
