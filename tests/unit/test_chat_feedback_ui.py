"""Chat feedback and Gate approval presentation stay custom, quiet, and informed."""

from jarvis.ui.server import STATIC_DIR

CHAT = (STATIC_DIR / "screens" / "chat.js").read_text(encoding="utf-8")
FEEDBACK = (STATIC_DIR / "ui" / "feedback.js").read_text(encoding="utf-8")
APP = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
INDEX = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
CSS = (STATIC_DIR / "kira.css").read_text(encoding="utf-8")


def test_chat_uses_custom_dialogs_and_toasts_not_browser_chrome() -> None:
    assert 'from "../ui/feedback.js"' in CHAT
    assert "confirmDialog" in CHAT and "promptDialog" in CHAT and "showToast" in CHAT
    assert "window.confirm" not in CHAT and "window.prompt" not in CHAT
    assert "innerHTML" not in FEEDBACK
    assert "textContent" in FEEDBACK
    assert "/api/" not in FEEDBACK  # presentation-only; it grants no authority itself


def test_gate_is_concise_by_default_but_retains_disclosed_exact_details() -> None:
    assert "approvalCopy" in APP
    for phrase in (
        "Kira wants to create a Gmail draft.",
        "Kira wants to write a local file.",
        "Kira wants to use your terminal.",
        "Kira wants to search the web.",
    ):
        assert phrase in APP
    assert 'id="ap-request"' in INDEX
    assert 'id="ap-details"' in INDEX
    assert "Review technical details" in INDEX
    assert 'id="ap-payload"' in INDEX  # exact action remains available, never silently hidden
    assert "Allow once" in INDEX and "Always allow" in INDEX and "Don't allow" in INDEX
    assert 'document.getElementById("ap-details").open = false' in APP
    assert ".approval-details" in CSS and ".dialog-overlay" in CSS and ".toast-stack" in CSS
    assert "restoreFocus" in FEEDBACK and 'event.key !== "Tab"' in FEEDBACK
