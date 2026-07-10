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
    assert 'href="#trace" data-screen="trace" class="debug-only"' in HTML
    assert 'href="#lab" data-screen="lab" class="debug-only"' in HTML
    assert 'route: "chat"' in APP
    assert 'name: parts[0] || "chat"' in APP
    assert "chat: renderChat" in APP


def test_chat_reuses_existing_turn_and_conversation_state() -> None:
    assert 'api.post("/api/turn"' in CONVERSATION
    assert "onConversationEvent(state, evt)" in APP
    assert 'from "./conversation.js"' in CHAT
    assert 'from "./conversation.js"' in DAILY
    assert "/api/approvals" not in CHAT
    assert "/api/model" not in CHAT


def test_chat_has_readable_full_height_composer_and_context_controls() -> None:
    for token in (
        'id="chat-input"',
        'class="chat-send"',
        'id="chat-convo-header"',
        'id="chat-model"',
        'id="chat-mode"',
        'id="chat-pending"',
    ):
        assert token in CHAT
    for token in (".screen.chat-screen", ".chat-shell", ".chat-thread", ".chat-composer"):
        assert token in CSS
    # app.js resets route classes for every render; Chat restores its own class before the
    # construction guard so a streamed response cannot collapse the primary layout.
    layout_class = 'container.classList.add("chat-screen")'
    construction_guard = 'if (!container.querySelector("#chat-input"))'
    assert CHAT.index(layout_class) < CHAT.index(construction_guard)
    assert 'event.key === "Enter" && !event.shiftKey' in CHAT


def test_chat_message_rendering_remains_text_only() -> None:
    assert "textContent = part" in CONVERSATION
    assert "code.textContent" in CONVERSATION
    assert "innerHTML" not in CONVERSATION
