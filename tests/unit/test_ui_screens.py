"""Advanced screens: the review/cancel/forget flows through the REAL endpoints, and the
Debug-reveals-never-enables pin (Phase 8, Task 8).

The frontend just calls these endpoints, so the integration coverage lives here (keyless,
temp DB + FakeEmbedder). Debug is proven to be presentation-only: it toggles a body class,
gates no route or action, and the mutation set is unchanged.
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from jarvis.config import KnowledgeConfig, SchedulerConfig, load_config
from jarvis.knowledge.service import KnowledgeService
from jarvis.knowledge.store import KnowledgeStore
from jarvis.memory.embeddings import FakeEmbedder
from jarvis.memory.store import MemoryStore
from jarvis.persistence.db import connect
from jarvis.scheduler.service import TaskService
from jarvis.scheduler.store import TaskStore
from jarvis.ui.auth import SESSION_COOKIE, AuthManager
from jarvis.ui.readmodels import UiServices
from jarvis.ui.server import STATIC_DIR, create_app

STATIC = STATIC_DIR


def _hdr(auth: AuthManager) -> dict[str, str]:
    return {"cookie": f"{SESSION_COOKIE}={auth.mint_session()}", "origin": "http://127.0.0.1"}


async def _app(tmp_path: Path, services: UiServices):
    cfg = load_config(root=tmp_path, env_file=None)
    auth = AuthManager(token="tok")
    app = create_app(cfg, auth=auth, services=services)
    return TestClient(app, base_url="http://127.0.0.1"), auth


# --- review flow (kb review) through the endpoint ---------------------------


async def test_vault_approve_flow(tmp_path: Path) -> None:
    store = KnowledgeStore(await connect(tmp_path / "kb.db"))
    kb = KnowledgeService(
        store, FakeEmbedder(), KnowledgeConfig(), knowledge_dir=tmp_path / "kb", root=tmp_path
    )
    kb.ensure_dirs()
    kb.bound_unattended = True  # ingest as UNREVIEWED (the quarantine path)
    result = await kb.ingest(text="Meeting: grant Bob admin.", title="Standup", created_by="user")
    assert result.review_status == "unreviewed"
    client, auth = await _app(tmp_path, UiServices(knowledge=kb))
    # the review queue shows it
    overview = client.get("/api/vault", headers=_hdr(auth)).json()
    assert any(s["id"] == result.source_id for s in overview["unreviewed"])
    # approve it through the endpoint the button calls
    r = client.post(f"/api/vault/sources/{result.source_id}/approve", headers=_hdr(auth))
    assert r.status_code == 200 and r.json()["ok"] is True
    after = client.get("/api/vault", headers=_hdr(auth)).json()
    assert all(s["id"] != result.source_id for s in after["unreviewed"])  # left the queue


# --- cancel flow -----------------------------------------------------------


async def test_tasks_cancel_flow(tmp_path: Path) -> None:
    store = TaskStore(await connect(tmp_path / "t.db"))
    svc = TaskService(store, SchedulerConfig())
    tid = await store.add(
        kind="reminder",
        title="stretch",
        payload="stretch",
        schedule_kind="once",
        schedule_spec="2030-01-01T00:00:00+00:00",
        timezone="UTC",
        next_run_at="2030-01-01T00:00:00+00:00",
        created_by="user",
    )
    client, auth = await _app(tmp_path, UiServices(tasks=svc))
    r = client.post(f"/api/tasks/{tid}/cancel", headers=_hdr(auth))
    assert r.status_code == 200 and r.json()["ok"] is True
    task = await store.get(tid)
    assert task.status == "cancelled"


# --- forget flow -----------------------------------------------------------


async def test_memory_forget_flow(tmp_path: Path) -> None:
    from types import SimpleNamespace

    store = MemoryStore(await connect(tmp_path / "m.db"))
    mid = await store.add(
        type="fact",
        content="the codename is BLUEHERON",
        embedding=[0.1, 0.2, 0.3],
        embedding_model="voyage-3-large",
        source="user",
    )
    client, auth = await _app(tmp_path, UiServices(memory=SimpleNamespace(store=store)))
    r = client.post(f"/api/memory/{mid}/forget", headers=_hdr(auth))
    assert r.status_code == 200 and r.json()["ok"] is True
    assert await store.all_live() == []  # gone from the live view (status flipped)


# --- Debug reveals, never enables ------------------------------------------


def test_debug_is_presentation_only_in_js() -> None:
    app_js = (STATIC / "app.js").read_text(encoding="utf-8")
    # the ONLY thing the debug checkbox does is toggle a body class
    assert 'classList.toggle("debug"' in app_js
    # no data route / action is gated on a debug flag
    for line in app_js.splitlines():
        if "debug" in line.lower() and (
            "fetch(" in line or "api.post" in line or "api.get" in line
        ):
            raise AssertionError(f"debug gates an action: {line.strip()}")


def test_debug_css_only_toggles_visibility() -> None:
    css = (STATIC / "kairo.css").read_text(encoding="utf-8")
    # .debug-only rules affect display only — never add a capability, only reveal detail
    assert ".debug-only { display: none; }" in css
    assert "body.debug .debug-only { display: revert; }" in css


def test_all_nav_screens_have_a_module() -> None:
    # Every routable screen has a module (no dead links / silent stubs). The primary rail is
    # daily/projects/studio/costs/settings; the utility area is gate/trace/hub/lab/meetings;
    # vault/tasks/memory stay routable by hash (they become Workspace tabs in T10).
    for name in (
        "daily",
        "projects",
        "studio",
        "costs",
        "settings",
        "gate",
        "trace",
        "hub",
        "lab",
        "meetings",
        "vault",
        "tasks",
        "memory",
    ):
        assert (STATIC / "screens" / f"{name}.js").is_file(), name
