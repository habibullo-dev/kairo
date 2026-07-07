"""Telegram chat id + Kakao client secret + Kakao redirect URI from env (connector config).

Keyless: monkeypatched env / config. Pins the effective-chat-id resolution, the friendly
missing-both message, the Kakao client-secret + redirect wiring, and that no chat id / secret
leaks in status output."""

from __future__ import annotations

from pathlib import Path

import pytest

from jarvis.cli import connect
from jarvis.config import (
    ConfigError,
    load_config,
    resolve_kakao_redirect_uri,
    resolve_telegram_chat_id,
)

_ENV = ("TELEGRAM_CHAT_ID", "KAKAO_REDIRECT_URI", "KAKAO_CLIENT_SECRET", "TELEGRAM_BOT_TOKEN")


@pytest.fixture(autouse=True)
def _clean(monkeypatch: pytest.MonkeyPatch) -> None:
    for k in _ENV:
        monkeypatch.delenv(k, raising=False)


# --- Telegram effective chat id --------------------------------------------


def test_env_chat_id_takes_precedence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "999-from-env")
    cfg = load_config(root=tmp_path, env_file=None)
    cfg.connectors.telegram.chat_id = "111-from-settings"
    assert resolve_telegram_chat_id(cfg) == "999-from-env"


def test_settings_chat_id_is_fallback(tmp_path: Path) -> None:
    cfg = load_config(root=tmp_path, env_file=None)  # no env
    cfg.connectors.telegram.chat_id = "111-from-settings"
    assert resolve_telegram_chat_id(cfg) == "111-from-settings"


def test_no_chat_id_anywhere(tmp_path: Path) -> None:
    cfg = load_config(root=tmp_path, env_file=None)
    assert resolve_telegram_chat_id(cfg) == ""


async def test_connect_telegram_configured_via_env_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Bot token + TELEGRAM_CHAT_ID in env, NOTHING in settings.yaml ⇒ configured + sends.
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "bot")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    cfg = load_config(root=tmp_path, env_file=None)
    assert cfg.connectors.telegram.chat_id == ""  # settings.yaml has none
    sent: dict = {}

    async def fake_send(*, bot_token, chat_id, text, http=None):
        sent.update(bot_token=bot_token, chat_id=chat_id)

    monkeypatch.setattr(connect, "send_telegram_message", fake_send)
    rc = await connect.connect_telegram(cfg, test=True, emit=lambda _l: None)
    assert rc == 0 and sent == {"bot_token": "bot", "chat_id": "42"}


async def test_connect_telegram_missing_both_is_friendly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "bot")  # token present, chat id absent both places
    cfg = load_config(root=tmp_path, env_file=None)
    lines: list[str] = []
    rc = await connect.connect_telegram(cfg, test=True, emit=lines.append)
    assert rc == 1
    msg = "\n".join(lines)
    assert "TELEGRAM_CHAT_ID" in msg and "connectors.telegram.chat_id" in msg


def test_status_uses_effective_chat_id_no_leak(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "bot")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "SECRET-CHAT-CANARY")
    cfg = load_config(root=tmp_path, env_file=None)
    lines: list[str] = []
    connect.show_status(cfg, emit=lines.append)
    joined = "\n".join(lines)
    assert "telegram: configured" in joined  # env chat id makes it configured
    assert "SECRET-CHAT-CANARY" not in joined  # presence only — the id itself never printed


# --- Kakao client secret + redirect URI ------------------------------------


def test_kakao_store_uses_client_secret(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KAKAO_CLIENT_SECRET", "kakao-sec")
    monkeypatch.setenv("KAKAO_REST_API_KEY", "rk")
    cfg = load_config(root=tmp_path, env_file=None)
    store = connect._kakao_store(cfg)
    assert store.client_secret == "kakao-sec"  # passed through when present


def test_kakao_store_optional_client_secret(tmp_path: Path) -> None:
    cfg = load_config(root=tmp_path, env_file=None)  # no KAKAO_CLIENT_SECRET
    store = connect._kakao_store(cfg)
    assert store.client_secret == ""  # PKCE-only app: blank is fine


def test_kakao_redirect_derived_from_port(tmp_path: Path) -> None:
    cfg = load_config(root=tmp_path, env_file=None)
    cfg.connectors.kakao.redirect_port = 8788
    assert resolve_kakao_redirect_uri(cfg) == "http://127.0.0.1:8788"


def test_kakao_redirect_env_agreeing_ok(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = load_config(root=tmp_path, env_file=None)
    cfg.connectors.kakao.redirect_port = 8788
    monkeypatch.setenv("KAKAO_REDIRECT_URI", "http://127.0.0.1:8788/")  # trailing slash tolerated
    assert resolve_kakao_redirect_uri(cfg) == "http://127.0.0.1:8788"


def test_kakao_redirect_env_disagreeing_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = load_config(root=tmp_path, env_file=None)
    cfg.connectors.kakao.redirect_port = 8788
    monkeypatch.setenv("KAKAO_REDIRECT_URI", "http://127.0.0.1:9999")
    with pytest.raises(ConfigError, match="disagrees"):
        resolve_kakao_redirect_uri(cfg)
