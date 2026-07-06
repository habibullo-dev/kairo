"""CLI wiring for the workstation (Phase 8, Task 9) — the composition contract.

Like test_voice_cli, the point is the *composition*, not I/O: `jarvis --ui` builds the app
from the REPL's collaborators with the UI approver seams — the turn loop's approver is the
UIApprover, the REPL's gate is shared, the turn lock is shared (so a UI turn can't interleave
a background job), and the read/mutate services are wired. Keyless: a FakeClient REPL, no DB.
"""

from __future__ import annotations

import io
from pathlib import Path

from rich.console import Console

from jarvis.cli.repl import Repl, build_ui_app, run_ui
from jarvis.config import load_config
from jarvis.core import FakeClient
from jarvis.core.prompts import VOICE_GUIDANCE
from jarvis.ui.approver import UIApprover
from jarvis.ui.session import UiSession


def _console() -> Console:
    return Console(file=io.StringIO(), force_terminal=False, width=100)


def _app(tmp_path: Path):
    config = load_config(root=tmp_path, env_file=None)
    repl = Repl(config, client=FakeClient([]), console=_console())
    return repl, build_ui_app(config, repl=repl)


def test_turn_loop_approver_is_the_ui_approver(tmp_path: Path) -> None:
    repl, app = _app(tmp_path)
    session = app.state.session
    assert isinstance(session, UiSession)
    assert isinstance(session.loop.approver, UIApprover)  # every ASK → the Gate queue
    assert session.loop.approver is app.state.ui_approver


def test_gate_and_turn_lock_are_shared(tmp_path: Path) -> None:
    repl, app = _app(tmp_path)
    # one gate: the loop, the approver's persist, and the policy view all see the same rules
    assert app.state.session.loop.gate is repl.gate
    assert app.state.gate is repl.gate
    # a UI turn is an interactive turn — serialized against background jobs
    assert app.state.session.turn_lock is repl.turn_lock


def test_services_bundle_wired(tmp_path: Path) -> None:
    repl, app = _app(tmp_path)
    svc = app.state.services
    assert svc.memory is repl.memory
    assert svc.tasks is repl.tasks
    assert svc.knowledge is repl.knowledge


def test_ui_loop_is_not_voice_framed(tmp_path: Path) -> None:
    _repl, app = _app(tmp_path)
    # the workstation is a typed surface — no voice framing in the turn loop's system prompt
    assert VOICE_GUIDANCE not in app.state.session.loop.system


async def test_run_ui_disabled_prints_and_returns(tmp_path: Path) -> None:
    config = load_config(root=tmp_path, env_file=None)  # ui.enabled defaults False
    out = io.StringIO()
    console = Console(file=out, force_terminal=False, width=100)
    await run_ui(config, console=console)  # returns immediately; never opens the DB
    assert "not enabled" in out.getvalue().lower()
