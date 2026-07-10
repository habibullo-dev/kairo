"""Chat lifecycle controls are compact UI affordances over existing session routes only."""

from jarvis.ui.server import STATIC_DIR


def test_chat_shows_identity_save_state_and_existing_lifecycle_actions() -> None:
    source = (STATIC_DIR / "screens" / "chat.js").read_text(encoding="utf-8")
    for text in (
        "New chat · unsaved", "Saving…", "Save failed", "Updated ${time}",
    ):
        assert text in source
    # The compact action menu lives in the shared conversation header. It uses only the existing
    # lifecycle routes and warns before replacing a failed-save chat.
    header = (STATIC_DIR / "ui" / "header.js").read_text(encoding="utf-8")
    for text in ("/api/sessions/new", "/rename", "/pin", "/archive", "hdr-menu", "window.confirm"):
        assert text in header
    assert "api.post(\"/api/turn" not in source and "api.post(\"/api/turn" not in header
    app = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    assert 'msg.kind === "session_persistence"' in app


def test_workspace_chats_marks_active_chat_and_starts_in_its_project() -> None:
    source = (STATIC_DIR / "screens" / "workspace" / "chats.js").read_text(encoding="utf-8")
    assert "active-chat" in source
    assert "New chat in this project" in source
    assert "/api/sessions?project_id=" in source
    assert "/api/projects/select" in source  # existing project switch before a new scoped chat
