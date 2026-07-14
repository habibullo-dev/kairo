"""A reset requires provider-by-provider consent before external access resumes."""

from __future__ import annotations

from pathlib import Path

import pytest

from jarvis.cli import connect as connect_cli
from jarvis.config import load_config
from jarvis.connectors.consent import (
    LOCKED_PROVIDERS,
    integration_consent_path,
    integration_is_locked,
    lock_all_integrations,
    locked_integrations,
    unlock_integration,
)
from jarvis.connectors.factory import build_connectors


def test_consent_marker_is_provider_scoped_and_invalid_content_fails_closed(
    tmp_path: Path,
) -> None:
    data = tmp_path / "data"
    lock_all_integrations(data)
    assert locked_integrations(data) == LOCKED_PROVIDERS

    unlock_integration(data, "google")
    assert not integration_is_locked(data, "google")
    assert integration_is_locked(data, "telegram")

    integration_consent_path(data).write_text("not-json", encoding="utf-8")
    assert locked_integrations(data) == LOCKED_PROVIDERS
    with pytest.raises(ValueError, match="Unknown"):
        unlock_integration(data, "calendar")


def test_factory_does_not_expose_locked_telegram(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "bot")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "123")
    config = load_config(root=tmp_path, env_file=None)
    config.connectors.telegram.enabled = True
    assert build_connectors(config) is not None

    lock_all_integrations(config.data_dir)
    assert build_connectors(config) is None


async def test_successful_connect_unlocks_only_requested_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "bot")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "123")
    config = load_config(root=tmp_path, env_file=None)
    lock_all_integrations(config.data_dir)

    args = type("Args", (), {"provider": "telegram", "test": False})()
    assert await connect_cli._dispatch(args, config) == 0
    assert not integration_is_locked(config.data_dir, "telegram")
    assert integration_is_locked(config.data_dir, "google")
    assert integration_is_locked(config.data_dir, "kakao")

    lines: list[str] = []
    connect_cli.show_status(config, emit=lines.append)
    assert "google: locked after data reset" in "\n".join(lines)
