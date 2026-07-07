"""daily_overview + hub connector status (Phase 9 Task 8) — keyless read models."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

from jarvis.config import load_config
from jarvis.connectors.base import ConnectorRegistry
from jarvis.connectors.demo import DemoGoogleClient, DemoNotifier
from jarvis.ui.auth import SESSION_COOKIE, AuthManager
from jarvis.ui.readmodels import UiServices, daily_overview, hub_status
from jarvis.ui.server import create_app


def _cfg(tmp_path: Path):
    return load_config(root=tmp_path, env_file=None)


async def test_daily_overview_shape_with_no_services(tmp_path: Path) -> None:
    out = await daily_overview(_cfg(tmp_path), UiServices(), gate_pending=3)
    assert out["pending_approvals"] == 3
    assert out["tasks_today"] == [] and out["kb_review_count"] == 0
    assert out["digest"] is None and out["notices"] == []
    assert out["demo"] is False
    # tmp_path isn't a git repo ⇒ the "." repo state is None (never an error).
    assert out["repos"] == [{"path": ".", "state": None}]
    assert out["evals"]["ever_run"] is False
    assert out["evals"]["command"] == "jarvis eval gate"  # copy-command, never a run button


async def test_daily_overview_surfaces_digest_and_demo(tmp_path: Path) -> None:
    latest = SimpleNamespace(
        date_local="2026-07-06",
        generated_at="2026-07-06T08:00:00+00:00",
        summary="[DEMO] All calm.",
        suggested_actions=["Reply to Bob"],
        sections=[],
        delivered_to=["ui"],
    )

    class _Digests:
        async def latest(self):
            return latest

    services = UiServices(
        connectors=ConnectorRegistry(
            google=DemoGoogleClient(), notifiers={"telegram": DemoNotifier("telegram")}, demo=True
        ),
        digests=_Digests(),
    )
    out = await daily_overview(_cfg(tmp_path), services)
    assert out["digest"]["summary"] == "[DEMO] All calm."
    assert out["digest"]["suggested_actions"] == ["Reply to Bob"]
    assert out["demo"] is True and out["connectors"]["demo"] is True


async def test_eval_freshness_stale_when_head_moved(tmp_path: Path) -> None:
    # A gated rev that differs from HEAD ⇒ stale. (Synthesised repo state + history line.)
    cfg = _cfg(tmp_path)
    (cfg.data_dir / "evals").mkdir(parents=True, exist_ok=True)
    (cfg.data_dir / "evals" / "history.jsonl").write_text(
        '{"git_rev": "oldrev", "verdict": "PASS", "timestamp": "2026-07-01T00:00:00"}\n',
        encoding="utf-8",
    )
    from jarvis.ui.readmodels import _eval_freshness

    fresh = _eval_freshness(cfg, [{"path": ".", "state": {"head_rev": "oldrev"}}])
    assert fresh["stale"] is False and fresh["verdict"] == "PASS"
    stale = _eval_freshness(cfg, [{"path": ".", "state": {"head_rev": "newrev"}}])
    assert stale["stale"] is True


def test_hub_status_carries_connector_status_not_secrets(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    connectors = ConnectorRegistry(
        notifiers={"telegram": DemoNotifier("telegram")}, demo=True
    ).status()
    hub = hub_status(cfg, connectors=connectors)
    assert hub["connectors"]["demo"] is True
    assert "telegram" in hub["connectors"]["notifiers"]
    assert hub["mcp"]["connected"] is False  # honest stub unchanged


def test_hub_status_defaults_connectors_when_absent(tmp_path: Path) -> None:
    hub = hub_status(_cfg(tmp_path))
    assert hub["connectors"] == {"demo": False, "google": None, "notifiers": {}}


def test_daily_route_is_served_and_session_gated(tmp_path: Path) -> None:
    auth = AuthManager(token="t")
    app = create_app(_cfg(tmp_path), auth=auth)
    client = TestClient(app, base_url="http://127.0.0.1")
    assert client.get("/api/daily").status_code == 401  # no session
    r = client.get("/api/daily", headers={"cookie": f"{SESSION_COOKIE}={auth.mint_session()}"})
    assert r.status_code == 200
    body = r.json()
    assert "repos" in body and "evals" in body and "connectors" in body
