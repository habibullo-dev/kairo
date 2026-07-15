"""Operator-guide pins: commands are real and safety/recovery claims stay conservative."""

from jarvis.ui.server import STATIC_DIR

REPOSITORY_ROOT = STATIC_DIR.parents[3]
GUIDE = (REPOSITORY_ROOT / "docs" / "KIRA-USER-GUIDE.md").read_text(encoding="utf-8")
NORMALIZED_GUIDE = " ".join(GUIDE.split())
README = (REPOSITORY_ROOT / "README.md").read_text(encoding="utf-8")


def test_user_guide_uses_canonical_cli_rituals() -> None:
    for command in (
        "uv run kira --ui",
        "uv run kira connect google",
        "uv run kira connect status",
        "uv run kira connect telegram --test",
        "uv run kira connect kakao --test",
        "uv run kira dream run morning_briefing",
        "uv run kira backup create",
        "uv run kira backup verify",
        "uv run kira reset data",
    ):
        assert command in GUIDE

    assert "uv run jarvis" not in GUIDE


def test_user_guide_describes_current_identity_auth_and_storage() -> None:
    for claim in (
        "# Kira User Guide",
        "data/kira.db",
        "Kira backup format v2",
        "10 minutes",
        "Argon2id",
        "ANTHROPIC_API_KEY",
        "30-day idle",
        "90-day absolute",
        "schema-v33",
        "not scheduled unattended",
    ):
        assert claim in NORMALIZED_GUIDE

    for legacy_brand in ("Kairo", "Cairo", "Jarvis"):
        assert legacy_brand not in GUIDE


def test_user_guide_keeps_non_negotiable_safety_claims() -> None:
    for claim in (
        "drafts only",
        "cannot send email",
        "no voice-only approval",
        "preview → approve → execute",
        "Restore is not supported",
    ):
        assert claim in GUIDE


def test_user_guide_pins_remote_operator_runtime_boundaries() -> None:
    for claim in (
        "At every controller start",
        "only after the runtime reports the channel ready",
        "live-search, or other egress authority",
        "without per-query approval or semantic DLP",
        "not as a filesystem sandbox",
        "A write or shell call always parks the exact saved continuation",
    ):
        assert claim in NORMALIZED_GUIDE


def test_readme_reports_the_current_kira_release() -> None:
    for claim in (
        "# Kira",
        "**Kira 0.1.0** uses database schema **v33**",
        "Phase 16 Tasks 1–9",
        "Checkpoint K",
        "Dreaming is **NOT scheduled**",
        "data/kira.db",
        "logs/kira-YYYY-MM-DD.jsonl",
        "keyless cassette replay",
        "Chat is the shipped default screen",
        "ui.enabled: false",
        "`ANTHROPIC_API_KEY` is required to start Kira",
    ):
        assert claim in README

    for legacy_brand in ("Kairo", "Cairo", "Jarvis"):
        assert legacy_brand not in README


def test_backup_verification_example_matches_the_v2_directory_name() -> None:
    command = "uv run kira backup verify data/backups/kira-backup-<timestamp>-manual-<id>"

    assert command in GUIDE
    assert command in README


def test_attended_dreaming_requires_the_runtime_to_be_stopped() -> None:
    assert "Stop Kira before running one attended dreaming command:" in GUIDE
    assert "Stop Kira before exercising one of the five proposal-only jobs manually:" in README
