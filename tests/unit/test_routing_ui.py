"""Phase 15.6 Task 7: the routing UI surface — /api/models routing output + POST /api/model
policy switching. Auto is the default; the picker shows honest enabled/disabled reasons; no secret
leaks. Keyless (bare create_app + a manually-wired RoutingState/InteractiveModelState)."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from kira.config import load_config
from kira.ui.auth import SESSION_COOKIE, AuthManager
from kira.ui.readmodels import interactive_models
from kira.ui.server import create_app
from kira.ui.state import InteractiveModelState


def _hdr(auth: AuthManager, *, post: bool = False) -> dict[str, str]:
    h = {"cookie": f"{SESSION_COOKIE}={auth.mint_session()}"}
    if post:
        h["origin"] = "http://127.0.0.1"
    return h


def _client(tmp_path: Path):
    cfg = load_config(root=tmp_path, env_file=None)
    auth = AuthManager(token="tok")
    app = create_app(cfg, auth=auth)
    from kira.routing import RoutingState

    app.state.interactive_models = InteractiveModelState(cfg.models.main)
    app.state.routing = RoutingState()  # default AUTO
    app.state.last_route = None
    return TestClient(app, base_url="http://127.0.0.1"), app, auth


# --- read model: Auto default + recommended + honest reasons ----------------
def test_interactive_models_defaults_to_auto_recommended(tmp_path: Path) -> None:
    m = interactive_models(load_config(root=tmp_path, env_file=None), policy="auto")
    assert m["policy"] == "auto"
    assert m["auto"]["recommended"] is True and m["auto"]["current"] is True
    assert m["auto"]["description"] == "uses cheap models first, escalates only when needed"


def test_external_reasons_distinguish_textonly_from_nonprivate(tmp_path: Path) -> None:
    m = interactive_models(load_config(root=tmp_path, env_file=None), policy="auto")
    reasons = {e["id"]: e["reason"] for e in m["external"]}
    # private_ok-but-text-only providers: Auto uses them, not a manual pick.
    assert "text-only" in reasons["gemini"] and "text-only" in reasons["openai"]
    # non-private workers: never receive the private main chat.
    for name in ("qwen", "deepseek", "zai"):
        assert "not allowed for private context" in reasons[name]
    # none is selectable as a manual main-chat pick
    assert all(not e["selectable"] for e in m["external"])


# --- routes: GET reflects policy; POST switches auto<->manual ---------------
def test_get_models_reflects_auto_default(tmp_path: Path) -> None:
    client, _app, auth = _client(tmp_path)
    body = client.get("/api/models", headers=_hdr(auth)).json()
    assert body["policy"] == "auto" and body["auto"]["current"] is True


def test_post_model_auto_and_manual_switch(tmp_path: Path) -> None:
    client, app, auth = _client(tmp_path)
    # Pin a manual model ⇒ MANUAL.
    r = client.post("/api/model", json={"model": "claude-opus-4-8"}, headers=_hdr(auth, post=True))
    assert r.status_code == 200 and r.json()["policy"] == "manual"
    assert app.state.routing.mode().value == "manual"
    assert client.get("/api/models", headers=_hdr(auth)).json()["current"] == "claude-opus-4-8"
    # Back to Auto.
    r = client.post("/api/model", json={"model": "auto"}, headers=_hdr(auth, post=True))
    assert r.status_code == 200 and r.json()["policy"] == "auto"
    assert app.state.routing.mode().value == "auto"


def test_post_model_rejects_non_allowlisted(tmp_path: Path) -> None:
    # A non-private / text-only provider is never a manual main-chat pick (Anthropic allowlist).
    client, app, auth = _client(tmp_path)
    for bad in ("gpt-5.2", "gemini-2.5-flash", "qwen3-coder-plus"):
        r = client.post("/api/model", json={"model": bad}, headers=_hdr(auth, post=True))
        assert r.status_code == 400, bad
    assert app.state.routing.mode().value == "auto"  # a rejected pin never changes policy


def test_no_secret_on_models_route(tmp_path: Path) -> None:
    cfg = load_config(root=tmp_path, env_file=None)
    cfg.secrets = cfg.secrets.model_copy(update={"gemini_api_key": "SECRET-CANARY-GEMINI"})
    auth = AuthManager(token="tok")
    app = create_app(cfg, auth=auth)
    app.state.interactive_models = InteractiveModelState(cfg.models.main)
    from kira.routing import RoutingState

    app.state.routing = RoutingState()
    app.state.last_route = None
    client = TestClient(app, base_url="http://127.0.0.1")
    text = client.get("/api/models", headers=_hdr(auth)).text
    assert "SECRET-CANARY-GEMINI" not in text
