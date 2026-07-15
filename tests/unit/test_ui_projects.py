"""Projects grid frontend pins (Phase 11 T9).

The Projects screen is a card grid over /api/projects/overview. Its only writes are the
enumerated project metadata mutations (create/select/archive/pin/label); everything else reads
or navigates. Built entirely with el()/textContent so a project name/label can't inject markup.
"""

from __future__ import annotations

from kira.ui.server import STATIC_DIR

PROJECTS_JS = (STATIC_DIR / "screens" / "projects.js").read_text(encoding="utf-8")


def test_grid_over_the_overview_read_model() -> None:
    assert "/api/projects/overview" in PROJECTS_JS
    assert "projects-grid" in PROJECTS_JS


def test_writes_are_the_enumerated_project_mutations_only() -> None:
    # create / select / update / archive / pin / label / services — attended project settings.
    for ep in (
        "/api/projects", "/api/projects/select", "/update", "/pin", "/label", "/archive",
        "/reset",
        "/services",
    ):
        assert ep in PROJECTS_JS, ep
    assert "/api/turn" not in PROJECTS_JS
    assert "/api/orchestration" not in PROJECTS_JS


def test_renders_without_innerhtml() -> None:
    # Untrusted project names/labels reach the DOM only via el()/textContent.
    assert "innerHTML" not in PROJECTS_JS


def test_pin_and_label_present() -> None:
    assert "pin-star" in PROJECTS_JS and "label-select" in PROJECTS_JS


def test_attended_project_details_edit_uses_existing_metadata_route() -> None:
    # Metadata remains a human-reviewed form; it cannot select scope or invoke a run.
    assert "openProjectEdit" in PROJECTS_JS
    assert "Edit details" in PROJECTS_JS
    assert "/api/projects/${encodeURIComponent(project.id)}/update" in PROJECTS_JS
    assert "Save details" in PROJECTS_JS
    assert "project scope" in PROJECTS_JS
    assert "projectEditSequence" in PROJECTS_JS
    assert "activeProjectEdit?.owner !== owner" in PROJECTS_JS
    assert "current.saving" in PROJECTS_JS
    assert "try {" in PROJECTS_JS and "catch {" in PROJECTS_JS


def test_health_chips_present() -> None:
    for token in ("open_tasks", "sessions_week", "last_run", "month_spend_usd"):
        assert token in PROJECTS_JS, token


def test_project_reset_requires_attended_confirmation_and_password_step_up() -> None:
    for token in (
        "Start fresh",
        "confirmation.value !== project.name",
        "api.stepUp(password.value)",
        "retain_repositories: retain.checked",
        "/reset",
    ):
        assert token in PROJECTS_JS


def test_collections_row_navigates_only() -> None:
    # Built-in collection presets navigate (hash) — never mutate or depend on a hidden API.
    assert "collection-chip" in PROJECTS_JS
    assert "/api/views" not in PROJECTS_JS
    assert "location.hash" in PROJECTS_JS


def test_opening_an_inactive_workspace_switches_to_its_scoped_context_first() -> None:
    # The Graph/Vault read models correctly require the server-owned active workspace. A project
    # card therefore selects through the pre-existing lifecycle route before navigating instead
    # of rendering an inactive project's scoped panels as misleadingly empty.
    assert "const openWorkspace" in PROJECTS_JS
    assert 'api.post("/api/projects/select", { project_id: p.id })' in PROJECTS_JS
    assert "Open & switch" in PROJECTS_JS
    assert "workspace/" in PROJECTS_JS


def test_service_access_is_attended_scoped_and_names_only() -> None:
    for token in (
        "Service access",
        'Object.hasOwn(project.settings || {}, "services")',
        "services_enabled",
        "api.state?.context?.project_id",
        "current.saving",
        "sameServiceSelection",
        "expected_services",
        "project_busy",
        "service_access_changed",
        "pushEscape(closeIfIdle, card)",
    ):
        assert token in PROJECTS_JS
    assert "credentials_present" not in PROJECTS_JS
    assert "credential_env" not in PROJECTS_JS
