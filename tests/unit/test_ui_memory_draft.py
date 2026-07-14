"""Static contract for the attended memory-creation surface.

Memory is durable context, so the UI must start from an empty, human-reviewed form and submit
only to the existing human-authority endpoint.  The server remains the authority for workspace
scope; the browser must never select a project in the request body.
"""

from __future__ import annotations

from jarvis.ui.server import STATIC_DIR

DRAFT = (STATIC_DIR / "ui" / "memory-draft.js").read_text(encoding="utf-8")
GLOBAL_MEMORY = (STATIC_DIR / "screens" / "memory.js").read_text(encoding="utf-8")
WORKSPACE_MEMORY = (STATIC_DIR / "screens" / "workspace" / "memory.js").read_text(encoding="utf-8")
ROUTES = (STATIC_DIR.parents[3] / "tests" / "unit" / "test_ui_route_consumption.py").read_text(
    encoding="utf-8"
)


def test_memory_draft_requires_explicit_human_review_and_submission() -> None:
    assert "textarea" in DRAFT and "required: true" in DRAFT
    assert "const reviewedContent = content.value.trim()" in DRAFT
    assert 'api.post("/api/memory/remember", {' in DRAFT
    assert "content: reviewedContent" in DRAFT and "type: type.value" in DRAFT
    assert "Nothing is saved until you press Save memory." in DRAFT
    # The request carries only an expected-context freshness assertion; it never selects a
    # project for the server to trust.
    assert "project_id: expectedContext" not in DRAFT
    assert "innerHTML" not in DRAFT


def test_memory_draft_keeps_its_owner_open_while_a_save_is_pending() -> None:
    # A delayed request can neither close a newer draft nor strand its own controls after a
    # rejected fetch. Backdrop/Escape/Cancel all share the owner-aware close gate.
    assert "(owner && current !== owner) || current.saving" in DRAFT
    assert "if (activeDialog.saving) { resolve(false); return; }" in DRAFT
    assert "owner.saving = true" in DRAFT
    assert "cancel.disabled = true" in DRAFT
    assert "close(true, owner)" in DRAFT
    assert "Memory could not be saved. Check your connection and try again." in DRAFT


def test_memory_draft_binds_human_review_to_the_current_chat_context() -> None:
    # expected_context is a freshness assertion, not a project selector. The server remains the
    # authority and rejects a stale draft after another tab switches this live workspace.
    assert "const expectedContext = api.state.context" in DRAFT
    assert "sameContext(api.state.context, expectedContext)" in DRAFT
    assert "expected_context: expectedContext" in DRAFT
    assert "left.context_revision === right.context_revision" in DRAFT


def test_memory_draft_supports_only_server_accepted_types() -> None:
    for memory_type in ("fact", "preference", "project", "episode"):
        assert f'value: "{memory_type}"' in DRAFT


def test_global_and_workspace_memory_surfaces_open_the_same_attended_draft() -> None:
    for source in (GLOBAL_MEMORY, WORKSPACE_MEMORY):
        assert "import { openMemoryDraft }" in source
        assert "Remember something" in source
        assert "await openMemoryDraft(api)" in source


def test_memory_remember_is_no_longer_intentionally_unreachable() -> None:
    assert '"/api/memory/remember"' not in ROUTES
