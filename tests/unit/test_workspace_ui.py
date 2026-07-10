"""Project Workspace read models + shell pins (Phase 11 T10).

The Workspace shell (#workspace/{id}/{tab}) fetches one shared project context and lazy-loads the
active tab panel with a per-tab error boundary; the tab name is validated against a fixed allowlist
before the dynamic import (the hash is attacker-influenceable). The Activity feed is a derived,
metadata-only, project-scoped read model. Panels read project-scoped data and navigate; writes go
through the existing enumerated routes only.
"""

from __future__ import annotations

from jarvis.ui.readmodels import UiServices, activity_feed
from jarvis.ui.server import STATIC_DIR

APP_JS = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
WS_JS = (STATIC_DIR / "screens" / "workspace.js").read_text(encoding="utf-8")


async def test_activity_feed_degrades_without_services() -> None:
    out = await activity_feed(UiServices(), 7)
    assert out == {"events": [], "project_id": 7}


def test_workspace_registered_in_router() -> None:
    assert 'from "./screens/workspace.js"' in APP_JS
    assert "workspace: renderWorkspace" in APP_JS


def test_shell_validates_tab_against_allowlist_before_dynamic_import() -> None:
    # The tab comes from the (attacker-influenceable) hash; it must be checked against the fixed
    # TAB_KEYS allowlist before the dynamic import, so no crafted tab can load an off-path module.
    assert "TAB_KEYS" in WS_JS and "TAB_KEYS.includes" in WS_JS
    assert "await import(`./workspace/${tab}.js`)" in WS_JS
    for tab in ("overview", "chats", "artifacts", "memory", "tasks", "vault", "studio", "office",
                "graph", "costs", "activity"):
        assert f'"{tab}"' in WS_JS, tab


def test_shell_has_a_per_tab_error_boundary() -> None:
    # A panel that fails to load must not take down the whole workspace.
    assert "try {" in WS_JS and "catch" in WS_JS
    assert "Panel unavailable" in WS_JS


def test_shell_routes_by_hash_and_reads_context() -> None:
    assert "workspace/${projectId}/${key}" in WS_JS  # tab clicks deep-link by hash
    assert "/api/workspace/${projectId}" in WS_JS      # shared project context fetch


def test_shell_renders_without_innerhtml() -> None:
    assert "innerHTML" not in WS_JS


# --- the 9 tab panels ------------------------------------------------------

PANELS_DIR = STATIC_DIR / "screens" / "workspace"
PANELS = [
    "overview", "chats", "artifacts", "memory", "tasks", "vault", "studio", "office", "graph",
    "costs", "activity",
]
# The ONLY mutations each panel may make (existing routes). Read-only panels get an empty list.
PANEL_ROUTES = {
    "chats": ["/api/sessions/"],
    "artifacts": ["/api/artifacts/"],
    "memory": ["/api/memory/", "/api/graph/suggestions/"],  # Phase 15: the review queue
    "tasks": ["/api/tasks/"],
    "vault": ["/api/vault/sources/"],
    "overview": [],
    "studio": [],
    "office": [],  # render-only: Studio owns orchestration mutations
    "graph": [],  # Phase 15: read/navigate-only canvas (GET subgraph + node card; no mutations)
    "costs": [],
    "activity": [],
}


def _panel(name: str) -> str:
    return (PANELS_DIR / f"{name}.js").read_text(encoding="utf-8")


def test_all_workspace_panels_exist_and_export_render() -> None:
    for p in PANELS:
        assert (PANELS_DIR / f"{p}.js").is_file(), p
        assert "export async function render" in _panel(p), p


def test_panels_render_without_innerhtml() -> None:
    for p in PANELS:
        assert "innerHTML" not in _panel(p), p


def test_panels_write_only_their_enumerated_routes() -> None:
    for p in PANELS:
        txt = _panel(p)
        assert "/api/turn" not in txt, p  # never the agent turn path
        if "api.post(" in txt:
            assert PANEL_ROUTES[p], f"{p} must be read-only but calls api.post"
            assert any(r in txt for r in PANEL_ROUTES[p]), p


def test_panels_open_only_the_hardened_content_route() -> None:
    # A panel that opens an artifact uses the same-origin, hardened /content GET with noopener —
    # never an external_uri or any other URL.
    for p in PANELS:
        txt = _panel(p)
        if "window.open(" in txt:
            assert "/api/artifacts/" in txt and "/content" in txt and "noopener" in txt, p
            assert "window.open(a.external_uri" not in txt, p


def test_panels_read_project_scoped_endpoints() -> None:
    # Each data-backed panel scopes to the project (via project_id or the workspace path).
    for p in ("chats", "artifacts", "memory", "tasks", "vault", "studio", "costs"):
        assert "project_id" in _panel(p), p
    assert "/activity" in _panel("activity")
    assert "/office" in _panel("office")
    assert "/graph" in _panel("graph")
    assert "/api/workspace/" in _panel("overview")
