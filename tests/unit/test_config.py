"""Tests for the config layer. No real .env or API keys required."""

from __future__ import annotations

from pathlib import Path

import pytest

from jarvis.config import Config, ConfigError, Secrets, load_config

KEYS = ("ANTHROPIC_API_KEY", "VOYAGE_API_KEY", "TAVILY_API_KEY")


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Start every test from a clean slate — no ambient API keys leaking in."""
    for k in KEYS:
        monkeypatch.delenv(k, raising=False)


def _write_settings(root: Path, body: str) -> None:
    (root / "config").mkdir(parents=True, exist_ok=True)
    (root / "config" / "settings.yaml").write_text(body, encoding="utf-8")


def test_defaults_when_no_settings_file(tmp_path: Path) -> None:
    cfg = load_config(root=tmp_path, env_file=None)
    assert cfg.models.main == "claude-opus-4-8"
    assert cfg.models.utility == "claude-sonnet-5"
    assert cfg.limits.max_iterations == 25
    assert cfg.limits.compaction_threshold == 0.7
    assert cfg.paths.data_dir == Path("data")


def test_yaml_overrides_defaults(tmp_path: Path) -> None:
    _write_settings(
        tmp_path,
        "models:\n  main: claude-fable-5\nlimits:\n  max_iterations: 5\n",
    )
    cfg = load_config(root=tmp_path, env_file=None)
    assert cfg.models.main == "claude-fable-5"
    assert cfg.limits.max_iterations == 5
    # Unspecified values keep their defaults.
    assert cfg.models.utility == "claude-sonnet-5"
    assert cfg.limits.tool_timeout_seconds == 60.0


def test_paths_resolve_absolute_under_root(tmp_path: Path) -> None:
    cfg = load_config(root=tmp_path, env_file=None)
    assert cfg.data_dir == (tmp_path / "data").resolve()
    assert cfg.logs_dir == (tmp_path / "logs").resolve()


def test_ensure_dirs_creates_directories(tmp_path: Path) -> None:
    cfg = load_config(root=tmp_path, env_file=None)
    assert not cfg.data_dir.exists()
    cfg.ensure_dirs()
    assert cfg.data_dir.is_dir()
    assert cfg.logs_dir.is_dir()


def test_require_raises_with_actionable_message_when_missing(tmp_path: Path) -> None:
    cfg = load_config(root=tmp_path, env_file=None)
    with pytest.raises(ConfigError) as exc:
        cfg.require("anthropic")
    msg = str(exc.value)
    assert "ANTHROPIC_API_KEY" in msg
    assert ".env" in msg


def test_require_lists_all_missing_services(tmp_path: Path) -> None:
    cfg = load_config(root=tmp_path, env_file=None)
    with pytest.raises(ConfigError) as exc:
        cfg.require("anthropic", "voyage", "tavily")
    msg = str(exc.value)
    assert "ANTHROPIC_API_KEY" in msg and "VOYAGE_API_KEY" in msg and "TAVILY_API_KEY" in msg


def test_require_passes_when_key_present(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-123")
    cfg = load_config(root=tmp_path, env_file=None)
    cfg.require("anthropic")  # should not raise


def test_load_config_require_enforced_at_load(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        load_config(root=tmp_path, env_file=None, require=("anthropic",))


def test_secrets_read_from_env_file(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("ANTHROPIC_API_KEY=sk-from-file\n", encoding="utf-8")
    cfg = load_config(root=tmp_path)  # default env_file=".env" under root
    assert cfg.secrets.anthropic_api_key == "sk-from-file"
    cfg.require("anthropic")  # present via file


def test_env_var_overrides_env_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / ".env").write_text("ANTHROPIC_API_KEY=sk-from-file\n", encoding="utf-8")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-from-env")
    cfg = load_config(root=tmp_path)
    assert cfg.secrets.anthropic_api_key == "sk-from-env"


def test_non_mapping_yaml_raises(tmp_path: Path) -> None:
    _write_settings(tmp_path, "- just\n- a\n- list\n")
    with pytest.raises(ConfigError):
        load_config(root=tmp_path, env_file=None)


def test_config_is_constructible_directly() -> None:
    # Config with all-default sub-models and empty secrets is valid.
    cfg = Config.model_validate(
        {
            "root": Path.cwd(),
            "models": {},
            "limits": {},
            "paths": {},
            "secrets": Secrets(_env_file=None),  # type: ignore[call-arg]
        }
    )
    assert cfg.models.judge == "claude-opus-4-8"
