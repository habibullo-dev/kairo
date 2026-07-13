"""Notification actions must distinguish authorization from list hygiene."""

from jarvis.ui.server import STATIC_DIR

GATE = (STATIC_DIR / "screens" / "gate.js").read_text(encoding="utf-8")
PALETTE = (STATIC_DIR / "ui" / "palette.js").read_text(encoding="utf-8")


def test_notifications_explains_authorization_vs_clearing() -> None:
    assert "Approve & send” authorizes a write" in GATE
    assert "Clear from list" in GATE
    assert 'action: "done"' in GATE
    assert "Configured external nudges are count-only" in GATE
    assert "never authorize an action" in GATE


def test_palette_uses_the_shipped_notifications_name() -> None:
    assert '["gate", "Notifications", "Approvals and background activity"]' in PALETTE
