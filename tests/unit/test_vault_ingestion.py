"""Vault ingestion (Phase 9 Task 9): ingest_folder, review preview, POST /api/vault/ingest.

Keyless — FakeEmbedder, .md passthrough, no network. The pins: bulk folder ingest skips
secrets + refuses symlinks + dedupes; the review queue ships a content preview; the UI ingest
route runs the SAME sensitive-path gate floor as the tool (DENY ⇒ 403)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from jarvis.config import KnowledgeConfig, load_config
from jarvis.knowledge.service import KnowledgeService
from jarvis.knowledge.store import KnowledgeStore
from jarvis.memory.embeddings import FakeEmbedder
from jarvis.persistence.db import connect
from jarvis.ui.auth import SESSION_COOKIE, AuthManager
from jarvis.ui.readmodels import UiServices, vault_overview
from jarvis.ui.server import create_app

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
    from jarvis.ui.server import STATIC_DIR

    js = (STATIC_DIR / "screens" / "vault.js").read_text(encoding="utf-8")
    assert "/api/vault/ingest" in js  # the ingest box posts here
    assert "review-preview" in js  # per-source content preview (informed approval)
    assert "textContent" in js  # preview is rendered as TEXT, never HTML (untrusted content)


def test_ingest_route_requires_session(tmp_path: Path) -> None:
    # Valid loopback Origin (passes the anti-CSRF check) but NO session cookie ⇒ 401.
    auth = AuthManager(token="t")
    app = create_app(load_config(root=tmp_path, env_file=None), auth=auth)
    client = TestClient(app, base_url="http://127.0.0.1")
    r = client.post(
        "/api/vault/ingest", json={"text": "x"}, headers={"origin": "http://127.0.0.1"}
    )
    assert r.status_code == 401
