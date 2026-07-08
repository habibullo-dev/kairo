"""Phase 11 T4 routes: artifacts / saved views / global search / workspace + the content route.

Safety-critical (Checkpoint E): the /api/artifacts/{id}/content route serves ONLY registered,
in-managed-root, non-sensitive, non-quarantined, allowlisted-type files; and a manual secret
sweep covers the parameterized GETs the auto-sweep skips. Keyless (temp SQLite v9)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from jarvis.persistence.artifacts import ArtifactStore
from jarvis.persistence.db import connect
from jarvis.persistence.saved_views import SavedViewStore
from jarvis.projects import ProjectService, ProjectStore
from jarvis.ui.auth import SESSION_COOKIE, AuthManager
from jarvis.ui.readmodels import UiServices
from jarvis.ui.server import create_app

_OPEN: list = []
_TS = "2026-01-01T00:00:00+00:00"


@pytest.fixture(autouse=True)
async def _close():
    yield
    while _OPEN:
        await _OPEN.pop().close()


async def _setup(tmp_path: Path, *, auth_token: str = "tok", secret_update: dict | None = None):
    from jarvis.config import load_config

    cfg = load_config(root=tmp_path, env_file=None)
    if secret_update:
        cfg.secrets = cfg.secrets.model_copy(update=secret_update)
    db = await connect(tmp_path / "a.db")
    _OPEN.append(db)
    lock = asyncio.Lock()
    project_store = ProjectStore(db, lock)
    pid = await project_store.create(name="Proj")  # id 1
    projects = ProjectService(project_store)
    art_root = cfg.data_dir / "artifacts"
    art_root.mkdir(parents=True, exist_ok=True)
    store = ArtifactStore(db, lock, data_dir=cfg.data_dir, managed_roots={"artifacts": art_root})
    views = SavedViewStore(db, lock)
    auth = AuthManager(token=auth_token)
    app = create_app(
        cfg, auth=auth, services=UiServices(artifacts=store, views=views, projects=projects)
    )
    app.state.projects = projects
    client = TestClient(app, base_url="http://127.0.0.1")
    return client, auth, db, lock, store, cfg, pid, art_root


def _get(auth: AuthManager) -> dict[str, str]:
    return {"cookie": f"{SESSION_COOKIE}={auth.mint_session()}"}


def _post(auth: AuthManager) -> dict[str, str]:
    return {"cookie": f"{SESSION_COOKIE}={auth.mint_session()}", "origin": "http://127.0.0.1"}


# --- content route confinement (the safety-critical surface) --------------------------
async def test_content_serves_valid_in_root_file(tmp_path: Path) -> None:
    client, auth, _db, _lock, store, _cfg, pid, art_root = await _setup(tmp_path)
    (art_root / "note.md").write_text("hello artifact body", encoding="utf-8")
    aid = await store.register(origin_type="wiki", origin_id="w1", kind="wiki_page", title="Note",
                               local_path=art_root / "note.md", created_by="agent", project_id=pid)
    r = client.get(f"/api/artifacts/{aid}/content", headers=_get(auth))
    assert r.status_code == 200
    assert "hello artifact body" in r.text
    assert r.headers["content-type"].startswith("text/markdown")


async def test_content_refuses_external_uri_artifact(tmp_path: Path) -> None:
    client, auth, _db, _lock, store, _cfg, _pid, _root = await _setup(tmp_path)
    aid = await store.register(origin_type="digest", origin_id="d1", kind="digest", title="D",
                               external_uri="kairo://digest/1", created_by="system")
    assert client.get(f"/api/artifacts/{aid}/content", headers=_get(auth)).status_code == 404


async def test_content_refuses_quarantined_artifact(tmp_path: Path) -> None:
    client, auth, _db, _lock, store, _cfg, _pid, art_root = await _setup(tmp_path)
    (art_root / "q.md").write_text("quarantined body", encoding="utf-8")
    aid = await store.register(origin_type="meeting", origin_id="m1", kind="meeting_note",
                               title="M", local_path=art_root / "q.md", created_by="user",
                               sensitivity="quarantined")
    r = client.get(f"/api/artifacts/{aid}/content", headers=_get(auth))
    assert r.status_code == 404 and "quarantined body" not in r.text


async def test_content_refuses_unsupported_type(tmp_path: Path) -> None:
    client, auth, _db, _lock, store, _cfg, _pid, art_root = await _setup(tmp_path)
    (art_root / "x.exe").write_text("MZ", encoding="utf-8")
    aid = await store.register(origin_type="x", origin_id="e1", kind="blob", title="E",
                               local_path=art_root / "x.exe", created_by="system")
    assert client.get(f"/api/artifacts/{aid}/content", headers=_get(auth)).status_code == 415


async def test_content_refuses_path_escape_on_corrupted_row(tmp_path: Path) -> None:
    # register() refuses an escaping/sensitive path, so simulate a corrupted DB row via a direct
    # INSERT; the route's content_path re-confinement must still refuse to serve it.
    client, auth, db, lock, _store, _cfg, _pid, _root = await _setup(tmp_path)
    async with lock:
        cur = await db.execute(
            "INSERT INTO artifacts (kind, title, local_path, origin_type, created_by, "
            "labels_json, pinned, created_at, updated_at) "
            "VALUES ('doc','poison','../../../../etc/passwd','x','system','[]',0,?,?)",
            (_TS, _TS),
        )
        await db.commit()
    escaped_id = cur.lastrowid
    async with lock:
        cur = await db.execute(
            "INSERT INTO artifacts (kind, title, local_path, origin_type, created_by, "
            "labels_json, pinned, created_at, updated_at) "
            "VALUES ('doc','poison','artifacts/.env','x','system','[]',0,?,?)",
            (_TS, _TS),
        )
        await db.commit()
    sensitive_id = cur.lastrowid
    for bad in (escaped_id, sensitive_id):
        r = client.get(f"/api/artifacts/{bad}/content", headers=_get(auth))
        assert r.status_code == 404, bad
        assert "root:" not in r.text  # never the real /etc/passwd


async def test_content_unknown_id_is_404(tmp_path: Path) -> None:
    client, auth, *_ = await _setup(tmp_path)
    assert client.get("/api/artifacts/99999/content", headers=_get(auth)).status_code == 404


# --- artifacts list / detail / pin / label -------------------------------------------
async def test_artifacts_list_detail_pin_label(tmp_path: Path) -> None:
    client, auth, _db, _lock, store, _cfg, pid, art_root = await _setup(tmp_path)
    (art_root / "a.md").write_text("x", encoding="utf-8")
    aid = await store.register(origin_type="wiki", origin_id="w1", kind="wiki_page", title="A",
                               local_path=art_root / "a.md", created_by="agent", project_id=pid)
    listed = client.get("/api/artifacts", headers=_get(auth)).json()
    assert [a["id"] for a in listed["artifacts"]] == [aid]
    assert listed["artifacts"][0]["has_content"] is True
    assert "local_path" not in listed["artifacts"][0]  # internal path never shipped

    detail = client.get(f"/api/artifacts/{aid}", headers=_get(auth)).json()
    assert detail["title"] == "A" and detail["kind"] == "wiki_page"

    r_pin = client.post(f"/api/artifacts/{aid}/pin", json={"pinned": True}, headers=_post(auth))
    assert r_pin.json()["ok"]
    r_lbl = client.post(f"/api/artifacts/{aid}/label", json={"labels": ["coding", "review"]},
                        headers=_post(auth))
    assert r_lbl.json()["ok"]
    again = client.get(f"/api/artifacts/{aid}", headers=_get(auth)).json()
    assert again["pinned"] is True and again["labels"] == ["coding", "review"]


async def test_artifacts_label_rejects_non_list(tmp_path: Path) -> None:
    client, auth, _db, _lock, store, _cfg, _pid, art_root = await _setup(tmp_path)
    (art_root / "a.md").write_text("x", encoding="utf-8")
    aid = await store.register(origin_type="x", origin_id="w1", kind="doc", title="A",
                               local_path=art_root / "a.md", created_by="agent")
    r = client.post(f"/api/artifacts/{aid}/label", json={"labels": "nope"}, headers=_post(auth))
    assert r.status_code == 400


# --- saved views: save / list / delete -----------------------------------------------
async def test_views_save_list_delete(tmp_path: Path) -> None:
    client, auth, *_ = await _setup(tmp_path)
    saved = client.post("/api/views/save", json={"name": "Recent", "scope": "artifacts",
                                                 "query": {"pinned": True}}, headers=_post(auth))
    vid = saved.json()["id"]
    assert saved.json()["ok"]
    listed = client.get("/api/views", headers=_get(auth)).json()
    assert [v["id"] for v in listed["views"]] == [vid]
    assert client.post(f"/api/views/{vid}/delete", headers=_post(auth)).json()["ok"]
    assert client.get("/api/views", headers=_get(auth)).json()["views"] == []


async def test_views_save_rejects_bad_scope(tmp_path: Path) -> None:
    client, auth, *_ = await _setup(tmp_path)
    r = client.post("/api/views/save", json={"name": "X", "scope": "bogus"}, headers=_post(auth))
    assert r.status_code == 400


# --- search + workspace + projects pin ------------------------------------------------
async def test_search_route(tmp_path: Path) -> None:
    client, auth, _db, _lock, store, _cfg, pid, art_root = await _setup(tmp_path)
    (art_root / "a.md").write_text("x", encoding="utf-8")
    await store.register(origin_type="x", origin_id="w1", kind="doc", title="searchroutecanary",
                         local_path=art_root / "a.md", created_by="agent", project_id=pid)
    hits = client.get("/api/search", params={"q": "searchroutecanary"}, headers=_get(auth)).json()
    assert any(r["domain"] == "artifacts" for r in hits["results"])
    assert client.get("/api/search", params={"q": ""}, headers=_get(auth)).json()["results"] == []


async def test_workspace_and_project_pin(tmp_path: Path) -> None:
    client, auth, _db, _lock, _store, _cfg, pid, _root = await _setup(tmp_path)
    ws = client.get(f"/api/workspace/{pid}", headers=_get(auth)).json()
    assert ws["project_id"] == pid and ws["project"]["name"] == "Proj"
    r_pin = client.post(f"/api/projects/{pid}/pin", json={"pinned": True}, headers=_post(auth))
    assert r_pin.json()["ok"]


# --- manual secret sweep over the PARAMETERIZED GETs (auto-sweep skips {param}) --------
async def test_no_secret_on_parameterized_gets(tmp_path: Path) -> None:
    canaries = {
        "anthropic_api_key": "SECRET-CANARY-ANTHROPIC",
        "openai_api_key": "SECRET-CANARY-OPENAI",
        "voyage_api_key": "SECRET-CANARY-VOYAGE",
    }
    client, auth, _db, _lock, store, _cfg, pid, art_root = await _setup(
        tmp_path, auth_token="SECRET-CANARY-TOKEN", secret_update=canaries
    )
    (art_root / "a.md").write_text("ordinary body text", encoding="utf-8")
    aid = await store.register(origin_type="x", origin_id="w1", kind="doc", title="A",
                               local_path=art_root / "a.md", created_by="agent", project_id=pid)
    sid = auth.mint_session()
    needles = [*canaries.values(), "SECRET-CANARY-TOKEN", sid]
    # Parameterized GETs (auto-sweep skips {param}) PLUS the content-bearing non-parameterized
    # GETs, now with a composed store + seeded canaries.
    for path in (
        f"/api/artifacts/{aid}",
        f"/api/artifacts/{aid}/content",
        f"/api/workspace/{pid}",
        "/api/artifacts",
        "/api/views",
        "/api/search?q=A",
    ):
        r = client.get(path, headers={"cookie": f"{SESSION_COOKIE}={sid}"})
        blob = r.text + "\n" + "\n".join(f"{k}: {v}" for k, v in r.headers.items())
        for needle in needles:
            assert needle not in blob, f"{needle!r} leaked on GET {path}"


async def test_lab_overview_registers_latest_report_idempotently(tmp_path: Path) -> None:
    # The eval-report producer hook lives in the (read-only) Lab path: registering the latest
    # report as an artifact, idempotently + fail-soft + confined to the evals managed root.
    from jarvis.config import load_config
    from jarvis.ui.readmodels import lab_overview

    cfg = load_config(root=tmp_path, env_file=None)
    run_dir = cfg.data_dir / "evals" / "20260101-abc"
    run_dir.mkdir(parents=True)
    (run_dir / "report.md").write_text("# Eval gate\nPASS 19/19\n", encoding="utf-8")
    db = await connect(tmp_path / "lab.db")
    _OPEN.append(db)
    lock = asyncio.Lock()
    store = ArtifactStore(
        db, lock, data_dir=cfg.data_dir, managed_roots={"evals": cfg.data_dir / "evals"}
    )
    out1 = await lab_overview(cfg, artifacts=store)
    out2 = await lab_overview(cfg, artifacts=store)  # second open must not double-register
    assert "PASS 19/19" in out1["latest_report"] and "PASS 19/19" in out2["latest_report"]
    evals = [a for a in await store.list() if a.kind == "eval_report"]
    assert len(evals) == 1 and evals[0].origin_id == "20260101-abc"

    # Fail-soft: a store whose managed roots EXCLUDE evals suppresses the (confinement-refused)
    # register, and the Lab view still returns the report — no raise.
    store2 = ArtifactStore(
        db, lock, data_dir=cfg.data_dir, managed_roots={"artifacts": cfg.data_dir / "artifacts"}
    )
    out3 = await lab_overview(cfg, artifacts=store2)
    assert "PASS 19/19" in out3["latest_report"]
