"""Static contract for the attended task-creation surface.

Tasks can start unattended work, so the browser opens an editable, human-reviewed draft and
binds the eventual submit to the workspace context that was reviewed. The server owns scope.
"""

from __future__ import annotations

from jarvis.ui.server import STATIC_DIR

DRAFT = (STATIC_DIR / "ui" / "task-draft.js").read_text(encoding="utf-8")


def test_task_draft_requires_explicit_review_and_submission() -> None:
    assert "Review and schedule task" in DRAFT
    assert 'api.post("/api/tasks/create", {' in DRAFT
    assert "expected_context: expectedContext" in DRAFT
    assert "opening this draft never runs work" in DRAFT
    # The request carries only a freshness assertion; it cannot select the project the server
    # schedules into.
    assert "project_id: expectedContext" not in DRAFT
    assert "innerHTML" not in DRAFT


def test_task_draft_recovers_from_a_failed_or_stale_submit() -> None:
    # A delayed/rejected request must neither allow its dialog to close nor strand Schedule in
    # the disabled state. Backdrop, Escape, and Cancel share the owner-aware close gate.
    assert "(owner && current !== owner) || current.saving" in DRAFT
    assert "if (activeDialog.saving) { resolve(false); return; }" in DRAFT
    assert "sameContext(api.state.context, expectedContext)" in DRAFT
    assert "owner.saving = true" in DRAFT
    assert "submit.textContent = \"Scheduling…\"" in DRAFT
    assert "submit.textContent = \"Schedule task\"" in DRAFT
    assert "Task could not be scheduled. Check your connection and try again." in DRAFT
