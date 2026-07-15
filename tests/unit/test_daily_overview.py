"""daily_overview + hub connector status (Phase 9 Task 8) — keyless read models."""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from jarvis.config import ProjectIntelligenceConfig, load_config
from jarvis.connectors.base import ConnectorRegistry
from jarvis.connectors.demo import DemoGoogleClient, DemoNotifier
from jarvis.graph import GraphStore
from jarvis.intelligence import AnalysisJobState, AnalysisJobStore, ProjectReportStore
from jarvis.knowledge.store import KnowledgeStore
from jarvis.persistence.db import connect
from jarvis.projects import ProjectStore, seal_snapshot
from jarvis.ui.auth import SESSION_COOKIE, AuthManager
from jarvis.ui.readmodels import UiServices, daily_overview, hub_status
from jarvis.ui.server import create_app

_OPEN: list = []


@pytest.fixture(autouse=True)
async def _close() -> None:
    yield
    while _OPEN:
        await _OPEN.pop().close()


def _cfg(tmp_path: Path):
    return load_config(root=tmp_path, env_file=None)


def _intelligence_cfg(tmp_path: Path):
    return _cfg(tmp_path).model_copy(
        update={
            "project_intelligence": ProjectIntelligenceConfig(
                enabled=True, analyze_after_import=True
            )
        }
    )


async def _source(
    knowledge: KnowledgeStore, project_id: int, path: str, body: bytes
) -> None:
    digest = hashlib.sha256(body).hexdigest()
    await knowledge.add_source(
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


async def test_daily_overview_shape_with_no_services(tmp_path: Path) -> None:
    out = await daily_overview(_cfg(tmp_path), UiServices(), gate_pending=3)
    assert out["pending_approvals"] == 3
    assert out["tasks_today"] == [] and out["kb_review_count"] == 0
    assert out["digest"] is None and out["notices"] == []
    assert out["demo"] is False
    # Phase 11 Daily command-center reads: recent artifacts + latest run degrade to empty/None
    # when their stores aren't composed (never an error).
    assert out["recent_artifacts"] == [] and out["latest_run"] is None
    assert out["project_assessment"] is None
    # tmp_path isn't a git repo ⇒ the "." repo state is None (never an error).
    assert out["repos"] == [{"path": ".", "state": None}]
    assert out["evals"]["ever_run"] is False
    assert out["evals"]["replay_command"] == "uv run kira eval gate --suite core"
    assert "command" not in out["evals"] and "last_gate_cost_usd" not in out["evals"]
    assert "--scenario permission_denied" in out["evals"]["live_command"]
    assert "--max-cost-usd 1.00" in out["evals"]["live_command"]
    assert "positive finite --max-cost-usd LLM spend stop threshold" in out["evals"]["cost_note"]
    assert "partial signal, not closeout evidence" in out["evals"]["cost_note"]
    assert "Stop the running Kira process" in out["evals"]["cost_note"]
    assert "jarvis" not in str(out["evals"]).lower()


async def test_daily_project_assessment_lifecycle_is_compact_and_freshness_bound(
    tmp_path: Path,
) -> None:
    db = await connect(tmp_path / "daily-intelligence.db")
    _OPEN.append(db)
    lock = asyncio.Lock()
    project_id = await ProjectStore(db, lock).create(name="Kira")
    knowledge = KnowledgeStore(db, lock)
    graph = GraphStore(db, lock)
    jobs = AnalysisJobStore(db, lock)
    reports = ProjectReportStore(db, lock)

    async def _unreviewed_sources() -> list:
        return []

    services = UiServices(
        knowledge=SimpleNamespace(
            store=knowledge, unreviewed_sources=_unreviewed_sources
        ),
        graph=graph,
        analysis_jobs=jobs,
        project_reports=reports,
    )

    disabled = await daily_overview(
        _cfg(tmp_path), services, assessment_project_id=project_id
    )
    assert disabled["project_assessment"] == {"state": "disabled", "report": None}

    await _source(knowledge, project_id, "repo/app.py", b"app")
    snapshot = await seal_snapshot(knowledge, graph, project_id)
    job, _ = await jobs.enqueue(
        project_id=project_id,
        snapshot_hash=snapshot.snapshot_hash,
        profile_version="project-intelligence-v1",
        coverage=snapshot.coverage,
    )
    cfg = _intelligence_cfg(tmp_path)
    queued = await daily_overview(cfg, services, assessment_project_id=project_id)
    assert queued["project_assessment"] == {"state": "queued", "report": None}

    assert await jobs.claim(job.id) is not None
    running = await daily_overview(cfg, services, assessment_project_id=project_id)
    assert running["project_assessment"] == {"state": "running", "report": None}

    report, _ = await reports.create(
        project_id=project_id,
        snapshot_hash=snapshot.snapshot_hash,
        profile_version="project-intelligence-v1",
        summary="A focused project with one high-value improvement.",
        coverage=snapshot.coverage,
        strengths=[{"title": "Strong core"}],
        weaknesses=[{"title": "Thin interface"}],
        security_candidates=[{"title": "Review boundary"}],
        fe_be_gaps=[{"title": "Hidden backend capability"}],
        test_gaps=[{"title": "Missing UI contract"}],
        recommendations=[{"title": "Expose the capability"}],
    )
    assert await jobs.finish(job.id, AnalysisJobState.PUBLISHED) is True
    ready = await daily_overview(cfg, services, assessment_project_id=project_id)
    assert ready["project_assessment"] == {
        "state": "ready",
        "report": {
            "id": report.id,
            "summary_preview": "A focused project with one high-value improvement.",
            "created_at": report.created_at,
            "trust_class": "model_generated",
            "counts": {
                "strengths": 1,
                "weaknesses": 1,
                "security_candidates": 1,
                "frontend_backend_gaps": 1,
                "test_reliability_gaps": 1,
                "recommendations": 1,
            },
            "coverage": snapshot.coverage,
        },
    }

    await _source(knowledge, project_id, "repo/new.py", b"new")
    stale = await daily_overview(cfg, services, assessment_project_id=project_id)
    assert stale["project_assessment"] == {"state": "idle", "report": None}

    newer = await seal_snapshot(knowledge, graph, project_id)
    failed_job, _ = await jobs.enqueue(
        project_id=project_id,
        snapshot_hash=newer.snapshot_hash,
        profile_version="project-intelligence-v1",
        coverage=newer.coverage,
    )
    assert await jobs.claim(failed_job.id) is not None
    assert await jobs.finish(
        failed_job.id, AnalysisJobState.FAILED, error="provider detail must not ship"
    ) is True
    failed = await daily_overview(cfg, services, assessment_project_id=project_id)
    assert failed["project_assessment"] == {"state": "failed", "report": None}


async def test_daily_overview_surfaces_digest_and_demo(tmp_path: Path) -> None:
    latest = SimpleNamespace(
        date_local="2026-07-06",
        generated_at="2026-07-06T08:00:00+00:00",
        summary="[DEMO] All calm.",
        suggested_actions=["Reply to Bob"],
        sections=[],
        delivered_to=["ui"],
    )

    class _Digests:
        async def latest(self):
            return latest

    services = UiServices(
        connectors=ConnectorRegistry(
            google=DemoGoogleClient(), notifiers={"telegram": DemoNotifier("telegram")}, demo=True
        ),
        digests=_Digests(),
    )
    out = await daily_overview(_cfg(tmp_path), services)
    assert out["digest"]["summary"] == "[DEMO] All calm."
    assert out["digest"]["suggested_actions"] == ["Reply to Bob"]
    assert out["demo"] is True and out["connectors"]["demo"] is True


async def test_eval_freshness_stale_when_head_moved(tmp_path: Path) -> None:
    # A gated rev that differs from HEAD ⇒ stale. (Synthesised repo state + history line.)
    cfg = _cfg(tmp_path)
    (cfg.data_dir / "evals").mkdir(parents=True, exist_ok=True)
    (cfg.data_dir / "evals" / "history.jsonl").write_text(
        '{"git_rev": "oldrev", "verdict": "PASS", "timestamp": "2026-07-01T00:00:00"}\n',
        encoding="utf-8",
    )
    from jarvis.ui.readmodels import _eval_freshness

    fresh = _eval_freshness(cfg, [{"path": ".", "state": {"head_rev": "oldrev"}}])
    assert fresh["stale"] is False and fresh["verdict"] == "PASS"
    stale = _eval_freshness(cfg, [{"path": ".", "state": {"head_rev": "newrev"}}])
    assert stale["stale"] is True


def test_hub_status_carries_connector_status_not_secrets(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    connectors = ConnectorRegistry(
        notifiers={"telegram": DemoNotifier("telegram")}, demo=True
    ).status()
    hub = hub_status(cfg, connectors=connectors)
    assert hub["connectors"]["demo"] is True
    assert "telegram" in hub["connectors"]["notifiers"]
    assert hub["mcp"]["connected"] is False  # honest stub unchanged


def test_hub_status_defaults_connectors_when_absent(tmp_path: Path) -> None:
    hub = hub_status(_cfg(tmp_path))
    assert hub["connectors"] == {"demo": False, "google": None, "notifiers": {}}


def test_daily_route_is_served_and_session_gated(tmp_path: Path) -> None:
    auth = AuthManager(token="t")
    app = create_app(_cfg(tmp_path), auth=auth)
    client = TestClient(app, base_url="http://127.0.0.1")
    assert client.get("/api/daily").status_code == 401  # no session
    r = client.get("/api/daily", headers={"cookie": f"{SESSION_COOKIE}={auth.mint_session()}"})
    assert r.status_code == 200
    body = r.json()
    assert "repos" in body and "evals" in body and "connectors" in body
