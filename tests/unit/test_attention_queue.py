"""Phase 16 Task 2: the unified attention queue read model + the resolve route.

Pins: the queue UNIONS live Gate ASKs + write-intents + graph suggestions + durable attention
rows, each carrying source+ref pointing AT its own route (never duplicated authority); urgent
sorts first; counts by kind; project scoping. The resolve route flips ONLY attention rows'
metadata (done/dismiss/snooze) and adds no authority. Keyless."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from jarvis.attention import AttentionKind, AttentionPriority, AttentionStore
from jarvis.attention.readmodel import attention_queue
from jarvis.config import load_config
from jarvis.persistence.db import connect
from jarvis.ui.auth import SESSION_COOKIE, AuthManager
from jarvis.ui.readmodels import UiServices
from jarvis.ui.server import create_app

_OPEN: list = []


@pytest.fixture(autouse=True)
async def _close():
    yield
    while _OPEN:
        await _OPEN.pop().close()


async def _store(tmp_path: Path) -> AttentionStore:
    db = await connect(tmp_path / "a.db")
    _OPEN.append(db)
    lock = asyncio.Lock()
    from jarvis.projects import ProjectStore

    projects = ProjectStore(db, lock)
    for name in ("One", "Two"):
        await projects.create(name=name)
    return AttentionStore(db, lock)


# --- fakes for the other sources (each exposes only what the read model calls) ---
class _FakeIntents:
    def __init__(self, rows): self._rows = rows
    async def list(self, **_kw): return self._rows


class _FakeGraph:
    def __init__(self, rows): self._rows = rows
    async def list_suggestions(self, **_kw): return self._rows


class _FakeApprovals:
    def __init__(self, rows): self._rows = rows
    def pending(self): return self._rows


def _intent(iid, summary="Send draft", priority="normal", project_id=None):
    return SimpleNamespace(id=iid, summary=summary, priority=priority, project_id=project_id,
                           created_at="2026-07-10T01:00:00+00:00", preview={"body": "x"},
                           kind="gmail_draft_create")


def _sugg(sid, kind="memory", project_id=1):
    return SimpleNamespace(id=sid, kind=kind, payload={"content": "habit"}, project_id=project_id,
                           created_at="2026-07-10T02:00:00+00:00", trust_class="model_generated")


def _ask(did="d1", tool="send_notification"):
    return SimpleNamespace(decision_id=did, call=SimpleNamespace(name=tool),
                           to_public=lambda: {"tool": tool, "input": {"text": "hi"}})


async def test_queue_unions_all_sources_with_pointers(tmp_path: Path) -> None:
    s = await _store(tmp_path)
    await s.create(kind=AttentionKind.PROPOSAL, source="dreaming", title="Nightly review")
    q = await attention_queue(
        attention=s,
        intents=_FakeIntents([_intent(7)]),
        graph=_FakeGraph([_sugg(3)]),
        approvals=_FakeApprovals([_ask()]),
    )
    by_source = {i["source"]: i for i in q["items"]}
    assert set(by_source) == {"gate", "intent", "graph_suggestion", "attention"}
    assert by_source["intent"]["ref"] == "7"  # points AT the intent's own route
    assert by_source["graph_suggestion"]["ref"] == "3"
    assert by_source["gate"]["priority"] == "urgent"  # a blocking ASK leads
    assert q["items"][0]["source"] == "gate"  # urgent sorts first
    assert q["total"] == 4


async def test_counts_by_kind(tmp_path: Path) -> None:
    s = await _store(tmp_path)
    await s.create(kind=AttentionKind.PROPOSAL, source="dreaming", title="a")
    await s.create(kind=AttentionKind.ALERT, source="system", title="b",
                   priority=AttentionPriority.URGENT)
    q = await attention_queue(attention=s, intents=_FakeIntents([_intent(1)]))
    assert q["counts"] == {"proposal": 1, "alert": 1, "approval": 1}


async def test_queue_degrades_when_sources_absent(tmp_path: Path) -> None:
    s = await _store(tmp_path)
    await s.create(kind=AttentionKind.PROPOSAL, source="dreaming", title="only-attention")
    q = await attention_queue(attention=s)  # no intents/graph/approvals ⇒ just attention rows
    assert q["total"] == 1 and q["items"][0]["source"] == "attention"


async def test_project_scoping(tmp_path: Path) -> None:
    s = await _store(tmp_path)
    await s.create(kind=AttentionKind.PROPOSAL, source="dreaming", title="p1", project_id=1)
    await s.create(kind=AttentionKind.PROPOSAL, source="dreaming", title="p2", project_id=2)
    q1 = await attention_queue(attention=s, project_id=1)
    assert q1["total"] == 1 and q1["items"][0]["project_id"] == 1


# --- the resolve route (metadata-only; no authority) ---
def _hdr(auth: AuthManager, *, post: bool = False) -> dict[str, str]:
    h = {"cookie": f"{SESSION_COOKIE}={auth.mint_session()}"}
    if post:
        h["origin"] = "http://127.0.0.1"
    return h


async def _client(tmp_path: Path):
    cfg = load_config(root=tmp_path, env_file=None)
    auth = AuthManager(token="tok")
    app = create_app(cfg, auth=auth)
    store = await _store(tmp_path)
    app.state.services = UiServices(attention=store)
    return TestClient(app, base_url="http://127.0.0.1"), auth, store


async def test_resolve_route_flips_state(tmp_path: Path) -> None:
    client, auth, store = await _client(tmp_path)
    iid = await store.create(kind=AttentionKind.PROPOSAL, source="dreaming", title="x")
    r = client.post(f"/api/attention/{iid}/resolve", json={"action": "dismiss"},
                    headers=_hdr(auth, post=True))
    assert r.status_code == 200 and r.json()["state"] == "dismissed"
    assert (await store.get(iid)).state.value == "dismissed"


async def test_resolve_route_rejects_bad_action_and_missing(tmp_path: Path) -> None:
    client, auth, store = await _client(tmp_path)
    iid = await store.create(kind=AttentionKind.PROPOSAL, source="dreaming", title="x")
    assert client.post(f"/api/attention/{iid}/resolve", json={"action": "nuke"},
                       headers=_hdr(auth, post=True)).status_code == 400
    assert client.post("/api/attention/9999/resolve", json={"action": "done"},
                       headers=_hdr(auth, post=True)).status_code == 404
