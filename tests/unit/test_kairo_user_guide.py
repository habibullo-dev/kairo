"""Operator guide pins: commands are real and its safety claims stay conservative."""

from jarvis.ui.server import STATIC_DIR

GUIDE = (STATIC_DIR.parents[3] / "docs" / "KAIRO-USER-GUIDE.md").read_text(encoding="utf-8")


def test_user_guide_uses_real_cli_rituals() -> None:
    for command in (
        "uv run jarvis --ui",
        "uv run jarvis connect google",
        "uv run jarvis connect status",
        "uv run jarvis connect telegram --test",
        "uv run jarvis connect kakao --test",
        "uv run jarvis backup create",
        "uv run jarvis backup verify",
    ):
        assert command in GUIDE


def test_user_guide_keeps_non_negotiable_safety_claims() -> None:
    for claim in (
        "drafts only",
        "cannot send email",
        "no voice-only approval",
        "preview → approve → execute",
        "not scheduled",
        "verify/dry-run only",
    ):
        assert claim in GUIDE
