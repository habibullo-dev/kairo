"""Live events refresh only the visible read-only surface that can have changed."""

from kira.ui.server import STATIC_DIR

APP = (STATIC_DIR / "app.js").read_text(encoding="utf-8")


def test_background_notices_refresh_their_visible_evidence_surfaces() -> None:
    notice_handler = APP.split("function onNotice(notice)", 1)[1].split("function onVoice", 1)[0]

    assert 'refreshIfActive("gate")' in notice_handler
    assert 'refreshIfActive("tasks")' in notice_handler
    assert 'refreshWorkspaceTabs("tasks")' in notice_handler
    assert 'notice.kind === "digest"' in notice_handler


def test_completed_turn_refreshes_only_visible_memory_and_vault_surfaces() -> None:
    event_handler = APP.split("function onEvent(evt)", 1)[1].split("// --- approvals", 1)[0]

    assert 'refreshIfActive("memory")' in event_handler
    assert 'refreshIfActive("vault")' in event_handler
    assert 'refreshWorkspaceTabs("memory")' in event_handler
    assert 'refreshWorkspaceTabs("vault")' in event_handler
    assert "if (!vaultHasDraft())" in event_handler
    assert "function vaultHasDraft()" in APP
    assert 'input === document.activeElement || input.value.trim() !== ""' in APP
    assert "function refreshWorkspaceTabs(...tabs)" in APP
