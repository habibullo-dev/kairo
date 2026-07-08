"""Settings status-sections pins (Phase 11 T14).

Settings gains read-only status sections (providers/routes, services, connectors, budgets,
privacy/safety) sourced from the EXISTING /api/hub, /api/costs, /api/runner reads, plus a
presentation-only debug/trace toggle. Presence booleans only — never a key value. No mutation.
"""

from __future__ import annotations

from jarvis.ui.server import STATIC_DIR

SET = (STATIC_DIR / "screens" / "settings.js").read_text(encoding="utf-8")


def test_status_sections_read_existing_endpoints_only() -> None:
    assert "/api/hub" in SET and "/api/costs" in SET and "/api/runner" in SET
    assert "api.post" not in SET  # reads + client-side appearance only — never mutates


def test_all_status_sections_present() -> None:
    for section in ("Providers & model routes", "Services", "Connectors",
                    "Budgets & cost ledger", "Privacy & safety"):
        assert section in SET, section


def test_debug_toggle_is_presentation_only() -> None:
    assert "debugRow" in SET
    assert 'classList.remove("debug")' in SET and 'classList.add("debug")' in SET


def test_presence_only_rendering() -> None:
    # providers/connectors are rendered as presence pills (booleans), never a value.
    assert "presencePill" in SET
