"""Command palette pins (Phase 11 T7).

Ctrl/Cmd-K opens a palette that does two READ things: federated search (a GET on /api/search) and
navigation (jump to a screen, or open an artifact's read-only content). It must NEVER mutate — no
POST, no api.post — and it must render untrusted search snippets without innerHTML. These are
content pins over the hand-written assets plus one server-route pin.
"""

from __future__ import annotations

from pathlib import Path

from jarvis.ui import server as server_mod
from jarvis.ui.server import STATIC_DIR

PALETTE_JS = (STATIC_DIR / "ui" / "palette.js").read_text(encoding="utf-8")
APP_JS = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
SERVER = Path(server_mod.__file__).read_text(encoding="utf-8")


def test_palette_is_get_and_navigate_only() -> None:
    # The palette searches (GET) and navigates — it must never mutate. Check for an actual POST
    # call (".post(") rather than the string, so a doc comment can't trip it.
    assert ".post(" not in PALETTE_JS
    assert 'method: "POST"' not in PALETTE_JS
    assert "/api/search" in PALETTE_JS
    assert "_api.get(" in PALETTE_JS  # search is issued as a GET


def test_palette_renders_untrusted_snippets_without_innerhtml() -> None:
    # Search titles/snippets are untrusted content; rows are built via el()/textContent only.
    assert "innerHTML" not in PALETTE_JS


def test_palette_bound_to_hotkey_and_escape() -> None:
    assert "setPaletteToggle(toggle)" in PALETTE_JS  # binds the Ctrl/Cmd-K dispatcher slot
    assert "pushEscape(close)" in PALETTE_JS          # Escape closes it via the overlay stack
    assert "initPalette(api)" in APP_JS               # wired into the shell


def test_palette_navigation_targets_are_reads() -> None:
    # A result jumps to a screen (hash) or opens an artifact's read-only content in a new tab —
    # never a mutation; the content route is the hardened, registered-id-only GET from T4.
    assert "location.hash" in PALETTE_JS
    assert "/api/artifacts/" in PALETTE_JS and "/content" in PALETTE_JS
    assert "window.open(" in PALETTE_JS and "noopener" in PALETTE_JS


def test_palette_uses_the_shipped_overlay_classes() -> None:
    for cls in ("command-overlay", "command-palette", "search-input", "palette-results"):
        assert cls in PALETTE_JS


def test_search_route_stays_get_only() -> None:
    # The palette relies on /api/search being a read; there must be no POST variant.
    assert '@app.get("/api/search")' in SERVER
    assert '@app.post("/api/search")' not in SERVER
