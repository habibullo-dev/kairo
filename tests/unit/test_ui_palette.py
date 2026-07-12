"""Command palette pins (Phase 11 T7 + Phase 15.5 T7).

The palette searches (a GET on the unified /api/graph/search) and navigates. Phase 15.5 amends the
original "GET-only" rule: it MAY now perform a small set of reversible UI-state WRITES (New Chat,
Switch Project/Model/Mode) — but ONLY those four routes, funnelled through a single act() helper —
and NEVER the agent turn, an approval, or any Gate-reaching action. Untrusted snippets render via
el()/textContent. Content pins over the hand-written assets plus server-route pins.
"""

from __future__ import annotations

from pathlib import Path

from jarvis.ui import server as server_mod
from jarvis.ui.server import STATIC_DIR

PALETTE_JS = (STATIC_DIR / "ui" / "palette.js").read_text(encoding="utf-8")
APP_JS = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
SERVER = Path(server_mod.__file__).read_text(encoding="utf-8")

# The EXACT set of routes the palette may write to (the amended allowlist).
_ALLOWED_WRITES = ("/api/sessions/new", "/api/mode", "/api/model", "/api/projects/select")


def test_palette_writes_only_the_allowlisted_ui_state_routes() -> None:
    for route in _ALLOWED_WRITES:
        assert route in PALETTE_JS, route
    # every write funnels through ONE helper (act) — _api.post appears exactly once, and act is
    # only ever handed an allowlisted path. No other mutation path exists in the palette.
    assert PALETTE_JS.count("_api.post(") == 1
    # never the agent turn, an approval, or a graph-review write.
    assert "/api/turn" not in PALETTE_JS
    assert "/api/approvals" not in PALETTE_JS
    assert "/api/graph/suggestions" not in PALETTE_JS


def test_palette_renders_untrusted_snippets_without_innerhtml() -> None:
    assert "innerHTML" not in PALETTE_JS  # rows are el()/textContent only


def test_palette_bound_to_hotkey_and_escape() -> None:
    assert "setPaletteToggle(toggle)" in PALETTE_JS
    assert "pushEscape(close)" in PALETTE_JS
    assert "initPalette(api)" in APP_JS


def test_palette_result_routing_reads_and_navigates() -> None:
    # A chat result RESUMES via the shared helper; an artifact opens its hardened read-only content;
    # an entity opens the (navigate-only) focused graph tab; the rest navigate by hash.
    assert "resumeChat(" in PALETTE_JS
    assert "location.hash" in PALETTE_JS
    assert "/api/artifacts/" in PALETTE_JS and "/content" in PALETTE_JS
    assert "window.open(" in PALETTE_JS and "noopener" in PALETTE_JS
    assert "openEntity" in PALETTE_JS and "kairo:graph:focus:" in PALETTE_JS  # focus graph once


def test_palette_searches_the_unified_graph_search() -> None:
    assert "/api/graph/search" in PALETTE_JS
    assert "_api.get(" in PALETTE_JS  # search is a GET


def test_palette_makes_recent_chats_findable() -> None:
    # Blocker 3: the word-based unified search can miss chats, so the palette surfaces recent chats
    # by TITLE (always browsable + resumable) — searching "chat"/a title finds your conversations.
    assert "computeChats" in PALETTE_JS
    assert "/api/sessions" in PALETTE_JS and "resumeChat(" in PALETTE_JS


def test_palette_uses_the_shipped_overlay_classes() -> None:
    for cls in ("command-overlay", "command-palette", "search-input", "palette-results"):
        assert cls in PALETTE_JS


def test_search_routes_stay_get_only() -> None:
    for route in ("/api/search", "/api/graph/search"):
        assert f'@app.get("{route}")' in SERVER
        assert f'@app.post("{route}")' not in SERVER
