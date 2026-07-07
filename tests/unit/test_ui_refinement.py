"""Refined Daily/Gate/voice UI pins (Phase 8 refinement).

The refinement adopts the reference's product structure but must preserve the product rules:
the screen stays "Daily" (not "Command"), "Always allow" is visibly de-emphasized vs
"Approve once", Debug is off by default and presentation-only, and the voice states are
surfaced. These are content pins over the hand-written assets (no server behavior changed).
"""

from __future__ import annotations

from jarvis.ui.server import STATIC_DIR

INDEX = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
CSS = (STATIC_DIR / "kairo.css").read_text(encoding="utf-8")
APP_JS = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
DAILY_JS = (STATIC_DIR / "screens" / "daily.js").read_text(encoding="utf-8")


def test_screen_is_named_daily_not_command() -> None:
    assert 'data-screen="daily"' in INDEX
    assert ">Daily<" in INDEX  # the nav label + the mode toggle
    assert "Command" not in INDEX  # the reference's name is deliberately NOT adopted


def test_always_allow_is_deemphasized_vs_approve_once() -> None:
    # Approve once is the PRIMARY filled button; Always allow is a secondary outline (same
    # weight as Deny) — a wider grant must never look as casual as a one-time approval.
    assert ".btn-approve { border: none; background: var(--cyan)" in CSS
    assert ".btn-always { border: 1px solid var(--line-2); background: var(--user-bubble)" in CSS
    # order in the modal: Approve once, then Always allow, then Deny
    assert INDEX.index("ap-approve") < INDEX.index("ap-always") < INDEX.index("ap-deny")


def test_debug_is_off_by_default_and_presentation_only() -> None:
    # The mode toggle defaults to Daily; Debug is a body class that only reveals telemetry.
    assert 'id="mode-daily" class="active"' in INDEX
    assert "<body>" in INDEX and "<body class=" not in INDEX  # no debug class at load
    assert ".debug-only { display: none; }" in CSS
    assert 'classList.toggle("debug"' in APP_JS
    # Debug must not gate any data call
    for line in APP_JS.splitlines():
        gates_call = "fetch(" in line or "api.post" in line or "api.get" in line
        if "debug" in line.lower() and gates_call:
            raise AssertionError(f"debug gates an action: {line.strip()}")


def test_daily_has_the_priority_zones() -> None:
    # Pending approval (amber) is a distinct zone from the current-activity/today cards —
    # one primary attention surface, ordered above the rest.
    assert "zone-pending" in DAILY_JS and "Waiting on you" in DAILY_JS
    assert "Kairo is working" in DAILY_JS and "Kairo is idle" in DAILY_JS
    assert "Conversation" in DAILY_JS
    assert "composer" in DAILY_JS  # sticky composer
    # the pending zone is rendered before the activity/today cards
    assert DAILY_JS.index("zone-pending") < DAILY_JS.index("Kairo is idle")


def test_voice_states_and_talk_button_present() -> None:
    assert 'id="st-mic"' in INDEX  # Talk button
    for state in ("listening", "transcribing", "thinking", "speaking"):
        assert state in APP_JS  # the voice-state labels
    assert 'class="msg heard"' in CSS or ".msg.heard" in CSS  # heard transcript bubble style
