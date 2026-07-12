"""Daily command-center pins (Phase 11 T8).

T8 grows Daily into the §4 command center: the existing pending/Now/Briefing/Today cards plus
recent artifacts, latest orchestration run, cost today, notices and connector health — each with
a designed empty state. It stays calm (one primary attention surface = the amber pending zone),
and permits only an explicit attended briefing refresh through its existing route. It never
linkifies untrusted content. The load-bearing WS contract is unchanged.
"""

from __future__ import annotations

from jarvis.ui.server import STATIC_DIR

DAILY_JS = (STATIC_DIR / "screens" / "daily.js").read_text(encoding="utf-8")
CONVERSATION_JS = (STATIC_DIR / "screens" / "conversation.js").read_text(encoding="utf-8")
APP_JS = (STATIC_DIR / "app.js").read_text(encoding="utf-8")


def test_calm_briefing_cards_present() -> None:
    cards = ("daily-briefing", "daily-project", "daily-today", "daily-notice", "daily-cost-today")
    for cid in cards:
        assert cid in DAILY_JS, cid


def test_cost_today_shares_the_settled_runner_state() -> None:
    # The Daily "spent today" metric is written from the same renderRunnerState() source as the
    # status bar (never a second poll / divergent value).
    assert "daily-cost-today" in APP_JS
    assert "today_spend_usd" in APP_JS
    assert "Your briefing is up to date." in APP_JS
    assert "Send a message to begin." not in APP_JS


def test_every_card_has_a_designed_empty_state() -> None:
    # "Nothing empty": a briefing tells the person where to go next.
    assert 'class = "empty-state"' in DAILY_JS or 'className = "empty-state"' in DAILY_JS
    for phrase in ("No next tasks", "No briefing yet", "No new notifications", "Working globally"):
        assert phrase in DAILY_JS, phrase


def test_daily_refreshes_only_the_existing_attended_briefing_route() -> None:
    # Daily never submits a chat turn or any outward action. Refresh is a deliberate model call
    # through the already-pinned digest endpoint and is disabled while another turn is busy.
    assert 'api.post("/api/digest/run", {})' in DAILY_JS
    assert "daily-briefing-refresh" in DAILY_JS
    assert "briefingRefreshInFlight" in DAILY_JS
    assert "result.status === 409" in DAILY_JS
    assert "projectScoped" in DAILY_JS and "Global only" in DAILY_JS
    assert "Open the global workspace to refresh the daily briefing." in DAILY_JS
    assert 'api.post("/api/turn"' not in DAILY_JS
    assert 'from "./conversation.js"' not in DAILY_JS
    assert CONVERSATION_JS.count('api.post("/api/turn"') == 1


def test_resume_helper_loads_transcript() -> None:
    # api.resumeChat resumes the session AND loads its transcript into the conversation view, so
    # resuming actually shows the chat — via existing routes only.
    assert "async resumeChat(" in APP_JS
    assert "/resume" in APP_JS and "/api/sessions/" in APP_JS
    assert "state.chat =" in APP_JS


def test_untrusted_content_is_never_linkified() -> None:
    # Digest/notice data is never made into a link or a browser-open action.
    assert "window.open" not in DAILY_JS
    assert "external_uri" not in DAILY_JS


def test_priority_order_pending_before_activity() -> None:
    # The amber pending zone stays the single primary attention surface, above status/briefing.
    assert DAILY_JS.index('id="daily-pending"') < DAILY_JS.index('id="daily-status"')


def test_daily_overview_failure_shows_unavailable_not_loading() -> None:
    # A failed /api/daily must never leave cards stuck on "Loading…" forever.
    assert "Briefing unavailable" in DAILY_JS
