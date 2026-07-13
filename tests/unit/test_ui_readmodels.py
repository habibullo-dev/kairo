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


async def test_lab_overview_reads_history_and_baselines(tmp_path: Path) -> None:
    cfg = load_config(root=tmp_path, env_file=None)
    evals = cfg.data_dir / "evals"
    evals.mkdir(parents=True)
    (evals / "history.jsonl").write_text(
        '{"git_rev":"abc","verdict":"PASS"}\n{"git_rev":"def","verdict":"PASS"}\n',
        encoding="utf-8",
    )
    baselines = tmp_path / "b.yaml"
    baselines.write_text("schema_version: 1\n", encoding="utf-8")
    lab = await lab_overview(cfg, baselines_path=baselines)
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
        # A live local Workspace may review one hash-pinned parked task continuation. The route
        # only delegates to the host-owned resume seam after workspace + nonce revalidation.
        ("POST", "/api/parked-task-approvals/{run_id}/resolve"),
        ("POST", "/api/turn"),
        ("POST", "/api/turn/cancel"),
        ("POST", "/api/runner/pause"),
        ("POST", "/api/runner/resume"),
        ("POST", "/api/vault/sources/{source_id}/approve"),
        ("POST", "/api/vault/sources/{source_id}/reject"),
        ("POST", "/api/vault/ingest"),  # Phase 9: human-initiated ingest (same gate floor)
        # Chat attachment is the same explicit human local-file ingest, not a tool/executor path.
        ("POST", "/api/chat/attachments"),
        # Explicit local lifecycle action: reject one displayed project-folder import; no tool,
        # executor, external write, or hard deletion.
        ("POST", "/api/chat/knowledge/detach"),
        ("POST", "/api/digest/run"),  # Phase 9: run the Daily Digest now
        ("POST", "/api/tasks/{task_id}/cancel"),
        ("POST", "/api/memory/{memory_id}/forget"),
        ("POST", "/api/sessions/{session_id}/pin"),  # Phase 10: pin/unpin a chat
        ("POST", "/api/sessions/{session_id}/resume"),  # Phase 10: resume a chat into the UI
        ("POST", "/api/projects"),  # Phase 10: create a project
        ("POST", "/api/projects/{project_id}/update"),  # Phase 10: edit a project
        ("POST", "/api/projects/{project_id}/archive"),  # Phase 10: archive a project
        ("POST", "/api/projects/select"),  # Phase 10: set the active-project scope
        ("POST", "/api/mode"),  # Phase 10: set the interactive run mode
        ("POST", "/api/memory/remember"),  # Phase 10: human-authority remember (promote target)
        ("POST", "/api/tasks/create"),  # Phase 10: human-authority task create (promote target)
        ("POST", "/api/orchestration/run"),  # Phase 10B: launch a team+workflow run
        ("POST", "/api/orchestration/{run_id}/cancel"),  # Phase 10B: cancel the in-flight run
        ("POST", "/api/voice/listen"),
        ("POST", "/api/voice/meeting"),
        # Phase 15.5 full browser voice: a browser-captured utterance runs a voice turn through the
        # UNCHANGED VoiceApprover (screen stays the only approval surface — no new authority); tts
        # synthesizes the SAFE caption for playback (stateless). Same voice floor as listen/meeting.
        ("POST", "/api/voice/utterance"),
        ("POST", "/api/voice/tts"),
        ("POST", "/api/projects/{project_id}/pin"),  # Phase 11: pin/unpin a project card
        ("POST", "/api/projects/{project_id}/label"),  # Phase 11: set a project's category label
        ("POST", "/api/projects/{project_id}/services"),  # Phase 13: narrow-only service selection
        ("POST", "/api/artifacts/{artifact_id}/pin"),  # Phase 11: pin/unpin an artifact
        ("POST", "/api/artifacts/{artifact_id}/label"),  # Phase 11: set an artifact's labels
        # Phase 12: the outward-write approval queue — human-only. approve EXECUTES the stored
        # intent (the only path that performs a connector write); reject/undo close the loop.
        ("POST", "/api/intents/{intent_id}/approve"),
        ("POST", "/api/intents/{intent_id}/reject"),
        ("POST", "/api/intents/{intent_id}/undo"),
        # Phase 15: the graph suggestion review ops (the ONLY new authority — the Vault
        # approve/reject pattern; quarantined proposal -> durable memory/asserted node|edge).
        ("POST", "/api/graph/suggestions/{suggestion_id}/approve"),
        ("POST", "/api/graph/suggestions/{suggestion_id}/reject"),
        # Phase 15.5: human-authority UI-state ops (the sessions/pin mold — no tool, no executor,
        # no Gate reach). Model selection is Anthropic-only; new/rename/archive are chat metadata.
        ("POST", "/api/model"),
        ("POST", "/api/effort"),  # Phase 15.5: per-model output-config effort (cost control)
        ("POST", "/api/sessions/new"),
        ("POST", "/api/sessions/{session_id}/rename"),
        ("POST", "/api/sessions/{session_id}/archive"),
        # Phase 16: the ONE new attention route — a metadata-only state flip (done/dismiss/snooze)
        # on a durable attention row. It grants NO authority: a proposal's accept is the human on
        # its source's EXISTING gated route. All other queue items keep their own approve/reject.
        ("POST", "/api/attention/{item_id}/resolve"),
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
            "google_client_secret": "SECRET-CANARY-GOOGLE-SECRET",
            "telegram_bot_token": "SECRET-CANARY-TELEGRAM",
            "telegram_chat_id": "SECRET-CANARY-CHATID",
            "kakao_rest_api_key": "SECRET-CANARY-KAKAO",
            "kakao_client_secret": "SECRET-CANARY-KAKAO-SECRET",
            "firecrawl_api_key": "SECRET-CANARY-FIRECRAWL",  # Phase 13 research-service keys
            "exa_api_key": "SECRET-CANARY-EXA",
        }
    )
    auth = AuthManager(token="SECRET-CANARY-TOKEN")
    app = create_app(cfg, auth=auth)

    # Phase 9: seed a real connector token file on disk and expose it through the connector
    # status path — Hub/Daily must report scopes/expiry, never the token itself.
    from jarvis.connectors.base import ConnectorRegistry
    from jarvis.connectors.google import google_provider
    from jarvis.connectors.google.client import GoogleClient
    from jarvis.connectors.tokens import TokenState, TokenStore, write_token_state

    tokdir = cfg.data_dir / "connectors"
    write_token_state(
        tokdir / "google_token.json",
        TokenState(
            provider="google",
            access_token="SECRET-CANARY-ACCESS",
            refresh_token="SECRET-CANARY-REFRESH",
            expires_at="2030-01-01T00:00:00+00:00",
            obtained_at="2026-01-01T00:00:00+00:00",
            scopes=["calendar.readonly"],
        ),
    )
    store = TokenStore(
        tokdir / "google_token.json",
        provider=google_provider(),
        client_id="cid",
        client_secret="SECRET-CANARY-GOOGLE-SECRET",
    )
    app.state.services.connectors = ConnectorRegistry(google=GoogleClient(store))

    client = TestClient(app, base_url="http://127.0.0.1")
    sid = auth.mint_session()
    needles = [
        "SECRET-CANARY-ANTHROPIC",
        "SECRET-CANARY-OPENAI",
        "SECRET-CANARY-VOYAGE",
        "SECRET-CANARY-TAVILY",
        "SECRET-CANARY-ELEVEN",
        "SECRET-CANARY-GOOGLE-SECRET",
        "SECRET-CANARY-TELEGRAM",
        "SECRET-CANARY-KAKAO",
        "SECRET-CANARY-ACCESS",  # the token file's access token — never on the wire
        "SECRET-CANARY-REFRESH",  # the refresh token — never on the wire
        "SECRET-CANARY-TOKEN",
        "SECRET-CANARY-FIRECRAWL",  # Phase 13: research-service keys never on the wire
        "SECRET-CANARY-EXA",
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
