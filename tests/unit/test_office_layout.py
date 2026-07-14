"""Office layout persistence is localStorage-ONLY (Phase 14 M3). Like ui/theme.js, the per-project
view layout (mode + collapsed rooms) lives client-side — there is deliberately NO server layout
route, so no new authority and the mutation-route closed set is unchanged (stays 35). Values are
clamped on read (localStorage is user-writable). Structural pins over the shipped JS."""

from __future__ import annotations

from jarvis.ui.server import STATIC_DIR

OFFICE_JS = (STATIC_DIR / "screens" / "workspace" / "office.js").read_text(encoding="utf-8")


def test_layout_persists_per_project_in_localstorage() -> None:
    assert "kira:office:" in OFFICE_JS  # canonical per-project key (theme.js convention)
    assert "kairo:office:" in OFFICE_JS  # previous-brand value is migrated once
    assert "readMigrated(" in OFFICE_JS and "writeStored(" in OFFICE_JS


def test_layout_mode_is_clamped_on_read() -> None:
    # A tampered/garbage localStorage value can never widen the mode beyond the two known layouts.
    assert 'raw.mode === "office" ? "office" : "compact"' in OFFICE_JS


def test_collapsible_rooms_persist() -> None:
    assert "toggleRoomCollapse" in OFFICE_JS and "room-caret" in OFFICE_JS
    assert "_mounted.collapsed" in OFFICE_JS  # the collapsed set is tracked + saved


def test_layout_adds_no_server_route() -> None:
    # localStorage ONLY — the plan's optional POST /api/projects/{id}/office-layout is NOT shipped,
    # so the mutation-route closed set (pinned at 35 in test_ui_readmodels) is untouched, and the
    # Office writes nothing to a project/settings route.
    assert "office-layout" not in OFFICE_JS
    assert "/api/projects/" not in OFFICE_JS
