"""Trust and accessibility contracts for the attended approval dialog."""

from __future__ import annotations

import re

from jarvis.ui.server import STATIC_DIR

INDEX = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
APP_JS = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
KEYS_JS = (STATIC_DIR / "ui" / "keys.js").read_text(encoding="utf-8")
CSS = (STATIC_DIR / "kairo.css").read_text(encoding="utf-8")


def _tag(element_id: str) -> str:
    match = re.search(rf"<[^>]+\bid=[\"']{re.escape(element_id)}[\"'][^>]*>", INDEX)
    assert match is not None, element_id
    return match.group(0)


def _function(name: str, following: str) -> str:
    return APP_JS.split(f"function {name}", 1)[1].split(f"function {following}", 1)[0]


def test_every_decision_waits_for_a_live_nonce() -> None:
    for element_id in ("ap-approve", "ap-always", "ap-deny"):
        assert "disabled" in _tag(element_id), element_id

    resolve = _function("resolveApproval", "hideApproval")
    assert "action !==" not in resolve
    assert "if (!p.nonce" in resolve
    assert resolve.index("if (!p.nonce") < resolve.index("api.post(")


def test_pending_projection_is_removed_only_after_backend_confirmation() -> None:
    resolve = _function("resolveApproval", "hideApproval")
    post = resolve.index("api.post(")
    confirmation = resolve.index("result.ok", post)
    removal = resolve.index("state.pending.delete", confirmation)

    assert post < confirmation < removal
    assert "catch" in resolve
    assert "p.nonce = null" in resolve
    assert resolve.count("recoverApproval(p") == 2
    assert 'api.get("/api/approvals")' in APP_JS
    assert 'wsSend({ type: "approval_shown"' in APP_JS


def test_approval_dialog_has_modal_semantics_and_managed_focus() -> None:
    dialog = _tag("approval-dialog")
    assert 'role="dialog"' in dialog
    assert 'aria-modal="true"' in dialog
    assert 'aria-labelledby="ap-kind ap-tool"' in dialog
    assert 'aria-describedby="ap-request ap-waiting"' in dialog
    assert 'tabindex="-1"' in dialog

    status = _tag("ap-status")
    assert 'role="status"' in status
    assert 'aria-live="polite"' in status
    assert 'aria-atomic="true"' in status
    assert "hidden" in _tag("ap-retry")

    show = _function("showTopApproval", "onNonce")
    hide = _function("hideApproval", "clearPendingApprovals")
    assert "pushEscape(hideApproval, dialog)" in show
    assert "dialog.focus()" in show
    assert "setApprovalBackgroundInert(true)" in show
    assert "_approvalRestoreFocus" in hide and ".focus()" in hide
    assert "setApprovalBackgroundInert(false)" in hide
    assert "trapRoot" in KEYS_JS and 'ev.key === "Tab"' in KEYS_JS


def test_hidden_or_retried_failures_remain_recoverable() -> None:
    recover = _function("recoverApproval", "showTopApproval")
    assert "_approvalRecovering.has" in recover
    assert "try" in recover and "finally" in recover
    assert "p._shown = false" in recover
    assert "showTopApproval()" in recover


def test_large_approval_details_scroll_without_hiding_actions() -> None:
    assert "max-height: calc(100dvh - 40px)" in CSS
    assert ".approve-card .bd { min-height: 0; overflow-y: auto;" in CSS
    assert ".approve-card .actions { flex: none;" in CSS
    assert "Secure confirmation is taking longer than expected." in APP_JS
    assert 'id="ap-retry"' in INDEX
