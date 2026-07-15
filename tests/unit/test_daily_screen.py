"""Daily briefing pins — it guides to Chat and detail homes without becoming a dashboard."""

from __future__ import annotations

from kira.ui.server import STATIC_DIR

DAILY = (STATIC_DIR / "screens" / "daily.js").read_text(encoding="utf-8")
APP = (STATIC_DIR / "app.js").read_text(encoding="utf-8")


def test_daily_is_a_briefing_that_routes_to_chat_and_detail_homes() -> None:
    assert 'href="#chat">Continue chat' in DAILY
    for route in ("#hub", "#studio", "#costs", "#vault", "#tasks"):
        assert route in DAILY
    assert 'id="daily-pending"' in DAILY
    assert DAILY.index('id="daily-pending"') < DAILY.index('id="daily-status"')


def test_daily_has_no_duplicate_chat_or_unattended_mutation_surface() -> None:
    for removed in ("daily-chat", "composer-input", "daily-convo-header", "daily-workflows",
                    "daily-artifacts", "daily-run", "daily-connectors", "daily-changed"):
        assert removed not in DAILY
    # Daily exposes only the existing, explicit briefing refresh — never chat turns or writes.
    assert 'api.post("/api/digest/run", {})' in DAILY
    assert DAILY.count("api.post(") == 1
    assert 'api.post("/api/turn"' not in DAILY


def test_briefing_and_notices_use_safe_text_nodes() -> None:
    assert "summary.textContent = digest.summary" in DAILY
    assert "title.textContent = notice.title" in DAILY
    assert "innerHTML =" not in DAILY.split("function renderPending")[1]


def test_demo_badge_present() -> None:
    assert "No briefing yet" in DAILY


def test_app_routes_notice_messages() -> None:
    # Background notices reach the browser and a digest notice refreshes Daily.
    assert 'msg.kind === "notice"' in APP
    assert "onNotice" in APP
