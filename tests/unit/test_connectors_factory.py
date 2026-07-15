"""build_connectors (Phase 9 Task 6): live-vs-demo composition, demo never masks live."""

from __future__ import annotations

from pathlib import Path

import pytest

from kira.config import load_config
from kira.connectors.factory import build_connectors

_ENV = ("GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "TELEGRAM_BOT_TOKEN", "KAKAO_REST_API_KEY")


@pytest.fixture(autouse=True)
def _clean(monkeypatch: pytest.MonkeyPatch) -> None:
    for k in _ENV:
        monkeypatch.delenv(k, raising=False)


def _write(tmp_path: Path, body: str) -> None:
    (tmp_path / "config").mkdir(parents=True, exist_ok=True)
    (tmp_path / "config" / "settings.yaml").write_text(body, encoding="utf-8")


def test_nothing_configured_returns_none(tmp_path: Path) -> None:
    assert build_connectors(load_config(root=tmp_path, env_file=None)) is None


def test_demo_mode_builds_badged_registry_when_no_keys(tmp_path: Path) -> None:
    _write(tmp_path, "connectors:\n  demo: true\n")
    reg = build_connectors(load_config(root=tmp_path, env_file=None))
    assert reg is not None and reg.demo is True
    assert reg.google is not None  # DemoGoogleClient
    assert reg.has_notifier("telegram") and reg.has_notifier("kakao")
    assert reg.status()["demo"] is True


def test_demo_is_refused_when_real_keys_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # demo:true MUST NOT mask a live account — with real Google keys present, demo is ignored.
    _write(tmp_path, "connectors:\n  demo: true\n  google:\n    enabled: true\n")
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "gid")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "gsecret")
    reg = build_connectors(load_config(root=tmp_path, env_file=None))
    # No token on disk yet ⇒ google not exposed ⇒ registry is None, but crucially NOT demo.
    assert reg is None or reg.demo is False


def test_live_telegram_notifier_built_from_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write(tmp_path, "connectors:\n  telegram:\n    enabled: true\n    chat_id: '123'\n")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "bot")
    reg = build_connectors(load_config(root=tmp_path, env_file=None))
    assert reg is not None and reg.demo is False
    assert reg.has_notifier("telegram")
    assert reg.google is None
