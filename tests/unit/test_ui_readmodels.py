"""Read models + the safety pins (Phase 8, Task 5).

Screens are views over existing services (no new storage, no new authority). The
load-bearing pins here: the **route-closed-set** (the only mutations the UI exposes are the
enumerated human-authority ops) and the **secret-absence sweep** (no launch token, cookie
value, API key, or env value ever appears in any response — Hub reports presence booleans
only). DB-backed models run against a temp SQLite + real stores; Hub/Lab are keyless.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

from jarvis.config import load_config
from jarvis.memory.store import MemoryStore
from jarvis.persistence.db import connect
from jarvis.scheduler.service import TaskService
from jarvis.scheduler.store import TaskStore
from jarvis.ui.auth import SESSION_COOKIE, AuthManager
from jarvis.ui.readmodels import (
    UiServices,
    hub_status,
    lab_overview,
    list_memories,
    list_tasks,
)
from jarvis.ui.server import create_app

MODEL = "voyage-3-large"


# --- Hub: presence booleans only (no secret values) -------------------------


def test_hub_reports_presence_booleans_only(tmp_path: Path) -> None:
    cfg = load_config(root=tmp_path, env_file=None)
    # Pin both explicitly (the process env may carry a real key) so present/absent is
    # deterministic regardless of the machine's environment.
    cfg.secrets = cfg.secrets.model_copy(
        update={"anthropic_api_key": "SECRET-CANARY-XYZ", "openai_api_key": ""}
    )
    status = hub_status(cfg)
    assert status["providers"]["anthropic"] is True  # present ⇒ True
    assert status["providers"]["openai"] is False  # absent ⇒ False
    assert status["mcp"]["connected"] is False  # honest placeholder
    # the actual key value must NOT appear anywhere in the serialized status
    assert "SECRET-CANARY-XYZ" not in str(status)


# --- Lab: view over history + baselines (keyless, seeded files) --------------


def test_lab_overview_reads_history_and_baselines(tmp_path: Path) -> None:
    cfg = load_config(root=tmp_path, env_file=None)
    evals = cfg.data_dir / "evals"
    evals.mkdir(parents=True)
    (evals / "history.jsonl").write_text(
        '{"git_rev":"abc","verdict":"PASS"}\n{"git_rev":"def","verdict":"PASS"}\n',
        encoding="utf-8",
    )
    baselines = tmp_path / "b.yaml"
    baselines.write_text("schema_version: 1\n", encoding="utf-8")
    lab = lab_overview(cfg, baselines_path=baselines)
    assert lab["gate_runs"] == 2 and lab["history"][-1]["git_rev"] == "def"
    assert "schema_version" in lab["baselines"]


# --- DB-backed read models + mutations --------------------------------------


async def test_list_and_forget_memories(tmp_path: Path) -> None:
    store = MemoryStore(await connect(tmp_path / "m.db"))
    mid = await store.add(
        type="preference",
        content="likes concise answers",
        embedding=[0.1, 0.2, 0.3],
        embedding_model=MODEL,
        source="user",
    )
    memory = SimpleNamespace(store=store)  # list_memories only touches .store
    rows = await list_memories(memory)
    assert len(rows) == 1 and rows[0]["content"] == "likes concise answers"
    assert "embedding" not in rows[0]  # vector never shipped
    assert await store.forget(mid) is True
    assert await list_memories(memory) == []  # gone from the live view


async def test_list_tasks(tmp_path: Path) -> None:
    from jarvis.config import SchedulerConfig

    store = TaskStore(await connect(tmp_path / "t.db"))
    svc = TaskService(store, SchedulerConfig())
    await store.add(
        kind="reminder",
        title="stretch",
        payload="stretch",
        schedule_kind="once",
        schedule_spec="2030-01-01T00:00:00+00:00",
        timezone="UTC",
        next_run_at="2030-01-01T00:00:00+00:00",
        created_by="user",
    )
    rows = await list_tasks(svc)
    assert len(rows) == 1 and rows[0]["title"] == "stretch"


# --- routes: 503 without services, and the closed mutation set --------------


def _client(tmp_path: Path, *, services=None):
    cfg = load_config(root=tmp_path, env_file=None)
    auth = AuthManager(token="tok")
    app = create_app(cfg, auth=auth, services=services)
    return TestClient(app, base_url="http://127.0.0.1"), app, auth


def _auth(auth: AuthManager, **extra) -> dict[str, str]:
    return {"cookie": f"{SESSION_COOKIE}={auth.mint_session()}", **extra}


def test_service_routes_503_without_services(tmp_path: Path) -> None:
    client, _app_, auth = _client(tmp_path, services=UiServices())
    for path in ("/api/memory", "/api/tasks", "/api/vault", "/api/agents"):
        assert client.get(path, headers=_auth(auth)).status_code == 503, path


def test_hub_and_lab_always_available(tmp_path: Path) -> None:
    client, _app_, auth = _client(tmp_path)
    assert client.get("/api/hub", headers=_auth(auth)).status_code == 200
    assert client.get("/api/lab", headers=_auth(auth)).status_code == 200


def test_mutation_route_closed_set(tmp_path: Path) -> None:
    # THE pin: the ONLY state-changing routes are the enumerated human-authority ops. A new
    # route that reaches a tool/executor directly, or any mutation outside this list, fails.
    _client_, app, _auth_ = _client(tmp_path)
    mutating = {
        (method, route.path)
        for route in app.routes
        for method in getattr(route, "methods", set()) or set()
        if method in {"POST", "PUT", "PATCH", "DELETE"}
    }
    assert mutating == {
        ("POST", "/api/approvals/{decision_id}/resolve"),
        ("POST", "/api/turn"),
        ("POST", "/api/turn/cancel"),
        ("POST", "/api/runner/pause"),
        ("POST", "/api/runner/resume"),
        ("POST", "/api/vault/sources/{source_id}/approve"),
        ("POST", "/api/vault/sources/{source_id}/reject"),
        ("POST", "/api/tasks/{task_id}/cancel"),
        ("POST", "/api/memory/{memory_id}/forget"),
        ("POST", "/api/voice/listen"),
        ("POST", "/api/voice/meeting"),
    }


# --- the full secret-absence sweep -----------------------------------------


def test_no_secret_crosses_the_wire_on_any_get(tmp_path: Path) -> None:
    # Seed distinctive canaries so absence is meaningful, not vacuous.
    cfg = load_config(root=tmp_path, env_file=None)
    cfg.secrets = cfg.secrets.model_copy(
        update={
            "anthropic_api_key": "SECRET-CANARY-ANTHROPIC",
            "openai_api_key": "SECRET-CANARY-OPENAI",
            "voyage_api_key": "SECRET-CANARY-VOYAGE",
            "tavily_api_key": "SECRET-CANARY-TAVILY",
            "elevenlabs_api_key": "SECRET-CANARY-ELEVEN",
        }
    )
    auth = AuthManager(token="SECRET-CANARY-TOKEN")
    app = create_app(cfg, auth=auth)
    client = TestClient(app, base_url="http://127.0.0.1")
    sid = auth.mint_session()
    needles = [
        "SECRET-CANARY-ANTHROPIC",
        "SECRET-CANARY-OPENAI",
        "SECRET-CANARY-VOYAGE",
        "SECRET-CANARY-TAVILY",
        "SECRET-CANARY-ELEVEN",
        "SECRET-CANARY-TOKEN",
        sid,
    ]
    # Walk every registered GET route (skip parameterized ones needing an id) + the core set.
    get_paths = {
        route.path
        for route in app.routes
        if "GET" in (getattr(route, "methods", set()) or set()) and "{" not in route.path
    }
    for path in sorted(get_paths):
        r = client.get(path, headers={"cookie": f"{SESSION_COOKIE}={sid}"})
        blob = r.text + "\n" + "\n".join(f"{k}: {v}" for k, v in r.headers.items())
        for needle in needles:
            assert needle not in blob, f"{needle!r} leaked on GET {path}"
