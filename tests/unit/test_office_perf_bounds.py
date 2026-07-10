"""Office performance + accessibility pins (Phase 14 M4). A long/large run must not degrade the
view: bus events are coalesced into one requestAnimationFrame repaint, and every live structure is
bounded (the feed is capped in BOTH the DOM and the pending buffer; per-member/per-room patches
dedupe via a Map/Set). Keyboard + ARIA make it navigable without a mouse and legible without color.
Keyless: structural pins over the shipped JS + the Phase 14 CSS block (the full-render pixel DoD
runs in the screenshot-harness env)."""

from __future__ import annotations

import re

from jarvis.ui.server import STATIC_DIR

OFFICE_JS = (STATIC_DIR / "screens" / "workspace" / "office.js").read_text(encoding="utf-8")
CSS = (STATIC_DIR / "kairo.css").read_text(encoding="utf-8")
CSS_BLOCK = re.search(r"/\* --- Phase 14: AI Team Office.*\Z", CSS, re.S).group(0)


# --- performance bounds ---------------------------------------------------
def test_live_feed_is_capped_in_dom_and_buffer() -> None:
    assert re.search(r"FEED_CAP\s*=\s*\d+", OFFICE_JS)  # a fixed numeric cap
    # DOM: trims oldest rows past the cap; buffer: queueFeed shifts so it can't grow unbounded
    # even if a flush is delayed (backgrounded tab).
    assert "feed.children.length > FEED_CAP" in OFFICE_JS and "feed.removeChild" in OFFICE_JS
    assert "_pending.feed.length > FEED_CAP" in OFFICE_JS and "_pending.feed.shift()" in OFFICE_JS


def test_bus_repaints_are_coalesced_into_one_frame() -> None:
    # Events mutate the model + mark pending work; one rAF flush repaints (guarded single-flight).
    assert "requestAnimationFrame(flushPending)" in OFFICE_JS
    assert "if (_rafId) return" in OFFICE_JS  # never schedules two frames at once
    assert "function flushPending" in OFFICE_JS


def test_pending_patch_structures_are_bounded() -> None:
    # rooms dedupe via a Set (<= teams), member patches via a Map keyed by team::role (<= members):
    # a burst can never accumulate an unbounded array of pending work; flush clears them.
    assert "rooms: new Set()" in OFFICE_JS and "nodes: new Map()" in OFFICE_JS
    assert "_pending.nodes.set(" in OFFICE_JS
    assert "p.rooms.clear(); p.nodes.clear(); p.feed.length = 0" in OFFICE_JS


def test_no_full_rerender_on_events() -> None:
    # Never re-fetch or re-import the panel on a live event (the whole point of surgical patching).
    assert "refreshIfActive(" not in OFFICE_JS
    assert OFFICE_JS.count("api.get(") == 1  # exactly the initial load fetch, never on an event


def test_live_events_are_scoped_to_the_mounted_project_and_remount_clears_frames() -> None:
    # A project-A start event cannot make an Office mounted for project B adopt its run.  Any
    # already-queued rAF work is cancelled and cleared before the next Office mount.
    assert "msg.project_id !== _mounted.projectId" in OFFICE_JS
    assert "function resetPending" in OFFICE_JS and "cancelAnimationFrame(_rafId)" in OFFICE_JS
    assert "resetPending(); // an old project's scheduled rAF" in OFFICE_JS


# --- accessibility --------------------------------------------------------
def test_keyboard_roving_between_member_nodes() -> None:
    assert "function onFloorKeys" in OFFICE_JS
    assert "ArrowUp" in OFFICE_JS and "ArrowRight" in OFFICE_JS
    assert "next.focus()" in OFFICE_JS


def test_aria_and_non_color_status() -> None:
    assert '"aria-live": "polite"' in OFFICE_JS  # live feed announces politely
    assert 'role: "region"' in OFFICE_JS  # each room is a labelled region
    assert 'role: "list"' in OFFICE_JS and 'role: "listitem"' in OFFICE_JS  # stage map is a list
    assert "statusPill(n.status" in OFFICE_JS  # status carries a TEXT label, not color alone


def test_motion_is_gated_for_reduced_motion() -> None:
    assert "@media (prefers-reduced-motion: reduce)" in CSS_BLOCK
    assert "animation: none" in CSS_BLOCK
