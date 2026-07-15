"""Static contract for the attended task-creation surface.

Tasks can start unattended work, so the browser opens an editable, human-reviewed draft and
binds the eventual submit to the workspace context that was reviewed. The server owns scope.
"""

from __future__ import annotations

from kira.ui.server import STATIC_DIR

DRAFT = (STATIC_DIR / "ui" / "task-draft.js").read_text(encoding="utf-8")
TASKS = (STATIC_DIR / "screens" / "tasks.js").read_text(encoding="utf-8")
WORKSPACE_TASKS = (STATIC_DIR / "screens" / "workspace" / "tasks.js").read_text(
    encoding="utf-8"
)


def test_task_draft_requires_explicit_review_and_submission() -> None:
    assert "Review and schedule task" in DRAFT
    assert 'api.post("/api/tasks/create", {' in DRAFT
    assert "expected_context: expectedContext" in DRAFT
    assert "opening this draft never runs work" in DRAFT
    # The request carries only a freshness assertion; it cannot select the project the server
    # schedules into.
    assert "project_id: expectedContext" not in DRAFT
    assert "innerHTML" not in DRAFT
    assert "Expected final-answer phrases" in DRAFT
    assert "verify_contains: verifyContains.length ? verifyContains : null" in DRAFT
    assert "does not prove an external action occurred" in DRAFT
    assert "Final output:" in DRAFT


def test_task_draft_recovers_from_a_failed_or_stale_submit() -> None:
    # A delayed/rejected request must neither allow its dialog to close nor strand Schedule in
    # the disabled state. Backdrop, Escape, and Cancel share the owner-aware close gate.
    assert "(owner && current !== owner) || current.saving" in DRAFT
    assert "if (activeDialog.saving) { resolve(false); return; }" in DRAFT
    assert "sameContext(api.state?.context, binding.context)" in DRAFT
    assert "owner.saving = true" in DRAFT
    assert "submit.textContent = \"Scheduling…\"" in DRAFT
    assert "submit.textContent = \"Schedule task\"" in DRAFT
    assert "Task could not be scheduled. Check your connection and try again." in DRAFT


def test_manual_task_mode_is_explicit_and_does_not_fabricate_run_provenance() -> None:
    assert 'export function openManualTaskDraft(api)' in DRAFT
    assert 'openTaskDraft({}, api, { mode: "manual" })' in DRAFT
    assert 'manual ? "" : sourcePayload(reviewedSource)' in DRAFT
    assert 'manual ? "" : (String(reviewedSource.title' in DRAFT
    assert 'mode = "follow_up"' in DRAFT
    assert 'source.mode' not in DRAFT
    assert "unsupported task draft mode" in DRAFT
    assert "Create a task" in DRAFT
    assert "Opening this draft never runs work" in DRAFT


def test_manual_task_entry_points_are_live_on_global_and_workspace_tasks() -> None:
    for screen in (TASKS, WORKSPACE_TASKS):
        assert "openManualTaskDraft" in screen
        assert "New task" in screen
        assert "api.renderIsCurrent()" in screen
    # The global and project panels invoke the reviewed module; neither can choose project scope
    # or bypass the one server-owned creation route.
    assert 'api.post("/api/tasks/create"' not in TASKS
    assert 'api.post("/api/tasks/create"' not in WORKSPACE_TASKS


def test_task_review_is_bound_to_navigation_and_workspace_authority() -> None:
    for token in (
        "reviewBinding",
        "reviewIsCurrent",
        "api.authorityToken()",
        "api.navigationToken()",
        "api.authorityIsCurrent(binding.authority)",
        "api.navigationIsCurrent(binding.navigation)",
    ):
        assert token in DRAFT
