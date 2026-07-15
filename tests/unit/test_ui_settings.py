"""Settings status-sections pins (Phase 11 T14).

Settings gains read-only status sections (providers/routes, services, connectors, budgets,
privacy/safety) sourced from /api/hub, /api/costs, and the shared runner-status cache, plus a
presentation-only debug/trace toggle. Presence booleans only — never a key value. No mutation.
"""

from __future__ import annotations

from kira.ui.server import STATIC_DIR

SET = (STATIC_DIR / "screens" / "settings.js").read_text(encoding="utf-8")


def test_status_sections_read_existing_endpoints_only() -> None:
    assert "/api/hub" in SET and "/api/costs" in SET and "runnerStatus" in SET
    assert "api.post" not in SET  # reads + client-side appearance only — never mutates


def test_all_status_sections_present() -> None:
    for section in ("Providers & model routes", "Services", "Connectors",
                    "Configured policy overrides", "Budgets & cost ledger", "Skill Forge",
                    "Privacy & safety"):
        assert section in SET, section


def test_debug_toggle_is_presentation_only() -> None:
    assert "debugRow" in SET
    assert 'classList.remove("debug")' in SET and 'classList.add("debug")' in SET


def test_presence_only_rendering() -> None:
    # providers/connectors are rendered as presence pills (booleans), never a value.
    assert "presencePill" in SET


def test_skill_forge_is_configuration_only_and_read_only() -> None:
    assert "configured_packs" in SET and "Configured pins" in SET
    assert "runtime inactive" in SET and "never loads pack files" in SET
    assert "SkillCatalog" not in SET and "api.post" not in SET


def test_attention_routing_uses_the_server_truth_with_a_neutral_fallback() -> None:
    assert "attention_routing" in SET
    assert "No attention-routing status available." in SET


def test_configured_policy_is_read_only_and_does_not_claim_effective_permission() -> None:
    assert "configured_policy" in SET and "Explicit decisions" in SET
    assert "Configured policy only" in SET and "taint safety rules still apply" in SET
    assert "api.post" not in SET
