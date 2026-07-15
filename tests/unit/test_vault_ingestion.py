"""Vault ingestion (Phase 9 Task 9): ingest_folder, review preview, POST /api/vault/ingest.

Keyless — FakeEmbedder, .md passthrough, no network. The pins: bulk folder ingest skips
secrets + refuses symlinks + dedupes; the review queue ships a content preview; the UI ingest
route runs the SAME sensitive-path gate floor as the tool (DENY ⇒ 403)."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from starlette.datastructures import UploadFile

from kira.attention import AttentionStore
from kira.config import KnowledgeConfig, load_config
from kira.core.execution import ExecutionContext
from kira.graph import GraphStore
from kira.graph.builder import rebuild as rebuild_graph
from kira.knowledge.service import KnowledgeService
from kira.knowledge.store import KnowledgeStore
from kira.memory.embeddings import FakeEmbedder
from kira.persistence.db import connect
from kira.persistence.sessions import SessionStore
from kira.projects import ProjectStore
from kira.projects.service import ProjectService
from kira.ui.auth import SESSION_COOKIE, AuthManager
from kira.ui.readmodels import UiServices, vault_overview
from kira.ui.server import WORKSPACE_HEADER, create_app

_OPEN: list = []


@pytest.fixture(autouse=True)
async def _close():
    yield
    while _OPEN:
        await _OPEN.pop().close()


async def _service(tmp_path: Path) -> KnowledgeService:
    store = KnowledgeStore(await connect(tmp_path / "kb.db"))
    _OPEN.append(store.db)
    svc = KnowledgeService(
        store,
        FakeEmbedder(),
        KnowledgeConfig(),
        knowledge_dir=tmp_path / "knowledge",
        root=tmp_path,
    )
    svc.ensure_dirs()
    return svc


# --- ingest_folder ---------------------------------------------------------


async def test_ingest_folder_ingests_md_and_skips_secrets(tmp_path: Path) -> None:
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "a.md").write_text("# A\n\ncontent a", encoding="utf-8")
    (docs / "b.md").write_text("# B\n\ncontent b", encoding="utf-8")
    (docs / ".env").write_text("SECRET=1", encoding="utf-8")  # no ingestible suffix → ignored
    (docs / "pic.png").write_bytes(b"\x89PNG")  # non-ingestible extension → ignored
    # An ingestible file at a SENSITIVE path exercises the sensitive-skip branch specifically.
    sens = docs / "data" / "connectors"
    sens.mkdir(parents=True)
    (sens / "token.md").write_text("# secret\n\nnope", encoding="utf-8")
    report = await (await _service(tmp_path)).ingest_folder(docs)
    assert len(report.ingested) == 2  # only a.md + b.md
    joined = " ".join(report.ingested)
    assert ".env" not in joined and "token.md" not in joined  # secrets never ingested
    assert any("sensitive" in reason for _p, reason in report.skipped)  # token.md skipped


async def test_ingest_folder_dedupes_on_second_run(tmp_path: Path) -> None:
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "a.md").write_text("# A\n\nonce", encoding="utf-8")
    svc = await _service(tmp_path)
    first = await svc.ingest_folder(docs)
    assert len(first.ingested) == 1
    second = await svc.ingest_folder(docs)
    assert second.ingested == [] and len(second.duplicates) == 1  # identical bytes are a no-op


@pytest.mark.skipif(sys.platform == "win32", reason="symlink creation needs privilege on Windows")
async def test_ingest_folder_refuses_symlinks(tmp_path: Path) -> None:
    secret = tmp_path / "outside.md"
    secret.write_text("# Outside\n\nprivate", encoding="utf-8")
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "link.md").symlink_to(secret)  # a symlink INTO the folder
    report = await (await _service(tmp_path)).ingest_folder(docs)
    assert report.ingested == []
    assert any("symlink refused" in r for _p, r in report.skipped)


async def test_ingest_folder_missing_dir_reports_failed(tmp_path: Path) -> None:
    report = await (await _service(tmp_path)).ingest_folder(tmp_path / "nope")
    assert report.failed and "not a directory" in report.failed[0][1]


# --- review preview --------------------------------------------------------


async def test_source_markdown_preview_capped(tmp_path: Path) -> None:
    (tmp_path / "big.md").write_text("# Big\n\n" + ("x" * 5000), encoding="utf-8")
    svc = await _service(tmp_path)
    result = await svc.ingest(path="big.md", created_by="user")
    preview = await svc.source_markdown(result.source_id, max_chars=100)
    assert preview is not None and len(preview) <= 120 and "truncated" in preview
    assert await svc.source_markdown(9999) is None  # unknown source


async def test_vault_overview_includes_preview_for_unreviewed(tmp_path: Path) -> None:
    svc = await _service(tmp_path)
    svc.bound_unattended = True  # forces the ingested source into the unreviewed quarantine
    (tmp_path / "q.md").write_text("# Quarantined\n\nreview me", encoding="utf-8")
    await svc.ingest(path="q.md", created_by="user")
    overview = await vault_overview(svc)
    assert len(overview["unreviewed"]) == 1
    assert "review me" in overview["unreviewed"][0]["preview"]  # preview shown for informed review


async def test_vault_review_queue_is_project_scoped_without_narrowing_global_queue(
    tmp_path: Path,
) -> None:
    svc = await _service(tmp_path)
    projects = ProjectStore(svc.store.db, svc.store.lock)
    project_id = await projects.create(name="Scoped review project")
    svc.bound_unattended = True
    await svc.ingest(text="GLOBAL-REVIEW-CANARY", title="global note")
    await svc.ingest(text="PROJECT-REVIEW-CANARY", title="project note", project_id=project_id)

    global_queue = await vault_overview(svc)
    assert {item["title"] for item in global_queue["unreviewed"]} == {"global note", "project note"}
    project_queue = await vault_overview(svc, project_id=project_id)
    assert [item["title"] for item in project_queue["unreviewed"]] == ["project note"]
    assert "GLOBAL-REVIEW-CANARY" not in str(project_queue)


async def test_vault_readiness_is_project_scoped_and_bodies_free(tmp_path: Path) -> None:
    svc = await _service(tmp_path)
    projects = ProjectStore(svc.store.db, svc.store.lock)
    project_a = await projects.create(name="A")
    project_b = await projects.create(name="B")
    await svc.ingest(
        text="ALPHA-BODY-CANARY\nfrom .core import runner",
        title="repo/src/app.py",
        project_id=project_a,
    )
    await svc.ingest(
        text="def runner(): pass",
        title="repo/src/core.py",
        project_id=project_a,
    )
    await svc.ingest(
        text="PROJECT-B-BODY-CANARY",
        title="other/private.py",
        project_id=project_b,
    )
    graph = GraphStore(svc.store.db, svc.store.lock)
    await rebuild_graph(graph)

    overview = await vault_overview(svc, project_id=project_a, graph=graph)
    assert overview["stats"] == {"sources": 2, "unreviewed": 0, "chunks": 2}
    readiness = overview["project_readiness"]
    assert readiness == {
        "project_id": project_a,
        "sources": 2,
        "indexed_chunks": 2,
        "graph_available": True,
        "folder_links": 4,
        "import_links": 1,
        "ready": True,
        "detail": (
            "Relevant sections and verified local dependencies are available to project chat."
        ),
    }
    rendered = str(readiness)
    assert "ALPHA-BODY-CANARY" not in rendered
    assert "PROJECT-B-BODY-CANARY" not in rendered


async def test_vault_readiness_keeps_unreviewed_project_files_unusable(tmp_path: Path) -> None:
    svc = await _service(tmp_path)
    projects = ProjectStore(svc.store.db, svc.store.lock)
    project_id = await projects.create(name="Quarantined project")
    svc.bound_unattended = True
    await svc.ingest(
        text="AWAITING-REVIEW-CANARY",
        title="repo/private.py",
        project_id=project_id,
    )

    readiness = (await vault_overview(svc, project_id=project_id))["project_readiness"]
    assert readiness["sources"] == 1
    assert readiness["indexed_chunks"] == 0
    assert readiness["ready"] is False
    assert "awaiting review" in readiness["detail"]
    assert "AWAITING-REVIEW-CANARY" not in str(readiness)


async def test_vault_readiness_excludes_imports_through_unreviewed_sources(tmp_path: Path) -> None:
    svc = await _service(tmp_path)
    projects = ProjectStore(svc.store.db, svc.store.lock)
    project_id = await projects.create(name="Mixed review project")
    await svc.ingest(
        text="reviewed app\nfrom .core import runner",
        title="repo/src/app.py",
        project_id=project_id,
    )
    svc.bound_unattended = True
    await svc.ingest(
        text="UNREVIEWED-IMPORT-CANARY\ndef runner(): pass",
        title="repo/src/core.py",
        project_id=project_id,
    )
    graph = GraphStore(svc.store.db, svc.store.lock)
    await rebuild_graph(graph)

    readiness = (await vault_overview(svc, project_id=project_id, graph=graph))["project_readiness"]
    assert readiness["sources"] == 2
    assert readiness["indexed_chunks"] == 1
    assert readiness["import_links"] == 0
    assert "no local imports were resolved yet" in readiness["detail"]
    assert "UNREVIEWED-IMPORT-CANARY" not in str(readiness)


async def test_vault_readiness_ignores_asserted_import_edges(tmp_path: Path) -> None:
    svc = await _service(tmp_path)
    projects = ProjectStore(svc.store.db, svc.store.lock)
    project_id = await projects.create(name="Asserted graph project")
    app = await svc.ingest(text="reviewed app", title="repo/src/app.py", project_id=project_id)
    core = await svc.ingest(text="reviewed core", title="repo/src/core.py", project_id=project_id)
    graph = GraphStore(svc.store.db, svc.store.lock)
    await graph.upsert_edge(
        src_kind="source",
        src_id=str(app.source_id),
        dst_kind="source",
        dst_id=str(core.source_id),
        edge_kind="imports",
        origin="asserted",
        trust_class="reviewed",
        created_by="user",
        created_at="2026-01-01T00:00:00+00:00",
        project_id=project_id,
    )

    readiness = (await vault_overview(svc, project_id=project_id, graph=graph))["project_readiness"]
    assert readiness["import_links"] == 0
    assert "no local imports were resolved yet" in readiness["detail"]


# --- POST /api/vault/ingest ------------------------------------------------


def _app(tmp_path: Path, svc: KnowledgeService):
    auth = AuthManager(token="t")
    app = create_app(load_config(root=tmp_path, env_file=None), auth=auth)
    app.state.services = UiServices(knowledge=svc)
    client = TestClient(app, base_url="http://127.0.0.1")
    return client, auth


def _cookie(auth: AuthManager) -> dict:
    # POSTs are Origin-checked (anti-CSRF) before the session gate — send a loopback Origin.
    return {"cookie": f"{SESSION_COOKIE}={auth.mint_session()}", "origin": "http://127.0.0.1"}


async def test_ingest_route_ingests_a_text_note(tmp_path: Path) -> None:
    svc = await _service(tmp_path)
    client, auth = _app(tmp_path, svc)
    r = client.post(
        "/api/vault/ingest", json={"text": "a note", "title": "Note"}, headers=_cookie(auth)
    )
    assert r.status_code == 200 and r.json()["ok"] is True


async def test_chat_attachment_route_ingests_a_browser_selected_document(tmp_path: Path) -> None:
    svc = await _service(tmp_path)
    client, auth = _app(tmp_path, svc)
    r = client.post(
        "/api/chat/attachments",
        files={"file": ("brief.md", b"# Brief\n\nProject upload canary.", "text/markdown")},
        headers=_cookie(auth),
    )
    assert r.status_code == 200 and r.json()["ok"] is True
    source = await svc.store.get_source(r.json()["source_id"])
    assert source is not None and source.origin == "chat-upload:global:brief.md"
    assert "upload canary" in (svc.knowledge_dir / source.markdown_path).read_text(encoding="utf-8")


async def test_chat_attachment_secret_scan_creates_value_free_attention_alert(
    tmp_path: Path,
) -> None:
    svc = await _service(tmp_path)
    attention = AttentionStore(svc.store.db, svc.store.lock)
    client, auth = _app(tmp_path, svc)
    client.app.state.services = UiServices(knowledge=svc, attention=attention)
    secret = "realvalue123456789"
    response = client.post(
        "/api/chat/attachments",
        files={
            "file": (
                "config.yaml",
                f"api_key: {secret}\nservice: local\n".encode(),
                "text/yaml",
            )
        },
        headers=_cookie(auth),
    )
    body = response.json()
    assert response.status_code == 200 and body["suspected_secret_hits"] == 1
    item = await attention.get(body["secret_alert_id"])
    assert item is not None
    assert item.source == "knowledge_secret_scan" and item.category == "security"
    assert item.payload == {
        "source_id": body["source_id"],
        "file": "config.yaml",
        "hit_count": 1,
        "rules": ["credential_assignment"],
    }
    assert secret not in repr(item)


async def test_folder_upload_finalize_rebuilds_the_project_graph(tmp_path: Path) -> None:
    svc = await _service(tmp_path)
    projects = ProjectStore(svc.store.db, svc.store.lock)
    project_id = await projects.create(name="Folder project")
    project_service = ProjectService(projects)
    await project_service.activate(project_id)
    client, auth = _app(tmp_path, svc)
    client.app.state.projects = project_service
    client.app.state.services = UiServices(
        knowledge=svc,
        graph=GraphStore(svc.store.db, svc.store.lock),
    )
    uploaded = client.post(
        "/api/chat/attachments",
        files={"file": ("main.py", b"print('folder import')", "text/x-python")},
        data={"relative_path": "repo/src/main.py"},
        headers=_cookie(auth),
    )
    assert uploaded.status_code == 200 and uploaded.json()["ok"] is True
    assert "graph_rebuilt" not in uploaded.json()  # only the explicit finalize request rebuilds

    finalized = client.post(
        "/api/chat/attachments", data={"finalize": "true"}, headers=_cookie(auth)
    )
    assert finalized.status_code == 200 and finalized.json() == {
        "ok": True,
        "graph_rebuilt": True,
        "assessment": {"state": "disabled"},
    }
    edges = await client.app.state.services.graph.list_edges(
        project_id=project_id, include_global=False
    )
    assert any(edge.src_kind == "project" and edge.dst_kind == "folder" for edge in edges)
    assert any(edge.src_kind == "folder" and edge.dst_kind == "source" for edge in edges)


@pytest.mark.parametrize(
    ("job_state", "wire_state"),
    [
        ("queued", "queued"),
        ("running", "in_progress"),
        ("published", "ready"),
        ("failed", "failed"),
    ],
)
async def test_folder_finalize_returns_only_closed_assessment_state(
    tmp_path: Path,
    job_state: str,
    wire_state: str,
) -> None:
    svc = await _service(tmp_path)
    projects = ProjectStore(svc.store.db, svc.store.lock)
    project_id = await projects.create(name="Folder project")
    project_service = ProjectService(projects)
    await project_service.activate(project_id)
    client, auth = _app(tmp_path, svc)
    client.app.state.projects = project_service
    client.app.state.services = UiServices(
        knowledge=svc,
        graph=GraphStore(svc.store.db, svc.store.lock),
    )
    client.app.state.config.project_intelligence.enabled = True

    class Coordinator:
        async def enqueue_project(self, received_project_id: int):
            assert received_project_id == project_id
            return SimpleNamespace(enabled=True, state=job_state, secret="must-not-ship")

    client.app.state.project_intelligence = Coordinator()
    await svc.ingest_uploaded(
        "main.py", b"print('folder import')", project_id=project_id, relative_path="repo/main.py"
    )
    finalized = client.post(
        "/api/chat/attachments", data={"finalize": "true"}, headers=_cookie(auth)
    )
    assert finalized.status_code == 200
    assert finalized.json() == {
        "ok": True,
        "graph_rebuilt": True,
        "assessment": {"state": wire_state},
    }


async def test_folder_finalize_keeps_graph_success_when_assessment_enqueue_fails(
    tmp_path: Path,
) -> None:
    svc = await _service(tmp_path)
    projects = ProjectStore(svc.store.db, svc.store.lock)
    project_id = await projects.create(name="Folder project")
    project_service = ProjectService(projects)
    await project_service.activate(project_id)
    client, auth = _app(tmp_path, svc)
    client.app.state.projects = project_service
    client.app.state.services = UiServices(
        knowledge=svc,
        graph=GraphStore(svc.store.db, svc.store.lock),
    )
    client.app.state.config.project_intelligence.enabled = True

    class Coordinator:
        async def enqueue_project(self, _project_id: int):
            raise RuntimeError("provider body must not ship")

    client.app.state.project_intelligence = Coordinator()
    await svc.ingest_uploaded(
        "main.py", b"print('folder import')", project_id=project_id, relative_path="repo/main.py"
    )
    finalized = client.post(
        "/api/chat/attachments", data={"finalize": "true"}, headers=_cookie(auth)
    )
    assert finalized.status_code == 200
    assert finalized.json() == {
        "ok": True,
        "graph_rebuilt": True,
        "assessment": {"state": "unavailable"},
    }
    assert "provider body" not in finalized.text


async def test_project_folder_detach_rejects_only_that_folder_and_rebuilds_graph(
    tmp_path: Path,
) -> None:
    svc = await _service(tmp_path)
    projects = ProjectStore(svc.store.db, svc.store.lock)
    project_id = await projects.create(name="Folder project")
    project_service = ProjectService(projects)
    await project_service.activate(project_id)
    client, auth = _app(tmp_path, svc)
    client.app.state.projects = project_service
    client.app.state.services = UiServices(
        knowledge=svc,
        graph=GraphStore(svc.store.db, svc.store.lock),
    )
    await svc.ingest_uploaded("old.py", b"old", project_id=project_id, relative_path="wrong/old.py")
    retained = await svc.ingest_uploaded(
        "new.py", b"new", project_id=project_id, relative_path="right/new.py"
    )
    await rebuild_graph(client.app.state.services.graph)

    detached = client.post(
        "/api/chat/knowledge/detach", json={"root": "wrong"}, headers=_cookie(auth)
    )
    assert detached.status_code == 200 and detached.json() == {
        "ok": True,
        "detached_sources": 1,
        "cleared_chunks": 1,
    }
    assert (await svc.store.get_source(retained.source_id)).status == "live"
    assert await client.app.state.services.graph.list_edges(
        project_id=project_id, include_global=False
    )


async def test_chat_attachment_refuses_unknown_types_without_returning_parser_details(
    tmp_path: Path,
) -> None:
    svc = await _service(tmp_path)
    client, auth = _app(tmp_path, svc)
    r = client.post(
        "/api/chat/attachments",
        files={"file": ("payload.exe", b"not a document", "application/octet-stream")},
        headers=_cookie(auth),
    )
    assert r.status_code == 400
    assert r.json()["message"] == (
        "Kira couldn't add that file. Use a supported document under the upload limit."
    )


async def test_chat_files_are_limited_to_the_exact_live_session(tmp_path: Path) -> None:
    svc = await _service(tmp_path)
    sessions = SessionStore(svc.store.db)
    chat_a = await sessions.create_session()
    chat_b = await sessions.create_session()
    await svc.ingest_uploaded(
        "a.md", b"# A\n\nA chat canary", created_by="user", source_session_id=chat_a
    )
    await svc.ingest_uploaded(
        "b.md", b"# B\n\nB chat canary", created_by="user", source_session_id=chat_b
    )
    client, auth = _app(tmp_path, svc)
    client.app.state.session = SimpleNamespace(session_id=chat_a, project_id=None)
    payload = client.get("/api/chat/files", headers=_cookie(auth)).json()
    assert [row["title"] for row in payload["files"]] == ["a.md"]
    assert "A chat canary" not in str(payload) and "B chat canary" not in str(payload)
    assert "markdown_path" not in payload["files"][0]


async def test_chat_knowledge_is_project_scoped_and_bodies_free(tmp_path: Path) -> None:
    svc = await _service(tmp_path)
    projects = ProjectStore(svc.store.db)
    project_a = await projects.create(name="Project A")
    project_b = await projects.create(name="Project B")
    await svc.ingest_uploaded(
        "alpha.md", b"# Alpha\n\nPROJECT-A-BODY-CANARY", created_by="user", project_id=project_a
    )
    await svc.ingest_uploaded(
        "beta.md", b"# Beta\n\nPROJECT-B-BODY-CANARY", created_by="user", project_id=project_b
    )
    client, auth = _app(tmp_path, svc)
    client.app.state.session = SimpleNamespace(session_id=91, project_id=project_a)

    payload = client.get("/api/chat/knowledge", headers=_cookie(auth)).json()

    assert payload["project_id"] == project_a
    assert payload["source_count"] == 1
    assert [source["title"] for source in payload["sources"]] == ["alpha.md"]
    assert payload["graph"] == {
        "available": False,
        "nodes": [],
        "edge_count": 0,
        "truncated": False,
    }
    rendered = str(payload)
    assert "PROJECT-A-BODY-CANARY" not in rendered
    assert "PROJECT-B-BODY-CANARY" not in rendered
    assert "chat-upload" not in rendered
    assert "markdown_path" not in rendered

    # A workspace tab may state the project it is rendering, but the value is only a consistency
    # check against the authenticated chat/workspace scope — never a cross-project selector.
    matched = client.get(f"/api/chat/knowledge?project_id={project_a}", headers=_cookie(auth))
    assert matched.status_code == 200 and matched.json()["project_id"] == project_a
    foreign = client.get(f"/api/chat/knowledge?project_id={project_b}", headers=_cookie(auth))
    assert foreign.status_code == 404

    client.app.state.session = SimpleNamespace(session_id=91, project_id=None)
    global_payload = client.get("/api/chat/knowledge", headers=_cookie(auth)).json()
    assert global_payload["project_id"] is None
    assert global_payload["sources"] == []


async def test_chat_attachment_binds_to_a_legacy_live_session_when_one_exists(
    tmp_path: Path,
) -> None:
    svc = await _service(tmp_path)
    sessions = SessionStore(svc.store.db)
    chat_id = await sessions.create_session()
    client, auth = _app(tmp_path, svc)
    client.app.state.session = SimpleNamespace(session_id=chat_id, project_id=None)
    uploaded = client.post(
        "/api/chat/attachments",
        files={"file": ("legacy.md", b"# Legacy\n\nScoped upload", "text/markdown")},
        headers=_cookie(auth),
    )
    assert uploaded.status_code == 200 and uploaded.json()["ok"]
    source = await svc.store.get_source(uploaded.json()["source_id"])
    assert source is not None and source.source_session_id == chat_id
    listed = client.get("/api/chat/files", headers=_cookie(auth)).json()
    assert [row["title"] for row in listed["files"]] == ["legacy.md"]


async def test_chat_attachment_captures_one_expected_workspace_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    svc = await _service(tmp_path)
    sessions = SessionStore(svc.store.db, svc.store.lock)
    projects = ProjectStore(svc.store.db, svc.store.lock)
    session_a = await sessions.create_session()
    session_b = await sessions.create_session()
    project_a = await projects.create(name="Project A")
    project_b = await projects.create(name="Project B")
    workspace = SimpleNamespace(
        context=ExecutionContext(session_id=session_a, project_id=project_a),
        context_revision=8,
    )
    client, auth = _app(tmp_path, svc)
    client.app.state.workspaces = SimpleNamespace(
        resolve=lambda **_kw: workspace,
        transition_lock=asyncio.Lock(),
        claim_matches=lambda candidate, context, revision: (
            candidate is workspace
            and context == workspace.context
            and revision == workspace.context_revision
        ),
    )
    original_close = UploadFile.close
    switched = False

    async def close_and_switch_context(upload: UploadFile) -> None:
        nonlocal switched
        await original_close(upload)
        if not switched:
            switched = True
            # The vulnerable route read project_id before the body and session_id after close,
            # producing an impossible A/B source. The fixed route freezes A/A before either yield.
            workspace.context = ExecutionContext(session_id=session_b, project_id=project_b)
            workspace.context_revision += 1

    monkeypatch.setattr(UploadFile, "close", close_and_switch_context)
    response = client.post(
        "/api/chat/attachments",
        data={
            "expected_session_id": str(session_a),
            "expected_project_id": str(project_a),
            "expected_context_revision": "8",
        },
        files={"file": ("scoped.md", b"# Scoped\n\nContext", "text/markdown")},
        headers={**_cookie(auth), WORKSPACE_HEADER: "w" * 24},
    )
    assert response.status_code == 200 and response.json()["ok"] is True
    source = await svc.store.get_source(response.json()["source_id"])
    assert source is not None
    assert (source.source_session_id, source.project_id) == (session_a, project_a)

    rejected = client.post(
        "/api/chat/attachments",
        data={
            "expected_session_id": str(session_a),
            "expected_project_id": str(project_a),
            "expected_context_revision": "8",
        },
        files={"file": ("stale.md", b"# Stale", "text/markdown")},
        headers={**_cookie(auth), WORKSPACE_HEADER: "w" * 24},
    )
    assert rejected.status_code == 409


async def test_ingest_route_denies_sensitive_path_403(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("SECRET=1", encoding="utf-8")
    svc = await _service(tmp_path)
    client, auth = _app(tmp_path, svc)
    r = client.post("/api/vault/ingest", json={"path": ".env"}, headers=_cookie(auth))
    assert r.status_code == 403  # same sensitive-path floor as the ingest_source tool


async def test_ingest_route_requires_exactly_one(tmp_path: Path) -> None:
    svc = await _service(tmp_path)
    client, auth = _app(tmp_path, svc)
    r = client.post("/api/vault/ingest", json={"path": "a", "url": "b"}, headers=_cookie(auth))
    assert r.status_code == 400
    r2 = client.post("/api/vault/ingest", json={}, headers=_cookie(auth))
    assert r2.status_code == 400


def test_vault_js_has_ingest_box_and_text_preview() -> None:
    from kira.ui.server import STATIC_DIR

    js = (STATIC_DIR / "screens" / "vault.js").read_text(encoding="utf-8")
    assert "/api/vault/ingest" in js  # the ingest box posts here
    assert "review-preview" in js  # per-source content preview (informed approval)
    assert "textContent" in js  # preview is rendered as TEXT, never HTML (untrusted content)
    assert "project_readiness" in js
    assert "entire project into every prompt" in js

    workspace_js = (STATIC_DIR / "screens" / "workspace" / "vault.js").read_text(encoding="utf-8")
    assert "project_readiness" in workspace_js
    assert "direct verified dependencies" in workspace_js
    assert "/api/chat/knowledge?project_id=" in workspace_js


def test_ingest_route_requires_session(tmp_path: Path) -> None:
    # Valid loopback Origin (passes the anti-CSRF check) but NO session cookie ⇒ 401.
    auth = AuthManager(token="t")
    app = create_app(load_config(root=tmp_path, env_file=None), auth=auth)
    client = TestClient(app, base_url="http://127.0.0.1")
    r = client.post("/api/vault/ingest", json={"text": "x"}, headers={"origin": "http://127.0.0.1"})
    assert r.status_code == 401
