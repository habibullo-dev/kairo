"""Connector truth is consistent across surfaces (Phase 15.5 Task 4).

Daily, Hub, and Settings all embed the SAME capability_truth (computed once, server-side), so they
can never disagree about what's connected or usable — the exact "Daily says none, Settings says
Google exists" bug. Each screen renders it. Secret-safe. Keyless TestClient over a bare app."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from kira.config import load_config
from kira.permissions import PermissionGate
from kira.permissions.policy import Policy
from kira.tools import Permission
from kira.ui.auth import SESSION_COOKIE, AuthManager
from kira.ui.server import STATIC_DIR, create_app


def _client(tmp_path: Path):
    cfg = load_config(root=tmp_path, env_file=None)
    cfg.secrets = cfg.secrets.model_copy(update={"anthropic_api_key": "SECRET-CANARY-HUB"})
    auth = AuthManager(token="tok")
    return TestClient(create_app(cfg, auth=auth), base_url="http://127.0.0.1"), auth


def _hdr(auth: AuthManager) -> dict[str, str]:
    return {"cookie": f"{SESSION_COOKIE}={auth.mint_session()}"}


def test_daily_hub_settings_embed_identical_connector_truth(tmp_path: Path) -> None:
    client, auth = _client(tmp_path)
    daily = client.get("/api/daily", headers=_hdr(auth)).json()
    hub = client.get("/api/hub", headers=_hdr(auth)).json()
    settings = client.get("/api/settings", headers=_hdr(auth)).json()
    route = client.get("/api/capabilities", headers=_hdr(auth)).json()
    groups = {"connectors", "providers", "services", "voice", "mcp"}
    for payload in (daily, hub, settings):
        assert "capabilities" in payload
        assert groups <= set(payload["capabilities"])
    # The connector rows are IDENTICAL across every surface — one source of truth.
    assert daily["capabilities"]["connectors"] == hub["capabilities"]["connectors"]
    assert hub["capabilities"]["connectors"] == settings["capabilities"]["connectors"]
    assert settings["capabilities"]["connectors"] == route["connectors"]


def test_capability_rows_carry_state_exposed_reason(tmp_path: Path) -> None:
    client, auth = _client(tmp_path)
    caps = client.get("/api/capabilities", headers=_hdr(auth)).json()
    for r in caps["connectors"] + caps["providers"] + caps["services"]:
        assert set(r) >= {"name", "state", "exposed_to_chat", "reason"}
    prov = {r["name"]: r for r in caps["providers"]}
    assert prov["Anthropic"]["exposed_to_chat"] is True  # anthropic IS the chat
    assert all(not r["exposed_to_chat"] for r in caps["providers"] if r["name"] != "Anthropic")


def test_new_and_changed_gets_leak_no_secret(tmp_path: Path) -> None:
    client, auth = _client(tmp_path)
    for path in ("/api/capabilities", "/api/models", "/api/daily", "/api/hub", "/api/settings"):
        assert "SECRET-CANARY-HUB" not in client.get(path, headers=_hdr(auth)).text, path


def test_settings_uses_the_active_gate_policy_not_a_separate_config_copy(tmp_path: Path) -> None:
    cfg = load_config(root=tmp_path, env_file=None)
    policy = Policy(default=Permission.ASK, tools={"web_search": Permission.ALLOW})
    app = create_app(cfg, auth=AuthManager(token="tok"), gate=PermissionGate(policy, tmp_path))
    auth = app.state.auth
    payload = TestClient(app, base_url="http://127.0.0.1").get("/api/settings", headers=_hdr(auth))
    assert payload.status_code == 200
    assert payload.json()["configured_policy"] == {
        "state": "available",
        "scope": "configured_policy_only",
        "global_default": "ask",
        "overrides": [{"tool": "web_search", "decision": "allow"}],
    }


def test_capability_details_live_in_hub_and_settings() -> None:
    # Daily is intentionally a calm briefing in Slice 3; it links to Hub rather than duplicating
    # connector/provider truth. Hub and Settings remain the rendering homes for the same read model.
    for name in ("hub", "settings"):
        js = (STATIC_DIR / "screens" / f"{name}.js").read_text(encoding="utf-8")
        assert "capabilities" in js, name
    daily = (STATIC_DIR / "screens" / "daily.js").read_text(encoding="utf-8")
    assert 'href="#hub"' in daily
