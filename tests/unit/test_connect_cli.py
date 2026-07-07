"""`jarvis connect` internals (Phase 9 Task 3) — keyless, authorize/send monkeypatched."""

from __future__ import annotations

from pathlib import Path

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
