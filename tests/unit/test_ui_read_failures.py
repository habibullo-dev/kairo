"""Failed reads must not quietly render as empty product data."""

from jarvis.ui.server import STATIC_DIR

APP = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
DAILY = (STATIC_DIR / "screens" / "daily.js").read_text(encoding="utf-8")
HEADER = (STATIC_DIR / "ui" / "header.js").read_text(encoding="utf-8")
SETTINGS = (STATIC_DIR / "screens" / "settings.js").read_text(encoding="utf-8")


def test_read_helper_degrades_to_null_for_screen_level_error_states() -> None:
    assert "async get(path)" in APP
    assert "catch {\n      return null;" in APP


def test_daily_tasks_distinguishes_an_error_from_no_tasks() -> None:
    assert "Tasks unavailable" in DAILY
    assert "Kairo couldn't load scheduled work right now." in DAILY


def test_header_and_settings_explain_partial_read_failures() -> None:
    assert "Status unavailable" in HEADER
    assert "statusUnavailable" in HEADER
    assert "Some status is unavailable" in SETTINGS
    assert "The remaining status is shown below." in SETTINGS
