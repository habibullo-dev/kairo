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
