"""CLI wiring for the workstation (Phase 8, Task 9) — the composition contract.

Like test_voice_cli, the point is the *composition*, not I/O: `kira --ui` builds the app
from the REPL's collaborators with the UI approver seams — the turn loop's approver is the
UIApprover, the REPL's gate is shared, the turn lock is shared (so a UI turn can't interleave
a background job), and the read/mutate services are wired. Keyless: a FakeClient REPL, no DB.
"""

from __future__ import annotations

import io
from pathlib import Path
from types import SimpleNamespace

from rich.console import Console

from kira.attention import AttentionStore
from kira.cli.repl import (
    Repl,
    _build_project_intelligence_coordinator,
    _ui_access_urls,
    build_ui_app,
    run_ui,
)
from kira.config import load_config
from kira.core import FakeClient
from kira.core.prompts import VOICE_GUIDANCE
from kira.graph import GraphStore
from kira.intelligence import AnalysisJobStore, ProjectReportStore
from kira.knowledge.store import KnowledgeStore
from kira.orchestration import OrchestrationStore
from kira.persistence import SessionStore
from kira.persistence.db import connect
from kira.ui.approver import UIApprover
from kira.ui.auth import AuthManager
from kira.ui.readmodels import UiServices
from kira.ui.session import UiSession


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


def test_owner_auth_is_passed_through_composition(tmp_path: Path) -> None:
    config = load_config(root=tmp_path, env_file=None)
    repl = Repl(config, client=FakeClient([]), console=_console())
    owner_auth = object()
    app = build_ui_app(config, repl=repl, owner_auth=owner_auth)
    assert app.state.owner_auth is owner_auth


def test_ui_access_urls_separate_normal_login_from_recovery(tmp_path: Path) -> None:
    config = load_config(root=tmp_path, env_file=None)
    config.ui.host = "127.0.0.1"
    config.ui.port = 8787
    auth = AuthManager(token="one-use-token")
    assert _ui_access_urls(config, auth, enrolled=False) == {
        "setup": "http://127.0.0.1:8787/?token=one-use-token"
    }
    assert _ui_access_urls(config, auth, enrolled=True) == {
        "login": "http://127.0.0.1:8787/login",
        "recovery": "http://127.0.0.1:8787/?token=one-use-token",
    }


async def test_project_intelligence_stores_share_the_host_database(tmp_path: Path) -> None:
    config = load_config(root=tmp_path, env_file=None)
    db = await connect(tmp_path / "ui.db")
    try:
        store = SessionStore(db)
        repl = Repl(config, client=FakeClient([]), console=_console(), store=store)
        app = build_ui_app(config, repl=repl)
        assert isinstance(app.state.services.analysis_jobs, AnalysisJobStore)
        assert isinstance(app.state.services.project_reports, ProjectReportStore)
        assert app.state.services.analysis_jobs.db is db
        assert app.state.services.project_reports.lock is store.lock
        assert app.state.project_intelligence is None  # explicit feature gate remains off
    finally:
        await db.close()


async def test_project_intelligence_coordinator_requires_all_safe_dependencies(
    tmp_path: Path,
) -> None:
    config = load_config(root=tmp_path, env_file=None)
    config.project_intelligence.enabled = True
    db = await connect(tmp_path / "coordinator.db")
    try:
        store = SessionStore(db)
        knowledge = SimpleNamespace(store=KnowledgeStore(db, store.lock))
        repl = SimpleNamespace(
            knowledge=knowledge,
            graph=GraphStore(db, store.lock),
            tasks=None,
        )
        services = UiServices(
            analysis_jobs=AnalysisJobStore(db, store.lock),
            project_reports=ProjectReportStore(db, store.lock),
            attention=AttentionStore(db, store.lock),
            orchestration=OrchestrationStore(db, store.lock),
        )
        runner = object()
        coordinator = _build_project_intelligence_coordinator(
            config, repl=repl, services=services, runner=runner
        )
        assert coordinator is not None
        assert coordinator.knowledge is knowledge.store
        assert coordinator.runner is runner

        services.attention = None
        assert (
            _build_project_intelligence_coordinator(
                config, repl=repl, services=services, runner=runner
            )
            is None
        )
    finally:
        await db.close()


def test_ui_loop_is_not_voice_framed(tmp_path: Path) -> None:
    _repl, app = _app(tmp_path)
    # the workstation is a typed surface — no voice framing in the turn loop's system prompt
    assert VOICE_GUIDANCE not in app.state.session.loop.system


async def test_run_ui_disabled_prints_and_returns(tmp_path: Path) -> None:
    config = load_config(root=tmp_path, env_file=None)  # ui.enabled defaults False
    out = io.StringIO()
    console = Console(file=out, force_terminal=False, width=100)
    await run_ui(
        config,
        database=config.data_dir / "kira.db",
        console=console,
    )  # returns immediately; never opens the DB
    assert "not enabled" in out.getvalue().lower()
