"""Daily command-center pins (Phase 11 T8).

T8 grows Daily into the §4 command center: the existing pending/Now/Briefing/Today cards plus
recent artifacts, latest orchestration run, cost today, notices and connector health — each with
a designed empty state. It stays calm (one primary attention surface = the amber pending zone),
reads-only (the sole action path is the gated POST /api/turn), and never linkifies untrusted
content. The load-bearing WS contract (test_ui_refinement / test_ui_activity_settle) is unchanged.
"""

from __future__ import annotations

from jarvis.ui.server import STATIC_DIR

DAILY_JS = (STATIC_DIR / "screens" / "daily.js").read_text(encoding="utf-8")
APP_JS = (STATIC_DIR / "app.js").read_text(encoding="utf-8")


def test_new_command_center_cards_present() -> None:
    cards = (
        "daily-artifacts", "daily-run", "daily-notices",
        "daily-connectors", "daily-cost-today",
    )
    for cid in cards:
        assert cid in DAILY_JS, cid


def test_cost_today_shares_the_settled_runner_state() -> None:
    # The Daily "spent today" metric is written from the same renderRunnerState() source as the
    # status bar (never a second poll / divergent value).
    assert "daily-cost-today" in APP_JS
    assert "today_spend_usd" in APP_JS


def test_every_card_has_a_designed_empty_state() -> None:
    # "Nothing empty": the new cards teach the next action when they have no data.
    assert 'class = "empty-state"' in DAILY_JS or 'className = "empty-state"' in DAILY_JS
    for phrase in ("No artifacts yet", "No runs yet", "No briefing yet", "No messages yet"):
        assert phrase in DAILY_JS, phrase


def test_daily_reads_and_navigates_only() -> None:
    # Daily's only mutations are the gated turn and the (pre-existing) run-digest action — both
    # existing server routes; T8 adds NO new mutation. Every new card reads or navigates.
    assert DAILY_JS.count("api.post(") == 2
    assert "/api/turn" in DAILY_JS and "/api/digest/run" in DAILY_JS
    # Artifacts open the hardened read-only content GET; runs navigate to Studio (hash).
    assert "/api/artifacts/" in DAILY_JS and "/content" in DAILY_JS and "noopener" in DAILY_JS


def test_untrusted_content_is_never_linkified() -> None:
    # A digest/artifact external_uri must NOT be auto-opened (a linkified digest = phishing/exfil
    # surface). Only a locally-stored artifact (has_content) opens its same-origin content route.
    assert "a.has_content" in DAILY_JS
    # window.open only ever targets the same-origin /api/artifacts content route.
    assert "window.open(`/api/artifacts/" in DAILY_JS
    assert "window.open(a.external_uri" not in DAILY_JS
    assert "window.open(r.external_uri" not in DAILY_JS


def test_priority_order_pending_before_activity() -> None:
    # The amber pending zone stays the single primary attention surface, above the Now card.
    assert DAILY_JS.index("zone-pending") < DAILY_JS.index("Kairo is idle")


def test_connector_dot_reflects_real_status() -> None:
    # A disconnected connector must NOT show a green (connected) dot; connected-state is derived
    # from the registry status-dict fields, not object-truthiness, and honours needs_reconnect.
    assert '" off"' in DAILY_JS  # the grey off-dot is actually applied
    assert "connOn(" in DAILY_JS and "needs_reconnect" in DAILY_JS


def test_run_tone_uses_real_status_vocabulary() -> None:
    # OrchestrationRun statuses are ok/error/rejected/…, never completed/failed.
    assert '"ok"' in DAILY_JS and '"budget_stopped"' in DAILY_JS
    assert '"completed"' not in DAILY_JS and '"failed"' not in DAILY_JS


def test_artifact_icons_cover_the_common_kinds() -> None:
    # orchestration + meeting_note are the most-emitted kinds — they must have real icons.
    assert "orchestration:" in DAILY_JS and "meeting_note:" in DAILY_JS


def test_daily_overview_failure_shows_unavailable_not_loading() -> None:
    # A failed /api/daily must never leave cards stuck on "Loading…" forever.
    assert "Unavailable" in DAILY_JS
