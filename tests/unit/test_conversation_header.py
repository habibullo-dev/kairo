"""Conversation header + boot rehydration + composer truth (Phase 15.5).

The header shows real server state (scope / title / model / mode / capabilities) and its controls
POST only to the four enumerated UI-state route families — never /api/turn, never an approval. The
"No messages yet" reload bug is fixed by rehydrating the active transcript on boot (server truth,
not a client cache). All header text is textContent (chat titles are user/model text). Structural
reads of the shipped JS."""

from __future__ import annotations

from jarvis.ui.server import STATIC_DIR

HDR = (STATIC_DIR / "ui" / "header.js").read_text(encoding="utf-8")
APP = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
DAILY = (STATIC_DIR / "screens" / "daily.js").read_text(encoding="utf-8")
CHAT = (STATIC_DIR / "screens" / "chat.js").read_text(encoding="utf-8")


def test_header_exports_mount_and_refresh() -> None:
    assert "export async function mountHeader" in HDR
    assert "export async function refreshHeader" in HDR


def test_header_renders_from_server_state_not_fake_chips() -> None:
    # Values come from the read models, never hardcoded — and the old fake composer chip is gone.
    assert "/api/runner" in HDR and "/api/models" in HDR and "/api/capabilities" in HDR
    assert "opus-4-8" not in DAILY  # the hardcoded fake model chip was removed
    assert "opus-4-8" not in HDR  # the header never hardcodes a model id
    assert "hdr-model-menu" in HDR  # model/effort/mode live in one quiet composer popover
    assert 'id="chat-convo-header"' in CHAT  # the header is mounted once, persistently
    assert "daily-convo-header" not in DAILY


def test_header_writes_only_allowlisted_ui_state_routes() -> None:
    # The composer header mutates ONLY its small UI-state family. History mutations live in the
    # chat-side drawer, never the agent turn or an approval surface.
    assert "/api/turn" not in HDR
    assert "/api/approvals" not in HDR
    for route in ("/api/projects/select", "/api/model", "/api/mode"):
        assert route in HDR, route
    assert "kairo:chat-history" not in HDR
    assert "☰" not in HDR
    for route in ("/rename", "/archive", "/pin"):
        assert route in CHAT, route


def test_header_is_textcontent_only() -> None:
    assert "innerHTML" not in HDR  # chat titles / project names are untrusted user/model text


def test_boot_rehydrates_the_active_conversation() -> None:
    assert "rehydrateConversation" in APP
    assert "session_id" in APP and "/api/sessions/" in APP  # loads the transcript we are in


def test_app_handles_model_mode_project_ws_echoes() -> None:
    for kind in ("model_changed", "mode_changed", "project_changed"):
        assert kind in APP
