"""GET /api/workspace/{id}/graph + /api/graph/node/{kind}/{ref_id:path} (Phase 15 Task 3). Both are
READ-ONLY and bodies-free; the parameterized GETs are swept manually (the whole-GET sweep in
test_ui_readmodels skips {param} routes) for member prompts/results + key values. Keyless TestClient
with real stores + the deterministic builder over a temp DB."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import jarvis.core  # noqa: F401 - load core first (ledger<->core.context import cycle in isolation)
from jarvis.agents import AgentRunStore
from jarvis.config import load_config
from jarvis.graph import GraphStore
from jarvis.graph.builder import rebuild
from jarvis.orchestration import OrchestrationStore
from jarvis.persistence.db import connect
from jarvis.projects import ProjectService, ProjectStore
from jarvis.ui.auth import SESSION_COOKIE, AuthManager
from jarvis.ui.readmodels import UiServices
from jarvis.ui.server import create_app

_OPEN: list = []
_TS = "2026-03-15T00:00:00+00:00"


@pytest.fixture(autouse=True)
async def _close():
    yield
    while _OPEN:
        await _OPEN.pop().close()


async def _client(tmp_path: Path):
    cfg = load_config(root=tmp_path, env_file=None)
    cfg.secrets = cfg.secrets.model_copy(update={"anthropic_api_key": "SECRET-CANARY-ANTHROPIC"})
    db = await connect(tmp_path / "graph.db")
    _OPEN.append(db)
    lock = asyncio.Lock()
    pstore = ProjectStore(db, lock)
    await pstore.create(name="P")  # id 1
    orch, runs = OrchestrationStore(db, lock), AgentRunStore(db, lock)
    rid = await orch.begin_run(project_id=1, workflow="security_review", title="Sec review",
                               config={"team": "security"}, context_manifest=[],
                               estimated_cost_usd=0.1, budget_usd=1.0)
    await runs.begin_run(parent_session_id=None, parent_trace_id=None, title="security:lead",
                         prompt="SECRET-PROMPT-CANARY", tools_scope=["read_file"], project_id=1,
                         orchestration_run_id=rid, role="security", stage="council")
    await db.execute("INSERT INTO sessions (created_at, updated_at, title, kind, project_id) "
                     "VALUES (?, ?, 'Chat', 'interactive', 1)", (_TS, _TS))
    await db.execute(
        "INSERT INTO kb_wiki_links (from_path, to_path, to_raw, link_kind, created_at) "
        "VALUES ('pages/a.md','pages/b.md','b','wikilink',?)", (_TS,))
    store = GraphStore(db, lock)
    await rebuild(store)
    auth = AuthManager(token="tok")
    app = create_app(cfg, auth=auth)
    app.state.services = UiServices(
        graph=store, orchestration=orch, run_store=runs, projects=ProjectService(pstore)
    )
    return TestClient(app, base_url="http://127.0.0.1"), auth, rid


def _hdr(auth: AuthManager) -> dict[str, str]:
    return {"cookie": f"{SESSION_COOKIE}={auth.mint_session()}"}


async def test_graph_route_returns_projection(tmp_path: Path) -> None:
    client, auth, _ = await _client(tmp_path)
    r = client.get("/api/workspace/1/graph?depth=2", headers=_hdr(auth))
    assert r.status_code == 200
    data = r.json()
    assert data["focus"] == "project:1"
    assert {"project", "chat", "run", "member"} <= {n["kind"] for n in data["nodes"]}
    assert data["edges"] and "by_kind" in data["counts"]


async def test_node_card_route_and_path_converter(tmp_path: Path) -> None:
    client, auth, rid = await _client(tmp_path)
    r = client.get(f"/api/graph/node/run/{rid}", headers=_hdr(auth))
    assert r.status_code == 200 and r.json()["kind"] == "run"
    # the {ref_id:path} converter lets a wiki path (with a slash) address a node.
    w = client.get("/api/graph/node/wiki/pages/a.md", headers=_hdr(auth))
    assert w.status_code == 200 and w.json()["kind"] == "wiki"


async def test_graph_routes_require_session(tmp_path: Path) -> None:
    client, _auth, rid = await _client(tmp_path)
    assert client.get("/api/workspace/1/graph").status_code == 401
    assert client.get(f"/api/graph/node/run/{rid}").status_code == 401


async def test_graph_routes_leak_no_secret_or_body(tmp_path: Path) -> None:
    client, auth, rid = await _client(tmp_path)
    blobs = [
        client.get("/api/workspace/1/graph?depth=2", headers=_hdr(auth)).text,
        client.get(f"/api/graph/node/run/{rid}", headers=_hdr(auth)).text,
    ]
    for blob in blobs:
        for needle in ("SECRET-PROMPT-CANARY", "SECRET-CANARY-ANTHROPIC"):
            assert needle not in blob, needle
