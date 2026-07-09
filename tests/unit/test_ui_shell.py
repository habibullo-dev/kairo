"""Shell / IA / keyboard pins (Phase 11 T6).

T6 reworks the rail into a primary area (Daily/Projects/Studio/Costs/Settings) + a utility area
(Gate/Trace/Hub/Lab/Meetings), adds hash-arg routing (#workspace/{id}), a single keyboard
dispatcher (ui/keys.js), and a WS event bus (ui/bus.js). These are content pins over the
hand-written assets — the shell's load-bearing semantics (WS surface protocol, the nonce-minted
approval flow, server-side enforcement) are unchanged. The keyboard/bus layers add NO authority.
"""

from __future__ import annotations

from jarvis.ui.server import STATIC_DIR

INDEX = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
APP_JS = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
KEYS_JS = (STATIC_DIR / "ui" / "keys.js").read_text(encoding="utf-8")
BUS_JS = (STATIC_DIR / "ui" / "bus.js").read_text(encoding="utf-8")
THEME_JS = (STATIC_DIR / "ui" / "theme.js").read_text(encoding="utf-8")
SETTINGS_JS = (STATIC_DIR / "screens" / "settings.js").read_text(encoding="utf-8")
CSS = (STATIC_DIR / "kairo.css").read_text(encoding="utf-8")


def test_primary_rail_is_the_five_places() -> None:
    for screen in ("daily", "projects", "studio", "costs", "settings"):
        assert f'data-screen="{screen}"' in INDEX, screen
    assert ">Settings<" in INDEX
    # Vault/Tasks/Memory are no longer rail entries (they become Workspace tabs in T10). They
    # stay reachable by hash, so their modules must still exist (test_ui_screens covers that),
    # but the rail must not advertise them as top-level destinations.
    for gone in ("vault", "tasks", "memory"):
        assert f'data-screen="{gone}"' not in INDEX, gone


def test_utility_area_after_the_spacer() -> None:
    spacer = INDEX.index('class="spacer"')
    for screen in ("gate", "trace", "hub", "lab", "meetings"):
        assert INDEX.index(f'data-screen="{screen}"') > spacer, screen


def test_rail_surfaces_workspace_artifacts_and_search() -> None:
    # Phase 15.5: the rail exposes the previously-hidden power surfaces. Workspace deep-links to
    # the ACTIVE project (app.js sets its href + hides it in global scope); Artifacts is a
    # first-class destination; a ⌘K Search affordance opens the command palette.
    spacer = INDEX.index('class="spacer"')
    for screen in ("workspace", "artifacts"):
        assert f'data-screen="{screen}"' in INDEX
        assert INDEX.index(f'data-screen="{screen}"') < spacer  # primary area, before the spacer
    assert 'id="rail-workspace"' in INDEX and 'id="rail-search"' in INDEX
    assert "rail-search" in APP_JS and "openPalette" in APP_JS  # the button opens the palette
    assert "rail-workspace" in APP_JS and "#workspace/" in APP_JS  # href from the active project


def test_gate_badge_is_persistent_in_the_rail() -> None:
    # The pending-approval count rides the Gate rail entry and updates live from the socket.
    assert 'id="gate-badge"' in INDEX
    assert "updateGateBadge" in APP_JS


def test_router_supports_hash_args() -> None:
    # #workspace/{id} routes to the workspace screen with positional args (T10 consumes them).
    assert "parseHash" in APP_JS
    assert "routeArgs" in APP_JS
    assert "state.routeArgs" in APP_JS  # args threaded into the screen render


def test_keys_module_is_the_single_dispatcher() -> None:
    # One document keydown listener owns the shortcut surface.
    assert APP_JS.count('addEventListener("keydown"') == 0  # app.js delegates to keys.js
    assert 'addEventListener("keydown"' in KEYS_JS
    assert KEYS_JS.count('addEventListener("keydown"') == 1
    # Ctrl/Cmd-K is the palette hotkey (bound by the palette in T7 via setPaletteToggle).
    assert "ctrlKey || ev.metaKey" in KEYS_JS
    assert "setPaletteToggle" in KEYS_JS
    # Escape closes the top-most overlay (a stack), and per-screen scopes clear on navigate.
    assert "Escape" in KEYS_JS and "pushEscape" in KEYS_JS
    assert "clearScope" in KEYS_JS and "clearScope()" in APP_JS


def test_escape_dismisses_the_approval_modal() -> None:
    # The approval modal registers itself on the Escape stack when shown, and unregisters when
    # hidden — Escape dismisses it (leaving the decision pending), never auto-approves/denies.
    assert "pushEscape(hideApproval)" in APP_JS
    assert 'resolveApproval("approve")' not in KEYS_JS  # keys.js holds no approval authority


def test_unknown_route_never_interpolates_the_hash_into_innerhtml() -> None:
    # state.route comes from the hash (attacker-influenceable). The unknown-route fallback must
    # render with textContent, never interpolate the route name into innerHTML.
    assert "${cap(state.route)}" not in APP_JS
    assert "Unknown screen." in APP_JS
    assert "h.textContent = cap(state.route)" in APP_JS


def test_bus_is_a_ui_only_fanout() -> None:
    for name in ("export function on", "export function emit"):
        assert name in BUS_JS
    # app.js feeds the bus but the bus carries no fetch/post (it is not an authority boundary).
    assert "busEmit(" in APP_JS
    assert "fetch(" not in BUS_JS and "api.post" not in BUS_JS


def test_settings_appearance_adds_no_authority() -> None:
    # Appearance is client-side via theme.js (localStorage) — no server theme route. T14 status
    # sections READ existing endpoints (api.get) but Settings NEVER mutates: no api.post, no
    # direct fetch.
    assert "api.post" not in SETTINGS_JS
    assert "fetch(" not in SETTINGS_JS
    assert "theme.js" in SETTINGS_JS


def test_two_theme_controls_stay_in_sync() -> None:
    # The status-bar toggle and the Settings appearance row set the same theme state. theme.js
    # publishes one "appearance" event on every user change; app.js subscribes once and re-syncs
    # both the toggle pill and (if open) the Settings screen — so they never disagree.
    assert 'busEmit("appearance"' in THEME_JS
    assert 'busOn("appearance"' in APP_JS
    assert "syncTheme()" in APP_JS and 'refreshIfActive("settings")' in APP_JS


def test_unknown_route_uses_own_property_lookup() -> None:
    # state.route is hash-derived; screen resolution must be an own-property lookup so
    # "#__proto__" falls through to the safe unknown-route branch, not Object.prototype.
    assert "Object.hasOwn(screens, state.route)" in APP_JS


def test_layout_knob_widens_the_reading_column() -> None:
    # The Settings "Layout" control promises a wider reading column; the shell grid reads the
    # --nav knob and the expanded layout widens .screen, so the control is not inert.
    # minmax(0, 1fr) lets the main column shrink so a wide status bar can't force page overflow.
    assert "grid-template-columns: var(--nav) minmax(0, 1fr);" in CSS
    assert ':root[data-layout="expanded"] .screen { max-width:' in CSS
