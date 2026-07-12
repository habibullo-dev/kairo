"""Tests for the config layer. No real .env or API keys required."""

from __future__ import annotations

from pathlib import Path

import pytest

from jarvis.config import Config, ConfigError, Secrets, load_config

KEYS = (
    "ANTHROPIC_API_KEY",
    "VOYAGE_API_KEY",
    "TAVILY_API_KEY",
    "OPENAI_API_KEY",
    "DEEPSEEK_API_KEY",
    "DASHSCOPE_API_KEY",
    "ZAI_API_KEY",
    "GEMINI_API_KEY",
    "GOOGLE_CLIENT_ID",
    "GOOGLE_CLIENT_SECRET",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
    "KAKAO_REST_API_KEY",
    "KAKAO_CLIENT_SECRET",
    "KAKAO_REDIRECT_URI",
)


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


def test_ui_config_defaults(tmp_path: Path) -> None:
    cfg = load_config(root=tmp_path, env_file=None)
    assert cfg.ui.enabled is False  # opt-in surface, off by default
    assert cfg.ui.host == "127.0.0.1"  # loopback
    assert cfg.ui.port == 8787
    assert cfg.ui.heartbeat_seconds == 15.0
    assert cfg.ui.ring_buffer_events == 2000


def test_ui_loopback_hosts_allowed(tmp_path: Path) -> None:
    for host in ("127.0.0.1", "localhost", "::1"):
        _write_settings(tmp_path, f"ui:\n  host: {host!r}\n")
        cfg = load_config(root=tmp_path, env_file=None)
        assert cfg.ui.host == host


def test_ui_non_loopback_host_refused(tmp_path: Path) -> None:
    # A web surface reachable off-box is a config ERROR this phase (fail-closed, ADR-0008) —
    # not a bigger allowlist. This is the private-admin-console contract's first line.
    for host in ("0.0.0.0", "192.168.1.10", "example.com"):
        _write_settings(tmp_path, f"ui:\n  host: {host!r}\n")
        with pytest.raises(ConfigError):
            load_config(root=tmp_path, env_file=None)


def test_connectors_config_defaults(tmp_path: Path) -> None:
    cfg = load_config(root=tmp_path, env_file=None)
    c = cfg.connectors
    assert c.demo is False
    assert c.google.enabled is False and c.google.calendar_id == "primary"
    assert c.telegram.enabled is False and c.telegram.chat_id == ""
    assert c.telegram.notify_reminders is False
    assert c.kakao.enabled is False and c.kakao.redirect_port == 8788
    assert c.digest.enabled is False and c.digest.deliver == ["ui"]
    assert c.digest.rich_notify is False
    assert c.repos == ["."]


def test_connectors_config_yaml_override(tmp_path: Path) -> None:
    _write_settings(
        tmp_path,
        "connectors:\n"
        "  demo: true\n"
        "  google:\n    enabled: true\n    calendar_id: work@example.com\n"
        "  telegram:\n    enabled: true\n    chat_id: '12345'\n"
        "  repos: ['.', '../other']\n",
    )
    cfg = load_config(root=tmp_path, env_file=None)
    c = cfg.connectors
    assert c.demo is True
    assert c.google.enabled is True and c.google.calendar_id == "work@example.com"
    assert c.telegram.enabled is True and c.telegram.chat_id == "12345"
    assert c.repos == [".", "../other"]
    assert c.kakao.enabled is False  # unspecified keeps default


def test_digest_delivery_requires_enabled_notifier(tmp_path: Path) -> None:
    # Fail-closed: a digest can't target a channel whose notifier is off (ADR-0010).
    _write_settings(
        tmp_path,
        "connectors:\n  digest:\n    enabled: true\n    deliver: [ui, telegram]\n",
    )
    with pytest.raises(ConfigError):
        load_config(root=tmp_path, env_file=None)
    # With the notifier enabled it loads fine.
    _write_settings(
        tmp_path,
        "connectors:\n  telegram:\n    enabled: true\n"
        "  digest:\n    enabled: true\n    deliver: [ui, telegram]\n",
    )
    cfg = load_config(root=tmp_path, env_file=None)
    assert cfg.connectors.digest.deliver == ["ui", "telegram"]


def test_digest_delivery_rejects_unknown_channel(tmp_path: Path) -> None:
    _write_settings(tmp_path, "connectors:\n  digest:\n    deliver: [ui, carrier_pigeon]\n")
    with pytest.raises(ConfigError):
        load_config(root=tmp_path, env_file=None)


def test_connector_secrets_default_empty_and_read_from_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = load_config(root=tmp_path, env_file=None)
    assert cfg.secrets.google_client_id == ""
    assert cfg.secrets.google_client_secret == ""
    assert cfg.secrets.telegram_bot_token == ""
    assert cfg.secrets.kakao_rest_api_key == ""
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "gid-123")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "bot-abc")
    cfg2 = load_config(root=tmp_path, env_file=None)
    assert cfg2.secrets.google_client_id == "gid-123"
    cfg2.require("google")  # representative key present
    cfg2.require("telegram")


def test_require_reports_missing_connector_keys(tmp_path: Path) -> None:
    cfg = load_config(root=tmp_path, env_file=None)
    with pytest.raises(ConfigError) as exc:
        cfg.require("google", "kakao")
    msg = str(exc.value)
    assert "GOOGLE_CLIENT_ID" in msg and "KAKAO_REST_API_KEY" in msg


def test_services_config_defaults(tmp_path: Path) -> None:
    cfg = load_config(root=tmp_path, env_file=None)
    assert cfg.services.enabled == []  # fail-closed: nothing on until a human lists it
    assert cfg.services.semgrep_config == "p/ci"  # T6: works with --metrics=off (auto errors)
    assert cfg.services.playwright_allow_ports == []


def test_services_config_yaml_override(tmp_path: Path) -> None:
    # Regression: load_config must actually read the services: block. It previously dropped it,
    # so services.enabled stayed [] no matter the YAML — the whole 10B enablement flag was inert.
    _write_settings(
        tmp_path,
        "services:\n"
        "  enabled: [semgrep, gitleaks, playwright_local]\n"
        "  semgrep_config: ./rules\n"
        "  playwright_allow_ports: [5173, 3000]\n",
    )
    cfg = load_config(root=tmp_path, env_file=None)
    assert cfg.services.enabled == ["semgrep", "gitleaks", "playwright_local"]
    assert cfg.services.semgrep_config == "./rules"
    assert cfg.services.playwright_allow_ports == [5173, 3000]


def test_providers_config_defaults(tmp_path: Path) -> None:
    cfg = load_config(root=tmp_path, env_file=None)
    assert cfg.providers.enabled == []  # fail-closed: no opt-in provider on until listed
    assert cfg.providers.base_urls == {}


def test_providers_config_yaml_override(tmp_path: Path) -> None:
    # Regression (f086cc6 class): load_config must actually read the providers: block. If it
    # were dropped, providers.enabled would stay [] regardless and the 10C flag would be inert.
    _write_settings(
        tmp_path,
        "providers:\n"
        "  enabled: [deepseek, gemini]\n"
        "  base_urls:\n"
        "    deepseek: https://example.test/anthropic\n",
    )
    cfg = load_config(root=tmp_path, env_file=None)
    assert cfg.providers.enabled == ["deepseek", "gemini"]
    assert cfg.providers.base_urls == {"deepseek": "https://example.test/anthropic"}


def test_skills_config_is_off_by_default_and_accepts_human_pins(tmp_path: Path) -> None:
    assert load_config(root=tmp_path, env_file=None).skills.mode == "off"
    _write_settings(
        tmp_path,
        "skills:\n"
        "  mode: shadow\n"
        "  enabled:\n"
        "    - pack: core-engineering\n"
        "      version: 1.0.0\n"
        "      sha256: 0123456789ab\n",
    )
    cfg = load_config(root=tmp_path, env_file=None)
    assert cfg.skills.mode == "shadow"
    assert cfg.skills.enabled[0].pack == "core-engineering"


def test_skills_reject_unsafe_pack_pins(tmp_path: Path) -> None:
    _write_settings(
        tmp_path,
        "skills:\n"
        "  mode: active\n"
        "  enabled:\n"
        "    - pack: ../escape\n"
        "      version: 1.0.0\n"
        "      sha256: not-a-hash\n",
    )
    with pytest.raises(ConfigError):
        load_config(root=tmp_path, env_file=None)


def test_provider_secrets_read_from_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = load_config(root=tmp_path, env_file=None)
    assert cfg.secrets.deepseek_api_key == ""  # default empty
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-ds")
    monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-qw")
    monkeypatch.setenv("ZAI_API_KEY", "sk-zai")
    monkeypatch.setenv("GEMINI_API_KEY", "sk-gm")
    cfg2 = load_config(root=tmp_path, env_file=None)
    assert cfg2.secrets.deepseek_api_key == "sk-ds"
    assert cfg2.secrets.dashscope_api_key == "sk-qw"
    cfg2.require("deepseek", "qwen", "zai", "gemini")  # all present ⇒ no raise


def test_budgets_config_yaml_override(tmp_path: Path) -> None:
    # Regression: the budgets: block was likewise dropped by load_config.
    _write_settings(
        tmp_path,
        "budgets:\n"
        "  hard_stop_usd_per_run: 12.5\n"
        "  confirm_above_usd: 3.0\n"
        "  treat_unpriced_as_blocking: false\n",
    )
    cfg = load_config(root=tmp_path, env_file=None)
    assert cfg.budgets.hard_stop_usd_per_run == 12.5
    assert cfg.budgets.confirm_above_usd == 3.0
    assert cfg.budgets.treat_unpriced_as_blocking is False
    assert cfg.budgets.soft_warn_usd_per_run == 1.0  # unspecified keeps default


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
