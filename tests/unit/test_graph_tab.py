"""Graph tab + canvas structural pins (Phase 15 Task 7). The Memory Graph is an opt-in workspace tab
rendered by a self-contained Canvas engine: read/navigate-only (no mutation), no HTML-injection
sink, no external assets, and a DETERMINISTIC (hash-seeded, no Math.random) layout so screenshots
are stable. Structural (keyless) reads of the shipped JS."""

from __future__ import annotations

from jarvis.ui.server import STATIC_DIR

WS_JS = (STATIC_DIR / "screens" / "workspace.js").read_text(encoding="utf-8")
GRAPH_JS = (STATIC_DIR / "screens" / "workspace" / "graph.js").read_text(encoding="utf-8")
VIEW_JS = (STATIC_DIR / "ui" / "graphview.js").read_text(encoding="utf-8")


def test_graph_tab_in_allowlist() -> None:
    assert '["graph", "Graph"]' in WS_JS


def test_graph_panel_exists_and_exports_render() -> None:
    assert (STATIC_DIR / "screens" / "workspace" / "graph.js").is_file()
    assert "export async function render" in GRAPH_JS


def test_graph_builds_without_innerhtml() -> None:
    for js in (GRAPH_JS, VIEW_JS):
        assert "innerHTML" not in js and "insertAdjacentHTML" not in js


def test_graph_canvas_is_read_only() -> None:
    # No mutation from the graph surface — only GET reads; the canvas never posts.
    assert "api.post" not in GRAPH_JS and "api.post" not in VIEW_JS
    assert "/api/turn" not in GRAPH_JS


def test_saved_view_persists_to_localstorage_only() -> None:
    # The saved view (last focus + kind filters) is remembered in localStorage ONLY — never via a
    # server route — so the tab stays strictly read-only (Phase 15 Task 8).
    assert "localStorage" in GRAPH_JS and "kairo:graph:v4:" in GRAPH_JS
    assert "sessionStorage" in GRAPH_JS and "kairo:graph:focus:" in GRAPH_JS
    assert "saveState(" in GRAPH_JS  # called on filter / focus / reset


def test_folder_import_graph_opens_deep_hierarchy_and_avoids_reset_id_collisions() -> None:
    assert "depth: 6" in GRAPH_JS and "for (const depth of [2, 4, 6])" in GRAPH_JS
    assert "project creation timestamp" in GRAPH_JS
    assert "Project folders and files are shown through six levels" in GRAPH_JS


def test_code_map_is_a_read_only_view_with_truthful_local_import_copy() -> None:
    assert '"dependencies"' in GRAPH_JS and "Code map" in GRAPH_JS
    assert "External packages and dynamic imports stay out of the map" in GRAPH_JS
    assert "communities" in GRAPH_JS
    assert "function nodeLabel" in VIEW_JS and "split(/[\\\\/]/)" in VIEW_JS
    assert "pointermove" in VIEW_JS and "NODE_CAP = 300" in VIEW_JS


def test_dense_code_map_uses_a_small_decorative_constellation_palette_and_quiet_threads() -> None:
    # File grouping remains truthful metadata in the read model; the small visual palette is
    # explicitly decorative and cannot be mistaken for a source-kind claim.
    assert '"--accent-2", "--accent-3", "--good", "--attention"' in VIEW_JS
    assert "not semantic categories" in VIEW_JS
    assert 'e.edge_kind === "imports"' in VIEW_JS


def test_neural_canvas_keeps_interaction_local_and_accessible() -> None:
    # Zoom, pan, pulses, and relationship focus are presentational only: the graph still contains
    # no mutation route. Reduced-motion deliberately settles without the decorative pulse loop.
    for token in (
        "wheel", "pointerdown", "pointerup", "zoomAt", "resetCamera", "signalTick",
        "threadControl", "pointOnThread", "quadraticCurveTo",
    ):
        assert token in VIEW_JS
    assert "aria-label" in VIEW_JS and 'role: "status"' in VIEW_JS
    assert "reducedMotion()" in VIEW_JS
    assert "Titles are interaction-only" in VIEW_JS
    assert "const labelVisible = n === hovered || isSelected" in VIEW_JS


def test_graph_has_no_external_assets() -> None:
    for js in (GRAPH_JS, VIEW_JS):
        for banned in ("http://", "https://", "//cdn", "@import", "url(http"):
            assert banned not in js, banned


def test_layout_is_deterministic_no_random() -> None:
    # a hash-seeded layout with NO Math.random ⇒ the same graph draws the same way (stable DoD).
    assert "Math.random(" not in VIEW_JS  # the CALL — prose in the header comment is fine
    assert "hash01" in VIEW_JS


def test_canvas_and_reduced_motion() -> None:
    assert "getContext" in VIEW_JS  # Canvas 2D, not a DOM/SVG spiderweb
    assert "reduce-motion" in VIEW_JS or "prefers-reduced-motion" in VIEW_JS
    assert "NODE_CAP" in VIEW_JS  # a calm ceiling, never a whole-corpus hairball
