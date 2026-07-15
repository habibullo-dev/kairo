"""Exact-project, freshness-checked project-intelligence report routes."""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from kira.config import load_config
from kira.graph import GraphStore
from kira.intelligence import AnalysisJobStore, ProjectReportStore
from kira.knowledge.store import KnowledgeStore
from kira.persistence.db import connect
from kira.projects import ProjectStore, seal_snapshot
from kira.projects.service import ProjectService
from kira.ui.auth import SESSION_COOKIE, AuthManager
from kira.ui.readmodels import UiServices
from kira.ui.server import create_app

_OPEN: list = []


@pytest.fixture(autouse=True)
async def _close() -> None:
    yield
    while _OPEN:
        await _OPEN.pop().close()


async def _source(knowledge: KnowledgeStore, project_id: int, path: str, body: bytes) -> int:
    digest = hashlib.sha256(body).hexdigest()
    return await knowledge.add_source(
        kind="file",
        origin=f"chat-upload:{project_id}:{path}",
        title=path,
        content_hash=digest,
        raw_path=f"raw/{digest[:12]}",
        markdown_path=f"markdown/{digest[:12]}.md",
        markdown_hash=digest,
        converter="passthrough",
        converter_version="1",
        byte_size=len(body),
        mime="text/plain",
        review_status="reviewed",
        created_by="user",
        project_id=project_id,
    )


async def _fixture(tmp_path: Path):
    db = await connect(tmp_path / "reports.db")
    _OPEN.append(db)
    lock = asyncio.Lock()
    project_store = ProjectStore(db, lock)
    project_id = await project_store.create(name="Kira")
    project_service = ProjectService(project_store)
    await project_service.activate(project_id)
    knowledge = KnowledgeStore(db, lock)
    graph = GraphStore(db, lock)
    reports = ProjectReportStore(db, lock)
    await _source(knowledge, project_id, "repo/app.py", b"app")
    snapshot = await seal_snapshot(knowledge, graph, project_id)
    report, _ = await reports.create(
        project_id=project_id,
        snapshot_hash=snapshot.snapshot_hash,
        profile_version="project-intelligence-v1",
        orchestration_run_id=None,
        summary="Promising\u202e project\x00 with focused work needed.",
        coverage={
            "files_total": 1,
            "graph_edges": 0,
            "context_truncated": False,
            "bytes_total": -1,
            "internal_provider_cost": 9.9,
        },
        strengths=[
            {
                "title": "Clear entry point",
                "detail": "The project has a visible entry point.",
                "member": "architecture_backend",
                "severity": "invalid",
                "confidence": "high",
                "evidence": [
                    {"kind": "path", "ref": "repo/app.py", "local_path": "C:/secret"},
                    {"kind": "path", "ref": "C:/secret"},
                ],
                "raw_child_report": "must not ship",
            }
        ],
        security_candidates=[
            {
                "title": "Guard needs validation",
                "detail": "Authorization behavior needs independent validation.",
                "member": "security_risk",
                "severity": "high",
                "confidence": "medium",
                "validated": True,
                "evidence": [{"kind": "path", "ref": "repo/app.py"}],
            }
        ],
        recommendations=[
            {
                "title": "Review the guard",
                "goal": "Validate and repair the authorization boundary.",
                "priority": "high",
                "suggested_team": "security",
                "suggested_workflow": "security_review",
                "command": "run automatically",
            }
        ],
        evidence=[{"kind": "path", "ref": "repo/app.py"}],
    )
    auth = AuthManager(token="t")
    app = create_app(load_config(root=tmp_path, env_file=None), auth=auth)
    app.state.projects = project_service
    app.state.services = UiServices(
        knowledge=SimpleNamespace(store=knowledge),
        graph=graph,
        analysis_jobs=AnalysisJobStore(db, lock),
        project_reports=reports,
    )
    client = TestClient(app, base_url="http://127.0.0.1")
    headers = {
        "cookie": f"{SESSION_COOKIE}={auth.mint_session()}",
        "origin": "http://127.0.0.1",
    }
    return client, headers, project_store, project_service, knowledge, project_id, report


async def test_report_detail_is_bounded_current_and_candidate_only(tmp_path: Path) -> None:
    client, headers, _projects, _service, _knowledge, _project_id, report = await _fixture(
        tmp_path
    )
    response = client.get(
        f"/api/project-intelligence/reports/{report.id}", headers=headers
    )
    assert response.status_code == 200
    body = response.json()["report"]
    assert body["status"] == "current"
    assert body["summary"] == "Promising project with focused work needed."
    assert body["coverage"] == {
        "files_total": 1,
        "graph_edges": 0,
        "context_truncated": False,
    }
    assert body["strengths"][0]["severity"] == "info"
    assert body["security_candidates"][0]["validated"] is False
    assert body["security_candidates"][0]["validation"] == "candidate"
    rendered = repr(body)
    for forbidden in (
        "snapshot_hash",
        "orchestration_run_id",
        "raw_child_report",
        "local_path",
        "C:/secret",
        "internal_provider_cost",
        "run automatically",
    ):
        assert forbidden not in rendered


async def test_report_and_prefill_require_the_exact_active_project(tmp_path: Path) -> None:
    client, headers, projects, service, _knowledge, _project_id, report = await _fixture(tmp_path)
    foreign_id = await projects.create(name="Foreign")
    await service.activate(foreign_id)
    path = f"/api/project-intelligence/reports/{report.id}"
    assert client.get(path, headers=headers).status_code == 404
    assert client.get(f"{path}/studio-prefill", headers=headers).status_code == 404
    await service.activate(None)
    assert client.get(path, headers=headers).status_code == 404


async def test_studio_prefill_is_get_only_current_and_never_starts_work(tmp_path: Path) -> None:
    client, headers, _projects, _service, knowledge, project_id, report = await _fixture(tmp_path)
    path = f"/api/project-intelligence/reports/{report.id}/studio-prefill?recommendation=0"
    response = client.get(path, headers=headers)
    assert response.status_code == 200
    assert response.json() == {
        "ok": True,
        "prefill": {
            "report_id": report.id,
            "recommendation": 0,
            "team": "security",
            "workflow": "security_review",
            "task": (
                "Project assessment recommendation: Review the guard\n\n"
                "Goal: Validate and repair the authorization boundary."
            ),
        },
        "notice": "Review scope and cost. Nothing has started.",
    }
    assert client.post(path, headers=headers).status_code == 405
    missing = client.get(
        f"/api/project-intelligence/reports/{report.id}/studio-prefill?recommendation=9",
        headers=headers,
    )
    assert missing.status_code == 404

    await _source(knowledge, project_id, "repo/new.py", b"new")
    detail = client.get(
        f"/api/project-intelligence/reports/{report.id}", headers=headers
    )
    assert detail.status_code == 200 and detail.json()["report"]["status"] == "stale"
    stale = client.get(path, headers=headers)
    assert stale.status_code == 409
    assert "snapshot" not in stale.text.lower()
