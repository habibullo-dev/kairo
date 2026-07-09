"""GET /api/workspace/{id}/office route + its manual secret sweep (Phase 14 Task 1). The office
GET is PARAMETERIZED, so the whole-GET sweep in test_ui_readmodels skips it — this pins that no key
value and no member prompt/report body ever crosses this route, and that it returns the projection.
Keyless TestClient with real stores over a temp DB."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import jarvis.core  # noqa: F401 - load core first (ledger<->core.context import cycle in isolation)
from jarvis.agents import AgentRunStore
from jarvis.config import load_config
from jarvis.orchestration import OrchestrationStore
from jarvis.persistence.db import connect
from jarvis.projects import ProjectService, ProjectStore
from jarvis.ui.auth import SESSION_COOKIE, AuthManager
from jarvis.ui.readmodels import UiServices
from jarvis.ui.server import create_app

_OPEN: list = []


@pytest.fixture(autouse=True)
async def _close():
    yield
    while _OPEN:
        await _OPEN.pop().close()


async def _client(tmp_path: Path, *, seed_run: bool = True):
    cfg = load_config(root=tmp_path, env_file=None)
    cfg.secrets = cfg.secrets.model_copy(
        update={
            "anthropic_api_key": "SECRET-CANARY-ANTHROPIC",
            "openai_api_key": "SECRET-CANARY-OPENAI",
            "firecrawl_api_key": "SECRET-CANARY-FIRECRAWL",
        }
    )
    db = await connect(tmp_path / "office.db")
    _OPEN.append(db)
    lock = asyncio.Lock()
    pstore = ProjectStore(db, lock)
    await pstore.create(name="P")  # id 1
    store, run_store = OrchestrationStore(db, lock), AgentRunStore(db, lock)
    if seed_run:
        rid = await store.begin_run(
            project_id=1, workflow="security_review", title="Security · review",
            config={"team": "security"}, context_manifest=[],
            estimated_cost_usd=0.4, budget_usd=2.0,
        )
        mid = await run_store.begin_run(
            parent_session_id=None, parent_trace_id=None, title="security:lead",
            prompt="SECRET-PROMPT-CANARY", tools_scope=["read_file"], project_id=1,
            orchestration_run_id=rid, role="security", stage="council",
        )
        await run_store.complete_run(mid, status="ok", result_text="SECRET-REPORT-CANARY")
    auth = AuthManager(token="tok")
    app = create_app(cfg, auth=auth)
    app.state.services = UiServices(
        orchestration=store, run_store=run_store, projects=ProjectService(pstore)
    )
    return TestClient(app, base_url="http://127.0.0.1"), auth


def _hdr(auth: AuthManager) -> dict[str, str]:
    return {"cookie": f"{SESSION_COOKIE}={auth.mint_session()}"}


async def test_office_route_returns_projection(tmp_path: Path) -> None:
    client, auth = await _client(tmp_path)
    r = client.get("/api/workspace/1/office", headers=_hdr(auth))
    assert r.status_code == 200
    data = r.json()
    assert data["head"]["label"] == "Fable"
    assert data["stages"] == ["council", "synthesis", "execution", "review", "verdict"]
    assert {"security", "research"} <= {room["team"] for room in data["rooms"]}
    assert data["live"] and data["live"]["team"] == "security"


async def test_office_route_leaks_no_secret_or_body(tmp_path: Path) -> None:
    # Manual sweep for the PARAMETERIZED office GET (the whole-GET sweep skips {param} routes).
    client, auth = await _client(tmp_path)
    r = client.get("/api/workspace/1/office", headers=_hdr(auth))
    blob = r.text + "\n" + "\n".join(f"{k}: {v}" for k, v in r.headers.items())
    for needle in (
        "SECRET-CANARY-ANTHROPIC", "SECRET-CANARY-OPENAI", "SECRET-CANARY-FIRECRAWL",
        "SECRET-PROMPT-CANARY", "SECRET-REPORT-CANARY",
    ):
        assert needle not in blob, f"{needle!r} leaked on GET /api/workspace/1/office"


async def test_office_route_requires_session(tmp_path: Path) -> None:
    client, _auth = await _client(tmp_path, seed_run=False)
    assert client.get("/api/workspace/1/office").status_code == 401  # no session cookie
