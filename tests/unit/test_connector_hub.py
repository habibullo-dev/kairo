"""Connector Hub read model + UI safety pins.

The Hub is intentionally a read-only setup guide: it receives only the registry's safe
presence snapshot and never turns a connector command into a browser mutation.
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from jarvis.config import load_config
from jarvis.ui.auth import SESSION_COOKIE, AuthManager
from jarvis.ui.readmodels import connector_hub_overview
from jarvis.ui.server import STATIC_DIR, create_app


def _cfg(tmp_path: Path):
    return load_config(root=tmp_path, env_file=None)


def test_connector_hub_makes_google_scopes_and_boundaries_readable(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    overview = connector_hub_overview(
        cfg,
        connectors={
            "demo": False,
            "google": {
                "connected": True,
                "needs_reconnect": False,
                "scopes": [
                    "https://www.googleapis.com/auth/calendar.readonly",
                    "https://www.googleapis.com/auth/gmail.compose",
                    "https://www.googleapis.com/auth/drive.file",
                ],
            },
            "notifiers": {},
        },
    )
    google = overview["google"]
    assert google["state"] == "connected"
    assert [x["name"] for x in google["scopes"]] == [
        "Read calendar events",
        "Create and update Gmail drafts",
        "Create and update Kira-created Docs",
    ]
    gmail = next(x for x in google["services"] if x["name"] == "Gmail")
    drive = next(x for x in google["services"] if x["name"] == "Drive & Docs")
    assert all(x["state"] == "connected" for x in google["services"])
    assert "cannot send" in gmail["cannot"].lower()
    assert "no broad drive" in drive["cannot"].lower()
    assert "not a ui action" in google["disconnect_note"].lower()


def test_expired_google_and_notifiers_get_actionable_safe_states(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    cfg.connectors.google.enabled = True
    cfg.connectors.telegram.enabled = True
    cfg.connectors.kakao.enabled = True
    cfg.secrets = cfg.secrets.model_copy(
        update={
            "google_client_id": "id",
            "google_client_secret": "secret",
            "telegram_bot_token": "bot",
        }
    )
    overview = connector_hub_overview(
        cfg,
        connectors={
            "google": {"connected": True, "needs_reconnect": True},
            "notifiers": {"telegram": {"configured": True, "chat_id_set": True}},
        },
    )
    assert overview["google"]["state"] == "needs_reconnect"
    assert overview["telegram"]["state"] == "configured"
    assert overview["telegram"]["chat_id_set"] is True
    assert overview["kakao"]["state"] == "missing_key"
    assert overview["kakao"]["redirect_uri"].startswith("http://127.0.0.1:")


def test_hub_surfaces_remote_control_without_disclosing_its_allowed_chat(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    cfg.connectors.telegram.remote_control.enabled = True
    cfg.connectors.telegram.remote_control.allowed_chat_id = "123456789"
    cfg.secrets = cfg.secrets.model_copy(update={"telegram_bot_token": "bot"})
    overview = connector_hub_overview(cfg, connectors={"google": None, "notifiers": {}})
    telegram = overview["telegram"]
    assert telegram["state"] == "configured"
    assert telegram["remote_control"] == {
        "enabled": True,
        "ready": True,
        "max_model_messages_per_hour": 20,
    }
    assert "123456789" not in str(telegram)


def test_connector_hub_is_presence_only_and_keeps_provider_policy(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    secret = "CONNECTOR-HUB-SECRET-CANARY"
    cfg.secrets = cfg.secrets.model_copy(
        update={
            "google_client_id": secret,
            "google_client_secret": secret,
            "telegram_bot_token": secret,
            "kakao_rest_api_key": secret,
            "anthropic_api_key": secret,
        }
    )
    overview = connector_hub_overview(cfg, connectors={"google": None, "notifiers": {}})
    assert secret not in str(overview)
    assert "12345" not in str(overview)  # recipient/chat IDs are never represented
    providers = {row["id"]: row for row in overview["providers"]}
    assert {"anthropic", "openai", "gemini", "qwen", "deepseek", "zai"} <= set(providers)
    assert providers["anthropic"]["trusted_authority"] is True
    assert providers["qwen"]["private_ok"] is False
    assert all(isinstance(row["key_present"], bool) for row in providers.values())


def test_hub_route_does_not_leak_connector_or_provider_secret(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    secret = "HUB-ROUTE-SECRET-CANARY"
    cfg.secrets = cfg.secrets.model_copy(
        update={
            "google_client_id": secret,
            "google_client_secret": secret,
            "telegram_bot_token": secret,
            "kakao_rest_api_key": secret,
            "anthropic_api_key": secret,
        }
    )
    auth = AuthManager(token="tok")
    client = TestClient(create_app(cfg, auth=auth), base_url="http://127.0.0.1")
    response = client.get("/api/hub", headers={"cookie": f"{SESSION_COOKIE}={auth.mint_session()}"})
    assert response.status_code == 200
    assert secret not in response.text


def test_kakao_redirect_display_strips_query_and_fragment(tmp_path: Path, monkeypatch) -> None:
    secret = "KAKAO-REDIRECT-SECRET-CANARY"
    monkeypatch.setenv(
        "KAKAO_REDIRECT_URI", f"http://127.0.0.1:8788/oauth/callback?state={secret}#{secret}"
    )
    overview = connector_hub_overview(_cfg(tmp_path))
    assert overview["kakao"]["redirect_uri"] == "http://127.0.0.1:8788/oauth/callback"
    assert secret not in str(overview)


def test_hub_ui_is_copy_only_and_states_gmail_drive_notification_limits(tmp_path: Path) -> None:
    del tmp_path  # Static UI assertion; kept as a pytest fixture for consistent test discovery.
    source = (STATIC_DIR / "screens" / "hub.js").read_text(encoding="utf-8")
    assert "innerHTML" not in source
    assert "api.post" not in source and "fetch(" not in source
    assert "rule(\"Cannot\"" in source
    assert "google.services" in source
    assert "telegramCard" in source and "kakaoCard" in source
    assert "Remote chat" in source and "cannot approve" in source
    for command in (
        "uv run jarvis connect google", "uv run jarvis connect status",
        "uv run jarvis connect telegram --test", "uv run jarvis connect kakao",
        "uv run jarvis connect kakao --test",
    ):
        assert command in source
