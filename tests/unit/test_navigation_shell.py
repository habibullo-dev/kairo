"""Slice 5 navigation shell: clear route hierarchy, mobile labels, and Debug route gating."""

from jarvis.ui.server import STATIC_DIR

APP = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
HTML = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
CSS = (STATIC_DIR / "kira.css").read_text(encoding="utf-8")
WORKBENCH_PATH = STATIC_DIR.parents[3] / "tests" / "ui" / "workbench_dod.py"
WORKBENCH_DOD = WORKBENCH_PATH.read_text(encoding="utf-8")


def test_primary_navigation_starts_with_chat_and_daily_and_names_knowledge() -> None:
    assert HTML.index('href="#chat"') < HTML.index('href="#daily"') < HTML.index('href="#projects"')
    assert 'href="#vault" data-screen="vault"' in HTML
    assert ">Knowledge<" in HTML
    assert 'data-screen="workspace"' in HTML


def test_shell_exposes_route_location_and_gate_shortcut_without_new_authority() -> None:
    assert 'id="st-location"' in HTML
    assert 'id="st-attention" href="#gate"' in HTML
    assert "ROUTE_LABELS" in APP and "WORKSPACE_LABELS" in APP
    assert "api.post" not in HTML


def test_debug_routes_are_hidden_and_render_gated_when_debug_is_off() -> None:
    assert 'const DEBUG_ROUTES = new Set(["trace", "lab"])' in APP
    assert 'DEBUG_ROUTES.has(state.route) && !document.body.classList.contains("debug")' in APP
    assert '.debug-only, .rail .debug-only { display: none; }' in CSS


def test_mobile_navigation_is_labeled_bottom_navigation_with_composer_space() -> None:
    for token in (
        "Narrow/mobile", "position: fixed", "flex-direction: row",
        ".rail a .label { display: block",
        "padding-bottom: 66px", "min-height: calc(100dvh - 118px)",
    ):
        assert token in CSS


def test_workbench_visual_matrix_covers_the_slice_five_primary_surfaces() -> None:
    for state in (
        "chat-project", "daily-populated", "projects", "workspace-overview", "studio",
        "costs", "settings",
    ):
        assert state in WORKBENCH_DOD
