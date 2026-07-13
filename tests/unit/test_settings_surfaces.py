"""Settings maturity read model (Phase 13 Task 7). The `/api/settings` policy surface is
READ-ONLY and presence/state/NAMES only — never a key value. (The route-level sweep across every
GET lives in test_ui_readmodels.test_no_secret_crosses_the_wire_on_any_get, extended with the
research-service key canaries; this file pins the read model directly + its own value sweep.)"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from jarvis.config import SkillActivationConfig, SkillsConfig, load_config
from jarvis.permissions.policy import Policy
from jarvis.tools import Permission
from jarvis.ui.readmodels import settings_overview


def _cfg(tmp_path: Path, *, enabled=("firecrawl",)):
    cfg = load_config(root=tmp_path, env_file=None)
    cfg.services.enabled = list(enabled)
    return cfg


def test_presence_and_state_only_never_a_key_value(tmp_path: Path, monkeypatch) -> None:
    # Seed canaries in BOTH the typed Secrets and the environment; none may appear in the surface.
    cfg = _cfg(tmp_path)
    cfg.secrets = cfg.secrets.model_copy(
        update={
            "firecrawl_api_key": "CANARY-FC-VALUE",
            "exa_api_key": "CANARY-EXA-VALUE",
            "openai_api_key": "CANARY-OAI-VALUE",
        }
    )
    for var, val in (("FIRECRAWL_API_KEY", "CANARY-FC-ENV"), ("EXA_API_KEY", "CANARY-EXA-ENV")):
        monkeypatch.setenv(var, val)
    blob = json.dumps(settings_overview(cfg))
    for canary in ("CANARY-FC-VALUE", "CANARY-EXA-VALUE", "CANARY-OAI-VALUE", "CANARY-FC-ENV",
                   "CANARY-EXA-ENV"):
        assert canary not in blob, f"{canary} leaked into the settings surface"


def test_services_surface_full_policy_and_env_names(tmp_path: Path) -> None:
    ov = settings_overview(_cfg(tmp_path))
    fc = next(s for s in ov["services"] if s["name"] == "firecrawl")
    # availability state (the WHY-not-available) + policy badges + credential env NAMES
    assert fc["state"] in {"available", "disabled", "missing_credentials", "unpriced", "deferred"}
    assert fc["egress"] is True
    assert fc["context_policy"] == "public_only"
    assert fc["output_trust"] == "untrusted_external_content"
    assert fc["credential_env"] == ["FIRECRAWL_API_KEY"]  # NAME only


def test_providers_surface_authority_and_private_ok(tmp_path: Path) -> None:
    ov = settings_overview(_cfg(tmp_path))
    anth = next(p for p in ov["providers"] if p["name"] == "anthropic")
    assert "state" in anth and "trusted_authority" in anth and "private_ok" in anth
    assert "api_key" not in json.dumps(anth).lower() or "CANARY" not in json.dumps(anth)


def test_enable_hint_and_context_reuse_flag(tmp_path: Path) -> None:
    ov = settings_overview(_cfg(tmp_path, enabled=()))
    # Global flags are file-only: the surface tells the human the exact settings.yaml line.
    assert "services:" in ov["enable_hint"] and "enabled" in ov["enable_hint"]
    assert ov["services_enabled"] == []
    assert ov["context_reuse"]["enabled"] is False


def test_configured_policy_shows_only_explicit_nondefault_tool_decisions(tmp_path: Path) -> None:
    policy = Policy(
        default=Permission.ASK,
        tools={"web_search": Permission.ALLOW, "web_fetch": Permission.ASK},
    )
    assert settings_overview(_cfg(tmp_path), policy=policy)["configured_policy"] == {
        "state": "available",
        "scope": "configured_policy_only",
        "global_default": "ask",
        "overrides": [{"tool": "web_search", "decision": "allow"}],
    }


def test_configured_policy_never_claims_to_be_effective_per_call(tmp_path: Path) -> None:
    # read_file's intrinsic default is ALLOW, but an empty config policy correctly reports no
    # explicit override. The separate scope marker prevents callers from misreading this as the
    # effective result of PermissionGate.check("read_file", ...).
    configured = settings_overview(_cfg(tmp_path), policy=Policy())['configured_policy']
    assert configured["scope"] == "configured_policy_only"
    assert configured["global_default"] == "ask" and configured["overrides"] == []


def test_attention_push_routing_reports_disabled_without_a_selected_channel(tmp_path: Path) -> None:
    routing = settings_overview(_cfg(tmp_path))["attention_routing"]
    assert routing == {
        "state": "disabled",
        "reason": "No count-only attention push channels are configured.",
        "channels": {"urgent": [], "normal": [], "low": []},
    }


def test_attention_push_routing_requires_a_live_selected_notifier(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    cfg.attention = cfg.attention.model_copy(update={"urgent_channels": ["telegram"]})
    pending = settings_overview(cfg)["attention_routing"]
    assert pending["state"] == "configured_not_connected"
    live = settings_overview(cfg, connectors={"notifiers": {"telegram": {"configured": True}}})
    assert live["attention_routing"]["state"] == "active"
    assert live["attention_routing"]["live_channels"] == ["telegram"]
    assert "local Gate" in live["attention_routing"]["reason"]


def test_attention_push_routing_does_not_call_demo_delivery_live(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    cfg.attention = cfg.attention.model_copy(update={"urgent_channels": ["telegram"]})
    status = settings_overview(
        cfg,
        connectors={"demo": True, "notifiers": {"telegram": {"configured": True, "demo": True}}},
    )["attention_routing"]
    assert status["state"] == "demo"
    assert "no Telegram/Kakao message leaves" in status["reason"]


def test_skills_surface_is_configuration_only_and_never_loads_packs(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    # This deliberately malformed pack must not be read: the Settings read model returns only the
    # human-pinned config projection without constructing a SkillCatalog or touching disk.
    pack = tmp_path / "config" / "skills" / "packs" / "backend-engineering.md"
    pack.parent.mkdir(parents=True)
    pack.write_text("not a valid skill pack", encoding="utf-8")
    cfg.skills = SkillsConfig(
        mode="active",
        enabled=[
            SkillActivationConfig(
                pack="backend-engineering", version="2.1.0", sha256="a" * 64
            )
        ],
    )
    assert settings_overview(cfg)["skills"] == {
        "mode": "active",
        "configured_packs": [
            {"pack": "backend-engineering", "version": "2.1.0", "sha256_prefix": "a" * 12}
        ],
    }


def test_budgets_include_service_caps(tmp_path: Path) -> None:
    # The surface exposes the per-service cost caps (Task 8 defaults on ServicesConfig) alongside
    # the existing per-run budget limits.
    cfg = _cfg(tmp_path)
    b = settings_overview(cfg)["budgets"]
    assert b["service_max_usd_per_run"] == cfg.services.max_usd_per_run  # e.g. 1.0
    assert b["service_max_usd_per_day"] == cfg.services.max_usd_per_day  # e.g. 5.0
    assert "hard_stop_usd_per_run" in b  # existing budget limits are surfaced too


@pytest.mark.parametrize("enabled", [(), ("firecrawl",), ("firecrawl", "exa", "openai_image")])
def test_stable_shape_across_enable_sets(tmp_path: Path, enabled) -> None:
    ov = settings_overview(_cfg(tmp_path, enabled=enabled))
    assert {"providers", "services", "model_routes", "budgets", "connectors",
            "context_reuse", "skills", "configured_policy", "enable_hint",
            "services_enabled"} <= set(ov)
    assert ov["services_enabled"] == list(enabled)
