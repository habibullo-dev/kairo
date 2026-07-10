"""Office tab registration + shell-allowlist pins (Phase 14 Task 2).

The AI Team Office is a project workspace tab reached at #workspace/{id}/office. It must be a
first-class member of the fixed TABS allowlist (so the shell's validated dynamic import will load
it), render from the read-only assembler GET, and — like every workspace panel — build its DOM
without innerHTML. Adding it must not weaken the allowlist gate that rejects a crafted hash tab.
Structural (keyless): reads the shipped JS as text via STATIC_DIR, the established pattern.
"""

from __future__ import annotations

from jarvis.ui.server import STATIC_DIR

WS_JS = (STATIC_DIR / "screens" / "workspace.js").read_text(encoding="utf-8")
OFFICE_JS = (STATIC_DIR / "screens" / "workspace" / "office.js").read_text(encoding="utf-8")


def test_office_is_in_the_tab_allowlist() -> None:
    assert '["office", "Office"]' in WS_JS  # tab bar entry (key + label)


def test_office_panel_exists_and_exports_render() -> None:
    assert (STATIC_DIR / "screens" / "workspace" / "office.js").is_file()
    assert "export async function render" in OFFICE_JS


def test_office_reads_the_readonly_assembler_get() -> None:
    # Composes the office projection scoped to the project; never the agent turn path.
    assert "/office" in OFFICE_JS and "/api/workspace/" in OFFICE_JS
    assert "/api/turn" not in OFFICE_JS
    assert "api.post(" not in OFFICE_JS


def test_office_builds_without_innerhtml() -> None:
    # Same invariant as every workspace panel: no innerHTML/insertAdjacentHTML — agent/service text
    # is inert (textContent only via el()/_util helpers).
    assert "innerHTML" not in OFFICE_JS
    assert "insertAdjacentHTML" not in OFFICE_JS


def test_office_renders_the_canonical_office_shape() -> None:
    # The compact view surfaces the head chair, the stage rail, team rooms, and the live feed.
    for marker in ("Team Office", "office-chair", "stage-rail", "room-card", "office-feed"):
        assert marker in OFFICE_JS, marker


def test_adding_office_did_not_weaken_the_unknown_tab_gate() -> None:
    # The shell still defaults an unrecognized (attacker-influenceable) tab to a safe known one,
    # rather than importing an off-allowlist module.
    assert 'TAB_KEYS.includes(args && args[1]) ? args[1] : "overview"' in WS_JS


def test_office_has_compact_and_office_view_modes() -> None:
    # A Compact (default) | Office segmented toggle that swaps ONLY the root layout class — a pure
    # CSS relayout, never a re-render or refetch. The Office is never the app default; this picks
    # the tab's inner layout.
    assert 'let _mode = "compact"' in OFFICE_JS  # compact is the default view mode
    assert "office-modes" in OFFICE_JS and "mode-btn" in OFFICE_JS
    assert '"Compact"' in OFFICE_JS and '"Office"' in OFFICE_JS
    assert "office-compact" in OFFICE_JS and "office-full" in OFFICE_JS
    assert "root.className = rootClass()" in OFFICE_JS  # toggle relayouts via the root class only
    assert "refreshIfActive(" not in OFFICE_JS  # the Office never CALLS a shell re-render


def test_office_scene_maps_metadata_to_rooms_and_safe_visual_states() -> None:
    for team in ("research", "frontend", "backend", "security", "qa", "pm", "ops", "lounge"):
        assert team in OFFICE_JS
    states = ("thinking", "researching", "coding", "reviewing", "waiting_for_approval",
              "blocked", "done", "idle")
    for state in states:
        assert state in OFFICE_JS
    assert "sceneState" in OFFICE_JS and "source_team" in OFFICE_JS
