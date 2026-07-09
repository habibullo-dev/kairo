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
