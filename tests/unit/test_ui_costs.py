"""Cost Center pins (Phase 11 T13) — budget-warning banner, ROI aggregate + per-run list, all
dimensions, and the read-only /api/roi route. Read-only screen; metadata only; escaped rendering."""

from __future__ import annotations

from pathlib import Path

from kira.ui import server as server_mod
from kira.ui.server import STATIC_DIR

COSTS = (STATIC_DIR / "screens" / "costs.js").read_text(encoding="utf-8")
SERVER = Path(server_mod.__file__).read_text(encoding="utf-8")


def test_budget_banner_present_and_not_amber() -> None:
    # cost = teal monitoring; a hard breach is danger (red). Amber stays approval-only.
    assert "budget-banner" in COSTS and "budget_warning" in COSTS
    assert '"danger"' in COSTS and '"cost"' in COSTS
    assert "amber" not in COSTS and "attention" not in COSTS


def test_roi_aggregate_and_per_run() -> None:
    assert "/api/roi" in COSTS and "net_usd" in COSTS and "review_accepted" in COSTS
    assert "outcome_accounting" in COSTS and "Model-cost accounting" in COSTS
    assert "estimate_accuracy" in COSTS and "Estimate calibration" in COSTS
    assert "never changes pricing, routing, or budget limits automatically" in COSTS


def test_all_dimensions_present() -> None:
    for d in ("by_project", "by_model", "by_provider", "by_team", "by_role",
              "by_stage", "by_purpose", "by_service"):
        assert d in COSTS, d


def test_model_request_health_is_read_only_and_truthful() -> None:
    assert "model_request_health" in COSTS and "Model request health" in COSTS
    assert "Daily health (UTC)" in COSTS and "day.by_provider_model" in COSTS
    assert "Completed model-request latency only" in COSTS
    assert "end-to-end turn time" in COSTS and "Failure classes" in COSTS
    assert "telemetry_complete" in COSTS
    assert "exact counts, error rate, and latency percentiles" in COSTS


def test_escaped_no_innerhtml_injection() -> None:
    # Dimension keys (including user-controlled project names) go through esc(String(...)).
    assert "esc(String(" in COSTS
    assert 'import { esc } from "../ui/dom.js"' in COSTS


def test_roi_route_is_get_only() -> None:
    assert '@app.get("/api/roi")' in SERVER
    assert '@app.post("/api/roi")' not in SERVER
    assert "orchestration_outcome_accounting" in SERVER
    assert "orchestration_estimate_accuracy" in SERVER
