"""Daily activity settles after a denied gated turn (Phase 8 refinement bugfix).

Repro: submit a gated action → deny it → the modal closes, the Gate clears, the denial
message appears, but the Daily current-activity card lingered on "Kira is working" while the
status bar already said idle. Root cause: the card read a stale ``state.runner`` (only the
4s poll refreshed it). Fix: settle ``turn_busy`` on ``turn_completed`` and write BOTH surfaces
from one ``renderRunnerState()`` reading the same state.

Two pins: (1) the server-side settle signal the client derives from — a denied gated turn
completes, clears pending, and reports not-busy; (2) the client wiring that both surfaces
share the settled state.
"""

from __future__ import annotations

import asyncio
import io
from pathlib import Path

from rich.console import Console

from jarvis.cli.repl import Repl, build_ui_app
from jarvis.config import load_config
from jarvis.core import FakeClient, ToolCall, text_message, tool_use_message
from jarvis.core.execution import ExecutionContext, bind_execution_context
from jarvis.ui.server import STATIC_DIR


class _FakeWS:
    async def send_json(self, message: dict) -> None:  # a live watching client
        pass


def _console() -> Console:
    return Console(file=io.StringIO(), force_terminal=False, width=100)


async def _settle(cond, tries: int = 200) -> None:
    for _ in range(tries):
        if cond():
            return
        await asyncio.sleep(0)


async def test_denied_gated_turn_settles_to_idle(tmp_path: Path) -> None:
    config = load_config(root=tmp_path, env_file=None)
    # This scenario isolates the approval/settle lifecycle; the independent priced-chat preflight
    # cap would otherwise reject the fake turn before it reaches the Gate.
    config.chat.hard_stop_usd_per_turn = 0
    # (1) the model calls a gated tool (write_file = ASK); (2) after the denial, a denial reply.
    client = FakeClient(
        [
            tool_use_message([ToolCall("t1", "write_file", {"path": "notes.txt", "content": "x"})]),
            text_message("I couldn't do that — you denied it."),
        ]
    )
    repl = Repl(config, client=client, console=_console())
    app = build_ui_app(config, repl=repl)
    session, approvals, cm = app.state.session, app.state.approvals, app.state.connections
    context = ExecutionContext(session_id=101, project_id=None)
    conn = cm.register(_FakeWS(), owner_session="test")
    cm.bind_workspace(
        conn,
        owner_session="test",
        workspace_id="w" * 24,
        context=context,
    )

    # submit the gated action (fire-and-forget task, like the /api/turn route)
    with bind_execution_context(context):
        assert session.submit("write notes.txt please") is True
    await _settle(lambda: bool(approvals.pending()))
    assert session.busy is True  # working while it waits at the Gate

    # deny it (through the real replay-proof path)
    (pending,) = approvals.pending()
    nonce = await approvals.mint_nonce(pending.decision_id, conn)
    ok, _ = approvals.resolve(pending.decision_id, nonce, "deny")
    assert ok
    await _settle(lambda: not session.busy)

    # every bullet of the scenario, server-side:
    assert approvals.pending() == []  # modal closes / Gate count → 0
    completions = [e for e in session.ring if e.get("type") == "turn_completed"]
    assert completions, "a turn_completed event must reach the stream (the settle signal)"
    assert "denied" in completions[-1]["text"].lower()  # assistant denial message appears
    assert session.busy is False  # Daily activity returns to idle — NOT working

    # and the route the status bar + Daily card both read reports the settled state
    from jarvis.ui.server import _runner_status

    assert _runner_status(app.state.runner, session)["turn_busy"] is False


def test_daily_card_and_status_bar_share_one_settled_source() -> None:
    # The client wiring behind the fix: one function writes both surfaces from state.runner,
    # and a completed turn clears turn_busy immediately (not on the next poll).
    app_js = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    daily_js = (STATIC_DIR / "screens" / "daily.js").read_text(encoding="utf-8")
    assert "function renderRunnerState()" in app_js
    assert '"st-runner"' in app_js and '"daily-now-lead"' in app_js  # writes BOTH surfaces
    assert "turn_completed" in app_js and "turn_busy = false" in app_js  # settle on turn end
    assert 'id="daily-now-lead"' in daily_js and 'id="daily-now-dot"' in daily_js  # the card IDs
