"""Slice 6: the chat renderer has a deliberately small, DOM-only Markdown subset."""

from kira.ui.server import STATIC_DIR

CONVERSATION = (STATIC_DIR / "screens" / "conversation.js").read_text(encoding="utf-8")
CSS = (STATIC_DIR / "kira.css").read_text(encoding="utf-8")


def test_renderer_creates_an_explicit_safe_markdown_subset_without_html_sink() -> None:
    for token in (
        "renderMarkdown", "appendInline", "codeBlock", "heading(",
        "message-heading", "message-strong", "message-emphasis",
        "message-list", "message-quote",
    ):
        assert token in CONVERSATION
    assert "innerHTML" not in CONVERSATION
    assert "document.createElement" in CONVERSATION
    assert "textContent" in CONVERSATION


def test_common_chat_markdown_does_not_leak_raw_hashes_or_stars() -> None:
    assert "const HEADING =" in CONVERSATION
    assert 'document.createElement(level === 1 ? "h2"' in CONVERSATION
    assert 'document.createElement("strong")' in CONVERSATION
    assert 'document.createElement("em")' in CONVERSATION
    assert ".message-heading" in CSS
    assert ".message-strong" in CSS
    assert ".message-emphasis" in CSS
    # Intraword underscores belong to identifiers, not markdown emphasis.
    assert "(?<![\\w_])" in CONVERSATION and "(?![\\w_])" in CONVERSATION


def test_links_are_allowlisted_to_http_and_https_and_images_are_not_markdown() -> None:
    assert 'return ["https:", "http:"].includes(url.protocol)' in CONVERSATION
    assert "(?<!\\!)" in CONVERSATION
    assert 'link.rel = "noopener noreferrer"' in CONVERSATION
    assert 'document.createElement("img")' not in CONVERSATION


def test_code_blocks_have_copy_only_actions_and_mobile_safe_styles() -> None:
    for token in ("message-code-copy", "Copy code", "copyText(text)", "overflow-x: auto"):
        assert token in CONVERSATION or token in CSS
    assert ".message-code-block" in CSS
    assert ".message-code-copy" in CSS
