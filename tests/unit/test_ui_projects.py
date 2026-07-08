"""Projects grid frontend pins (Phase 11 T9).

The Projects screen is a card grid over /api/projects/overview. Its only writes are the
enumerated project metadata mutations (create/select/archive/pin/label); everything else reads
or navigates. Built entirely with el()/textContent so a project name/label can't inject markup.
"""

from __future__ import annotations

from jarvis.ui.server import STATIC_DIR

PROJECTS_JS = (STATIC_DIR / "screens" / "projects.js").read_text(encoding="utf-8")


def test_grid_over_the_overview_read_model() -> None:
    assert "/api/projects/overview" in PROJECTS_JS
    assert "projects-grid" in PROJECTS_JS


def test_writes_are_the_enumerated_project_mutations_only() -> None:
    # create / select / archive / pin / label — the metadata set. No turn/executor/orchestration.
    for ep in ("/api/projects", "/api/projects/select", "/pin", "/label", "/archive"):
        assert ep in PROJECTS_JS, ep
    assert "/api/turn" not in PROJECTS_JS
    assert "/api/orchestration" not in PROJECTS_JS


def test_renders_without_innerhtml() -> None:
    # Untrusted project names/labels reach the DOM only via el()/textContent.
    assert "innerHTML" not in PROJECTS_JS


def test_pin_and_label_present() -> None:
    assert "pin-star" in PROJECTS_JS and "label-select" in PROJECTS_JS


def test_health_chips_present() -> None:
    for token in ("open_tasks", "sessions_week", "last_run", "month_spend_usd"):
        assert token in PROJECTS_JS, token


def test_collections_row_navigates_only() -> None:
    # Built-in smart collections + user saved views navigate (hash) — never mutate.
    assert "collection-chip" in PROJECTS_JS
    assert "/api/views" in PROJECTS_JS  # user saved views (a GET)
    assert "location.hash" in PROJECTS_JS


def test_card_opens_workspace_by_hash() -> None:
    assert "workspace/" in PROJECTS_JS
