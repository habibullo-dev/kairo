"""Studio API + OrchestrationController + orchestration read models (Phase 10B Task 15).

Pins: the controller's one-run-at-a-time / project-required / two-step-confirm / cancel logic
(against a fake engine); the run-detail read model is bodies-free (member metadata only — a
child's prompt/result_text never reaches a UI surface); and the /api/studio + /api/orchestration
routes are wired and presence/metadata-only. Keyless: a fake engine + temp SQLite stores.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from jarvis.agents import AgentRunStore
from jarvis.config import BudgetsConfig, load_config
from jarvis.orchestration import WORKFLOWS, OrchestrationStore, estimate_run, resolve_team
from jarvis.orchestration.estimate import RunEstimate
from jarvis.persistence.db import connect
from jarvis.projects import ProjectStore
from jarvis.ui.auth import SESSION_COOKIE, AuthManager
from jarvis.ui.orchestration import OrchestrationController, serialize_estimate
from jarvis.ui.readmodels import (
    UiServices,
    orchestration_run_detail,
    orchestration_runs_view,
    teams_catalog,
    workflows_catalog,
)
from jarvis.ui.server import create_app

_OPEN: list = []


@pytest.fixture(autouse=True)
async def _close():
    yield
    while _OPEN:
        await _OPEN.pop().close()


# --- a fake engine + fake collaborators for the controller ------------------


class FakeEngine:
    def __init__(self, *, estimate: RunEstimate | None = None, run_id: int = 7) -> None:
        self._est = estimate
        self._run_id = run_id
        self.gate = asyncio.Event()  # release to let a launched run finish
        self.calls: list[dict] = []

    def check_provider_context(self, team, context) -> None:
        return None  # Phase 10C: the real engine refuses PRIVATE→non-trusted; no-op here

    def estimate(self, team, workflow, context, *, budget_usd=None) -> RunEstimate | None:
        return self._est

    async def run(self, **kw) -> int:
        self.calls.append(kw)
        sink = kw.get("on_event")
        if sink is not None:
            await sink(
                {"kind": "orchestration_started", "run_id": self._run_id, "schema_version": 2}
            )
        await self.gate.wait()  # stay in flight until the test releases it
        if sink is not None:
            await sink({"kind": "orchestration_completed", "run_id": self._run_id, "status": "ok"})
        return self._run_id


class _FakeProjStore:
    async def get(self, pid):  # no per-project overrides in these tests
        return None


class FakeProjects:
    def __init__(self, pid: int | None) -> None:
        self._pid = pid
        self.store = _FakeProjStore()

    def current(self):
        return SimpleNamespace(project_id=self._pid, name="Proj")


class FakeConn:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def broadcast(self, payload: dict) -> None:
        self.sent.append(payload)


def _estimate(decision: str = "ok") -> RunEstimate:
    return RunEstimate(
        total_usd=1.5,
        members=(),
        head_usd=0.2,
        unpriced=(),
        over_role_cap=(),
        over_team_budget=False,
        soft_warn=False,
        decision=decision,
        reason="x",
    )


def _controller(
    pid: int | None, *, estimate=None
) -> tuple[OrchestrationController, FakeEngine, FakeConn]:
    engine = FakeEngine(estimate=estimate)
    conn = FakeConn()
    ctrl = OrchestrationController(engine=engine, connections=conn, projects=FakeProjects(pid))
    return ctrl, engine, conn


# --- controller logic -------------------------------------------------------


async def test_start_requires_active_project() -> None:
    ctrl, _e, _c = _controller(None)  # global scope
    body, code = await ctrl.start(team_id="backend", workflow_id="implement", task="do it")
    assert code == 400 and "project" in body["message"]


async def test_start_rejects_empty_task_and_unknown_workflow() -> None:
    ctrl, _e, _c = _controller(1)
    b1, c1 = await ctrl.start(team_id="backend", workflow_id="implement", task="  ")
    assert c1 == 400 and "task brief" in b1["message"]
    b2, c2 = await ctrl.start(team_id="backend", workflow_id="nope", task="x")
    assert c2 == 400 and "unknown workflow" in b2["message"]


async def test_needs_confirmation_then_launch() -> None:
    ctrl, engine, _c = _controller(1, estimate=_estimate("confirm"))
    body, code = await ctrl.start(team_id="backend", workflow_id="implement", task="build x")
    assert code == 200 and body["needs_confirmation"] is True
    assert not engine.calls  # nothing launched on the confirm gate
    # Confirm ⇒ launch.
    body2, code2 = await ctrl.start(
        team_id="backend", workflow_id="implement", task="build x", confirmed=True
    )
    assert code2 == 202 and body2["started"] is True
    engine.gate.set()
    await ctrl._task  # let the launched run finish cleanly
    assert engine.calls and engine.calls[0]["confirmed"] is True


async def test_one_run_at_a_time_returns_409() -> None:
    ctrl, engine, _c = _controller(1, estimate=_estimate("ok"))
    _b, code = await ctrl.start(team_id="backend", workflow_id="implement", task="a")
    assert code == 202 and ctrl.busy
    b2, code2 = await ctrl.start(team_id="backend", workflow_id="implement", task="b")
    assert code2 == 409 and "already in flight" in b2["message"]
    engine.gate.set()
    await ctrl._task


async def test_cancel_targets_the_in_flight_run() -> None:
    ctrl, engine, _c = _controller(1, estimate=_estimate("ok"))
    await ctrl.start(team_id="backend", workflow_id="implement", task="a")
    await asyncio.sleep(0.01)  # let the started event set _current_run_id
    assert ctrl.cancel(engine._run_id) is True
    await ctrl._task  # the _run wrapper swallows the cancel (background task never errors)
    assert not ctrl.busy
    assert ctrl.cancel(engine._run_id) is False  # nothing in flight now


async def test_started_event_is_broadcast() -> None:
    ctrl, engine, conn = _controller(1, estimate=_estimate("ok"))
    await ctrl.start(team_id="backend", workflow_id="implement", task="a")
    await asyncio.sleep(0.01)
    assert any(p["kind"] == "orchestration_started" for p in conn.sent)
    engine.gate.set()
    await ctrl._task


def test_serialize_estimate_is_metadata_only() -> None:
    est = estimate_run(
        team=resolve_team("backend"),
        workflow=WORKFLOWS["implement"],
        registry=__import__("jarvis.models.registry", fromlist=["ModelRegistry"]).ModelRegistry(),
        pricing=__import__("jarvis.observability.cost", fromlist=["load_pricing"]).load_pricing(
            None
        ),
        budgets=BudgetsConfig(confirm_above_usd=1e9),
        context_tokens=200,
        max_rounds=3,
        iterations=6,
        out_per_call=2048,
    )
    s = serialize_estimate(est)
    assert s["decision"] == "ok" and s["total_usd"] > 0
    assert all({"member_id", "role", "model", "turns"} <= set(m) for m in s["members"])
    assert "prompt" not in str(s) and "secret" not in str(s).lower()


# --- read models: bodies-free run detail ------------------------------------


async def _stores(tmp_path: Path):
    db = await connect(tmp_path / "o.db")
    _OPEN.append(db)
    lock = asyncio.Lock()
    await ProjectStore(db, lock).create(name="P")  # id 1
    return OrchestrationStore(db, lock), AgentRunStore(db, lock)


async def test_run_detail_is_bodies_free(tmp_path: Path) -> None:
    store, run_store = await _stores(tmp_path)
    rid = await store.begin_run(
        project_id=1,
        workflow="security_review",
        title="Security · review",
        config={"team": "security"},
        context_manifest=[{"kind": "task_brief", "ref": "brief", "sha256": "abc", "tokens_est": 5}],
        estimated_cost_usd=0.4,
        budget_usd=2.0,
    )
    await store.complete_run(rid, status="ok", verdict="accept", synthesis_summary="looks fine")
    # A member run carrying a secret prompt + report — neither may surface in the read model.
    mid = await run_store.begin_run(
        parent_session_id=None,
        parent_trace_id=None,
        title="security:sec_lead",
        prompt="SECRET-PROMPT-CANARY",
        tools_scope=["read_file"],
        project_id=1,
        orchestration_run_id=rid,
        role="security",
        stage="council",
    )
    await run_store.complete_run(mid, status="ok", result_text="SECRET-REPORT-CANARY")

    detail = await orchestration_run_detail(store, run_store, rid)
    blob = str(detail)
    assert "SECRET-PROMPT-CANARY" not in blob and "SECRET-REPORT-CANARY" not in blob
    assert detail["run"]["synthesis_summary"] == "looks fine"
    assert detail["run"]["verdict"] == "accept"
    member = detail["members"][0]
    assert (
        member["role"] == "security" and member["stage"] == "council" and member["status"] == "ok"
    )
    assert "prompt" not in member and "result_text" not in member  # bodies-free


async def test_runs_view_lists_summaries(tmp_path: Path) -> None:
    store, _rs = await _stores(tmp_path)
    await store.begin_run(
        project_id=1,
        workflow="research",
        title="Research · research",
        config={"team": "research"},
        context_manifest=[],
        estimated_cost_usd=None,
        budget_usd=None,
    )
    view = await orchestration_runs_view(store, project_id=1)
    assert len(view["runs"]) == 1 and view["runs"][0]["team"] == "research"


def test_catalog_read_models_are_complete() -> None:
    teams = teams_catalog()
    assert len(teams) == 8 and all("members" in t and "id" in t for t in teams)
    # Every roster member exposes its route/tools/services/capability (Studio roster cards).
    for t in teams:
        for m in t["members"]:
            assert {"route_role", "tools", "services", "capability"} <= set(m)
    wfs = workflows_catalog()
    assert any(w["has_execution"] for w in wfs) and any(not w["has_execution"] for w in wfs)


# --- API routes: /api/studio bootstrap + /api/orchestration wiring ----------


def _client(tmp_path: Path, *, services=None, orchestrator=None):
    cfg = load_config(root=tmp_path, env_file=None)
    auth = AuthManager(token="tok")
    app = create_app(cfg, auth=auth, services=services)
    if orchestrator is not None:
        app.state.orchestrator = orchestrator
    return TestClient(app, base_url="http://127.0.0.1"), auth


def _cookie(auth: AuthManager) -> dict[str, str]:
    return {"cookie": f"{SESSION_COOKIE}={auth.mint_session()}"}


def test_studio_bootstrap_route(tmp_path: Path) -> None:
    client, auth = _client(tmp_path)
    r = client.get("/api/studio", headers=_cookie(auth))
    assert r.status_code == 200
    data = r.json()
    assert len(data["teams"]) == 8 and len(data["workflows"]) >= 8
    assert isinstance(data["services"], list) and isinstance(data["model_routes"], list)
    # presence-only: a service row names its credential envs but never a value
    assert all("credentials_present" in s for s in data["services"])


def test_orchestration_list_503_without_store(tmp_path: Path) -> None:
    client, auth = _client(tmp_path, services=UiServices())  # orchestration=None
    assert client.get("/api/orchestration", headers=_cookie(auth)).status_code == 503


async def test_orchestration_list_route_with_store(tmp_path: Path) -> None:
    store, _rs = await _stores(tmp_path)
    await store.begin_run(
        project_id=1,
        workflow="research",
        title="Research · research",
        config={"team": "research"},
        context_manifest=[],
        estimated_cost_usd=None,
        budget_usd=None,
    )
    client, auth = _client(tmp_path, services=UiServices(orchestration=store))
    r = client.get("/api/orchestration", headers=_cookie(auth))
    assert r.status_code == 200 and len(r.json()["runs"]) == 1
