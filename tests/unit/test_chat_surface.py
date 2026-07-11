"""Slice 1: the primary chat route remains a thin UI over the existing attended turn path."""

from jarvis.ui.server import STATIC_DIR

APP = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
HTML = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
DAILY = (STATIC_DIR / "screens" / "daily.js").read_text(encoding="utf-8")
CHAT = (STATIC_DIR / "screens" / "chat.js").read_text(encoding="utf-8")
CONVERSATION = (STATIC_DIR / "screens" / "conversation.js").read_text(encoding="utf-8")
CSS = (STATIC_DIR / "kairo.css").read_text(encoding="utf-8")


def test_chat_is_the_default_primary_route_with_a_clear_nav_entry() -> None:
    assert 'href="#chat" data-screen="chat"' in HTML
    assert 'href="#trace" data-screen="trace"' in HTML and "debug-only" in HTML
    assert 'href="#lab" data-screen="lab"' in HTML and "debug-only" in HTML
    assert 'route: "chat"' in APP
    assert 'name: parts[0] || "chat"' in APP
    assert "chat: renderChat" in APP
    assert ".rail .debug-only { display: none; }" in CSS


def test_chat_reuses_existing_turn_and_conversation_state() -> None:
    assert 'api.post("/api/turn"' in CONVERSATION
    assert "onConversationEvent(state, evt)" in APP
    assert 'from "./conversation.js"' in CHAT
    assert 'from "./conversation.js"' not in DAILY
    assert "/api/approvals" not in CHAT
    assert "/api/model" not in CHAT


def test_chat_has_readable_full_height_composer_and_context_controls() -> None:
    for token in (
        'id="chat-input"',
        'class="chat-send"',
        'id="chat-convo-header"',
        "chat-composer-toolbar",
        'id="chat-pending"',
        'id="chat-attach"',
        'id="chat-file-input"',
        "chat-attachments",
        'id="chat-mic"',
        'id="chat-voice-cancel"',
    ):
        assert token in CHAT
    assert "chat-intro" not in CHAT
    for token in (".screen.chat-screen", ".chat-shell", ".chat-thread", ".chat-composer"):
        assert token in CSS
    assert ".chat-composer .convo-header" in CSS
    # app.js resets route classes for every render; Chat restores its own class before the
    # construction guard so a streamed response cannot collapse the primary layout.
    layout_class = 'container.classList.add("chat-screen")'
    construction_guard = 'if (!container.querySelector("#chat-input"))'
    assert CHAT.index(layout_class) < CHAT.index(construction_guard)
    assert 'event.key === "Enter" && !event.shiftKey' in CHAT
    assert "Send <span" not in CHAT


def test_chat_message_rendering_remains_text_only() -> None:
    assert "document.createTextNode" in CONVERSATION
    assert "code.textContent" in CONVERSATION
    assert "innerHTML" not in CONVERSATION


def test_idle_global_chrome_collapses_without_removing_active_controls() -> None:
    active_when_needed = (
        'status.classList.toggle("status-active", !!busy || !!paused || attentionElsewhere)'
    )
    assert active_when_needed in APP
    assert 'status.classList.toggle("is-working", !!busy)' in APP
    assert ".status { grid-area: status; display: none; }" in CSS
    assert ".status.status-active" in CSS
    assert ".status.is-paused #st-resume" in CSS


def test_chat_voice_is_review_first_and_uses_the_existing_safe_controller() -> None:
    assert "api.toggleVoiceCapture" in CHAT
    assert "api.cancelVoiceCapture" in CHAT
    assert 'mode || "dictation"' in CHAT
    assert 'status.textContent = disabled ? "Voice unavailable"' in CHAT
    assert "/api/turn" not in CHAT


def test_composer_omits_unsupported_effort_and_uses_a_quiet_context_shelf() -> None:
    header = (STATIC_DIR / "ui" / "header.js").read_text(encoding="utf-8")
    assert "if (!supported) return null;" in header
    assert "n/a for this model" not in header
    assert "Auto-managed" not in header
    assert "hdr-model-menu" in header
    assert "☰" not in header  # no duplicate/hamburger control in the composer
    for token in ("chat-history-panel", "chat-history-layer", "chat-context-handle", "Library"):
        assert token in CHAT or token in CSS or token in header


def test_chat_uses_plain_project_language_and_a_time_aware_welcome() -> None:
    header = (STATIC_DIR / "ui" / "header.js").read_text(encoding="utf-8")
    for token in ("No project", "Untitled"):
        assert token in header
    for token in ("Good morning.", "Good afternoon.", "Good evening.", "hints"):
        assert token in CHAT


def test_chat_shelf_has_scoped_files_and_honest_project_outputs() -> None:
    for token in (
        "/api/chat/files", "/api/chat/outputs", "/api/chat/knowledge",
        "Files", "Outputs", "Knowledge", "Project outputs", "Open full graph", "Attached folders",
    ):
        assert token in CHAT
    assert "source_session_id" not in CHAT  # the browser never chooses the scope
    assert "api.download" in CHAT


def test_chat_knowledge_shelf_is_project_bound_and_metadata_only() -> None:
    for token in (
            "Choose a project", "Project files", "Knowledge connections",
        "uv run jarvis graph rebuild", "window.location.hash",
    ):
        assert token in CHAT
    assert "local_path" not in CHAT
    assert "markdown_path" not in CHAT


def test_chat_file_button_uses_the_scoped_local_upload_path() -> None:
    for token in (
        "chat-attach", "chat-file-input", "chat-folder-input", "webkitdirectory",
        "relative_path", "finalize", "projectImport", "FormData", "api.upload",
        "PROJECT_IMPORT_CONCURRENCY", "PROJECT_IMPORT_MAX_FILES", "data/connectors",
    ):
        assert token in CHAT
    assert '"/api/chat/attachments"' in CHAT
    assert "chatAttachments" in APP


def test_chat_knowledge_uses_the_safe_shared_source_tree() -> None:
    assert '"../ui/source-tree.js"' in CHAT
    assert "renderSourceTree(sources)" in CHAT
    assert '"/api/chat/knowledge/detach"' in CHAT
    assert "Remove this folder from the project?" in CHAT
    assert "cleared_chunks" in CHAT
