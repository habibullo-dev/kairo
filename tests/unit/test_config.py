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


def test_memory_config_defaults(tmp_path: Path) -> None:
    cfg = load_config(root=tmp_path, env_file=None)
    assert cfg.memory.enabled is True
    assert cfg.memory.top_k == 6
    assert cfg.memory.dedup_trigger == 0.85
    assert cfg.memory.reflection is True


def test_memory_config_yaml_override(tmp_path: Path) -> None:
    _write_settings(tmp_path, "memory:\n  enabled: false\n  top_k: 3\n")
    cfg = load_config(root=tmp_path, env_file=None)
    assert cfg.memory.enabled is False
    assert cfg.memory.top_k == 3
    assert cfg.memory.min_similarity == 0.35  # unspecified keeps default


def test_scheduler_config_defaults(tmp_path: Path) -> None:
    cfg = load_config(root=tmp_path, env_file=None)
    assert cfg.scheduler.enabled is True
    assert cfg.scheduler.misfire_grace_seconds == 3600
    assert cfg.scheduler.max_consecutive_failures == 3
    assert cfg.scheduler.max_job_iterations == 15
    assert cfg.scheduler.reflect_job_sessions is False
    assert cfg.scheduler.unattended_allow_tools == []


def test_scheduler_config_yaml_override(tmp_path: Path) -> None:
    _write_settings(
        tmp_path,
        "scheduler:\n  enabled: false\n  misfire_grace_seconds: 60\n"
        "  unattended_allow_tools: [web_fetch]\n",
    )
    cfg = load_config(root=tmp_path, env_file=None)
    assert cfg.scheduler.enabled is False
    assert cfg.scheduler.misfire_grace_seconds == 60
    assert cfg.scheduler.unattended_allow_tools == ["web_fetch"]
    assert cfg.scheduler.wake_cap_seconds == 30  # unspecified keeps default


def test_knowledge_config_defaults(tmp_path: Path) -> None:
    cfg = load_config(root=tmp_path, env_file=None)
    assert cfg.knowledge.enabled is True
    assert cfg.knowledge.pdf_converter == "markitdown"
    assert cfg.knowledge.top_k == 8
    assert cfg.knowledge.chunk_chars == 2000
    assert cfg.knowledge.max_ingest_bytes == 50_000_000


def test_knowledge_config_yaml_override(tmp_path: Path) -> None:
    _write_settings(tmp_path, "knowledge:\n  enabled: false\n  pdf_converter: docling\n")
    cfg = load_config(root=tmp_path, env_file=None)
    assert cfg.knowledge.enabled is False
    assert cfg.knowledge.pdf_converter == "docling"
    assert cfg.knowledge.top_k == 8  # unspecified keeps default


def test_knowledge_dir_resolves_under_root(tmp_path: Path) -> None:
    cfg = load_config(root=tmp_path, env_file=None)
    assert cfg.knowledge_dir == (tmp_path / "data" / "knowledge").resolve()


def test_sub_agents_config_defaults(tmp_path: Path) -> None:
    cfg = load_config(root=tmp_path, env_file=None)
    assert cfg.sub_agents.enabled is True
    assert cfg.sub_agents.model is None  # None = inherit models.main (resolved by the service)
    assert cfg.sub_agents.max_iterations == 15
    assert cfg.sub_agents.timeout_seconds == 600.0
    assert cfg.sub_agents.max_parallel == 4
    assert cfg.sub_agents.max_spawn_calls_per_turn == 8


def test_sub_agents_config_yaml_override(tmp_path: Path) -> None:
    _write_settings(
        tmp_path,
        "sub_agents:\n  enabled: false\n  model: claude-fable-5\n  max_parallel: 2\n",
    )
    cfg = load_config(root=tmp_path, env_file=None)
    assert cfg.sub_agents.enabled is False
    assert cfg.sub_agents.model == "claude-fable-5"
    assert cfg.sub_agents.max_parallel == 2
    assert cfg.sub_agents.max_iterations == 15  # unspecified keeps default
    assert cfg.sub_agents.max_spawn_calls_per_turn == 8


def test_sub_agents_blank_model_yaml_is_none(tmp_path: Path) -> None:
    # `model:` with no value parses to None (the inherit-main sentinel), matching
    # the committed settings.yaml which ships the key blank.
    _write_settings(tmp_path, "sub_agents:\n  model:\n")
    cfg = load_config(root=tmp_path, env_file=None)
    assert cfg.sub_agents.model is None


def test_voice_config_defaults(tmp_path: Path) -> None:
    cfg = load_config(root=tmp_path, env_file=None)
    assert cfg.voice.enabled is False  # opt-in surface, off by default
    assert cfg.voice.cloud_providers is False
    assert cfg.voice.stt_provider == "local"  # no-egress default
    assert cfg.voice.tts_provider == "local"
    assert cfg.voice.wake_word is None  # activation deferred (blank/None)
    assert cfg.voice.retain_audio is False  # transcript kept, raw audio discarded


def test_voice_cloud_provider_requires_optin(tmp_path: Path) -> None:
    # Selecting a cloud STT/TTS without the explicit opt-in is refused at config load —
    # no audio/spoken text leaves the machine to a third party by accident (ADR-0007).
    _write_settings(tmp_path, "voice:\n  stt_provider: openai\n")
    with pytest.raises(ConfigError):
        load_config(root=tmp_path, env_file=None)
    _write_settings(tmp_path, "voice:\n  tts_provider: elevenlabs\n")
    with pytest.raises(ConfigError):
        load_config(root=tmp_path, env_file=None)


def test_voice_cloud_provider_allowed_with_optin(tmp_path: Path) -> None:
    _write_settings(
        tmp_path,
        "voice:\n  enabled: true\n  cloud_providers: true\n  stt_provider: openai\n"
        "  tts_provider: elevenlabs\n",
    )
    cfg = load_config(root=tmp_path, env_file=None)
    assert cfg.voice.cloud_providers is True
    assert cfg.voice.stt_provider == "openai"
    assert cfg.voice.tts_provider == "elevenlabs"


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
