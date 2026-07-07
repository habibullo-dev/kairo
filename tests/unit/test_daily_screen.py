"""Daily screen zones + workflows (Phase 9 Task 10) — content pins over the hand-written JS.

The server behavior (routes, read model) is pinned elsewhere; here we pin the calm-UI product
rules: workflows go through the one gated turn path, the eval chip is a copy-command not a run
button (ADR-0005), digest/repo content is rendered as text (never HTML/linkified), and the
attention order (approval > activity > briefing) holds.
"""

from __future__ import annotations

from jarvis.ui.server import STATIC_DIR

DAILY = (STATIC_DIR / "screens" / "daily.js").read_text(encoding="utf-8")
APP = (STATIC_DIR / "app.js").read_text(encoding="utf-8")


def test_workflows_submit_through_the_turn_path() -> None:
    # Workflow chips are prepared prompts through POST /api/turn — no new action authority.
    assert "/api/turn" in DAILY
    assert "Summarize my inbox" in DAILY and "Prepare a reply" in DAILY
    # Draft prep ends at gmail_create_draft (which will ASK) — never an auto-send.
    assert "gmail_create_draft" in DAILY


def test_run_digest_button_posts_to_digest_run() -> None:
    assert "/api/digest/run" in DAILY
    assert "Run digest now" in DAILY


def test_eval_chip_is_copy_command_not_run_button() -> None:
    # ADR-0005: the eval gate stays a terminal ritual. Daily shows the command to copy, and
    # there is NO client-side eval-run trigger.
    assert "eval-cmd" in DAILY and "Copy" in DAILY
    assert "/api/eval" not in DAILY and "eval/run" not in DAILY


def test_digest_and_commit_text_rendered_as_textcontent() -> None:
    # Untrusted content (digest summary, snippets, commit subjects) is rendered with
    # textContent — never innerHTML, never linkified into a clickable/exfil URL.
    assert "summary.textContent = d.summary" in DAILY  # digest summary as text
    assert "line.textContent" in DAILY  # commit subjects as text
    assert "chip.textContent = action" in DAILY  # suggested actions as text (display only)
    briefing = DAILY.split("function fillBriefing")[1].split("function fillChanged")[0]
    assert "innerHTML = d" not in briefing  # digest content never set via innerHTML
    assert 'createElement("a")' not in briefing  # digest summary/actions never become links


def test_attention_order_pending_before_briefing() -> None:
    # One primary attention surface: pending approval renders before activity, then briefing.
    # Anchor on code-only markers (the header comment mentions the zone names too).
    body = DAILY.split("function renderZones")[1]
    assert body.index("zone-pending") < body.index("Kairo is idle") < body.index("daily-briefing")
    assert "daily-now-lead" in DAILY  # the settle IDs the status bar shares are preserved


def test_demo_badge_present() -> None:
    assert "Demo data" in DAILY  # demo digests are clearly badged


def test_app_routes_notice_messages() -> None:
    # Background notices reach the browser and a digest notice refreshes Daily.
    assert 'msg.kind === "notice"' in APP
    assert "onNotice" in APP
