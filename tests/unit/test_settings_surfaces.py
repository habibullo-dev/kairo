"""Settings maturity read model (Phase 13 Task 7). The `/api/settings` policy surface is
READ-ONLY and presence/state/NAMES only — never a key value. (The route-level sweep across every
GET lives in test_ui_readmodels.test_no_secret_crosses_the_wire_on_any_get, extended with the
research-service key canaries; this file pins the read model directly + its own value sweep.)"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from jarvis.config import load_config
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


def test_budgets_include_service_caps_slot(tmp_path: Path) -> None:
    # Task 7 exposes the per-service cap slots (None until Task 8 adds them to ServicesConfig).
    b = settings_overview(_cfg(tmp_path))["budgets"]
    assert "service_max_usd_per_run" in b and "service_max_usd_per_day" in b
    assert b["service_max_usd_per_run"] is None and b["service_max_usd_per_day"] is None
    assert "hard_stop_usd_per_run" in b  # existing budget limits are surfaced too


@pytest.mark.parametrize("enabled", [(), ("firecrawl",), ("firecrawl", "exa", "openai_image")])
def test_stable_shape_across_enable_sets(tmp_path: Path, enabled) -> None:
    ov = settings_overview(_cfg(tmp_path, enabled=enabled))
    assert {"providers", "services", "model_routes", "budgets", "connectors",
            "context_reuse", "enable_hint", "services_enabled"} <= set(ov)
    assert ov["services_enabled"] == list(enabled)
