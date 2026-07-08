"""Per-project service narrowing (Phase 13 Task 8): a project can NARROW the globally-enabled
services to a subset, never widen. Keyless. Covers the narrow-only route (subset enforced
server-side), the merge-safe store write, and the run-time enforcement (a narrowed-out service
tool refuses before doing anything)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

import jarvis.core  # noqa: F401 - load core first (ledger<->core.context import cycle in isolation)
from jarvis.config import load_config
from jarvis.persistence.db import connect
from jarvis.projects import ProjectService, ProjectStore
from jarvis.projects.context import ProjectContext
from jarvis.services.exa import ExaSearchTool
from jarvis.tools.base import ToolContext
from jarvis.ui.auth import SESSION_COOKIE, AuthManager
from jarvis.ui.server import create_app

_OPEN: list = []


@pytest.fixture(autouse=True)
def _reset_transport():
    yield
    ExaSearchTool.transport = None


@pytest.fixture(autouse=True)
async def _close():
    yield
    while _OPEN:
        await _OPEN.pop().close()


# --- the narrow-only route (subset enforced server-side) --------------------


async def _client(tmp_path: Path):
    cfg = load_config(root=tmp_path, env_file=None)
    cfg.services.enabled = ["firecrawl", "exa"]  # the global set a project may subset
    db = await connect(tmp_path / "p.db")
    _OPEN.append(db)
    store = ProjectStore(db, asyncio.Lock())
    auth = AuthManager(token="tok")
    app = create_app(cfg, auth=auth)
    app.state.projects = ProjectService(store)
    return TestClient(app, base_url="http://127.0.0.1"), auth, store


def _hdr(auth: AuthManager, *, post: bool = False) -> dict[str, str]:
    h = {"cookie": f"{SESSION_COOKIE}={auth.mint_session()}"}
    if post:
        h["origin"] = "http://127.0.0.1"
    return h


async def _make_project(client, auth) -> int:
    r = client.post("/api/projects", json={"name": "P"}, headers=_hdr(auth, post=True))
    return r.json()["id"]


async def test_route_accepts_a_subset(tmp_path: Path) -> None:
    client, auth, store = await _client(tmp_path)
    pid = await _make_project(client, auth)
    r = client.post(f"/api/projects/{pid}/services", json={"services": ["firecrawl"]},
                    headers=_hdr(auth, post=True))
    assert r.status_code == 200 and r.json()["ok"] is True
    assert (await store.get(pid)).settings["services"] == ["firecrawl"]


async def test_route_rejects_a_name_not_globally_enabled(tmp_path: Path) -> None:
    client, auth, store = await _client(tmp_path)
    pid = await _make_project(client, auth)
    # "searxng" is NOT in the global enabled set ⇒ a project cannot add it (no widening).
    r = client.post(f"/api/projects/{pid}/services", json={"services": ["firecrawl", "searxng"]},
                    headers=_hdr(auth, post=True))
    assert r.status_code == 400 and "cannot widen" in r.json()["message"]
    assert "services" not in (await store.get(pid)).settings  # nothing persisted


async def test_route_clears_narrowing_with_null(tmp_path: Path) -> None:
    client, auth, store = await _client(tmp_path)
    pid = await _make_project(client, auth)
    post = _hdr(auth, post=True)
    client.post(f"/api/projects/{pid}/services", json={"services": ["exa"]}, headers=post)
    r = client.post(f"/api/projects/{pid}/services", json={"services": None}, headers=post)
    assert r.status_code == 200 and r.json()["services"] is None
    assert "services" not in (await store.get(pid)).settings  # back to the full global set


async def test_route_rejects_non_list(tmp_path: Path) -> None:
    client, auth, _ = await _client(tmp_path)
    pid = await _make_project(client, auth)
    r = client.post(f"/api/projects/{pid}/services", json={"services": "firecrawl"},
                    headers=_hdr(auth, post=True))
    assert r.status_code == 400


# --- merge-safe store write (never clobbers sibling settings) ---------------


async def test_set_services_is_merge_safe(tmp_path: Path) -> None:
    db = await connect(tmp_path / "p.db")
    _OPEN.append(db)
    store = ProjectStore(db, asyncio.Lock())
    pid = await store.create(name="P")
    await store.set_label(pid, "backend")
    await store.set_services(pid, ["firecrawl"])
    p = await store.get(pid)
    assert p.settings["label"] == "backend" and p.settings["services"] == ["firecrawl"]
    await store.set_services(pid, None)  # clearing services keeps the label
    p2 = await store.get(pid)
    assert p2.settings["label"] == "backend" and "services" not in p2.settings


# --- run-time enforcement: a narrowed-out tool refuses ----------------------


def test_narrowed_out_flag() -> None:
    def ctx_with(services):
        pc = ProjectContext(project_id=1, name="P", repos=(), system_extra="", services=services)
        return ExaSearchTool(ToolContext(project=lambda: pc))

    assert ctx_with(("firecrawl",))._narrowed_out() is True  # exa not in the subset
    assert ctx_with(("exa", "firecrawl"))._narrowed_out() is False  # exa is in it
    assert ctx_with(None)._narrowed_out() is False  # no narrowing ⇒ full set
    assert ExaSearchTool(ToolContext(project=None))._narrowed_out() is False  # no project layer


async def test_run_refuses_and_sends_nothing_when_narrowed_out(tmp_path: Path) -> None:
    cfgdir = tmp_path / "config"
    cfgdir.mkdir()
    (cfgdir / "pricing.yaml").write_text(
        "schema_version: t\nmodels:\n  anthropic:\n    claude-opus-4-8: {input: 5, output: 25}\n"
        "services:\n  exa: {unit: search, usd_per_unit: 0.005}\n",
        encoding="utf-8",
    )
    cfg = load_config(root=tmp_path, env_file=None)
    cfg.services.enabled = ["exa", "firecrawl"]
    cfg.secrets = cfg.secrets.model_copy(update={"exa_api_key": "k"})
    pc = ProjectContext(project_id=1, name="P", repos=(), system_extra="", services=("firecrawl",))
    sent: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        sent["hit"] = True
        return httpx.Response(200, json={"results": []})

    ExaSearchTool.transport = httpx.MockTransport(handler)
    tool = ExaSearchTool(ToolContext(config=cfg, project=lambda: pc))
    out = await tool.run(tool.Params(query="q", max_results=3))
    assert out.is_error and "not enabled for the active project" in out.content
    assert "hit" not in sent  # narrowed out ⇒ the request was never sent
