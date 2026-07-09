"""Graph & Workspace discovery (Phase 15.5 Task 8).

The Workspace (incl. the Graph) is reachable from Daily's active-workspace card; the graph's empty
state teaches what it is + how to populate it; and memory rows deep-link into the focused graph
tab. All of it is GET/navigate-only — the deep-link sets a localStorage focus + a hash, never a
server write. Structural reads of the shipped JS."""

from __future__ import annotations

from jarvis.ui.server import STATIC_DIR

DAILY = (STATIC_DIR / "screens" / "daily.js").read_text(encoding="utf-8")
GRAPH = (STATIC_DIR / "screens" / "workspace" / "graph.js").read_text(encoding="utf-8")
MEMORY = (STATIC_DIR / "screens" / "workspace" / "memory.js").read_text(encoding="utf-8")


def test_daily_has_an_active_workspace_card() -> None:
    assert "daily-workspace" in DAILY and "fillWorkspace" in DAILY
    assert "Active workspace" in DAILY
    assert "#workspace/${proj.id}" in DAILY  # deep-links to the active project's workspace tabs
    assert "Choose a project" in DAILY  # the global-scope empty state guides to Projects


def test_graph_empty_state_teaches_and_offers_rebuild() -> None:
    assert "Nothing to graph yet" in GRAPH
    assert "jarvis graph rebuild" in GRAPH and "clipboard" in GRAPH  # copyable ritual
    assert "read-only" in GRAPH  # explains the graph changes nothing


def test_memory_rows_deep_link_into_the_focused_graph() -> None:
    assert "graphLink" in MEMORY
    assert "kairo:graph:" in MEMORY and "#workspace/${projectId}/graph" in MEMORY
    # the deep-link is navigate-only — it adds NO new mutation to the Memory panel's routes.
    assert "memory:${m.id}" in MEMORY  # focuses this specific memory node
