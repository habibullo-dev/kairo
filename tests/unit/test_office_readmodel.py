"""AI Team Office read model (Phase 14 Task 1). office_overview is a pure ASSEMBLER over existing
read models: teams→rooms→nodes, the head reviewer, the canonical stage map, the latest run's live
summary + per-member overlay, recent runs, and the activity feed. Metadata/summaries only — never
a prompt, report body, or key value. Keyless: a temp DB + real stores; no live model."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

import kira.core  # noqa: F401 - load core first (ledger<->core.context import cycle in isolation)
from kira.agents import AgentRunStore
from kira.config import load_config
from kira.orchestration import OrchestrationStore
from kira.persistence.db import connect
from kira.projects import ProjectStore
from kira.ui.readmodels import UiServices, office_overview, teams_catalog

_OPEN: list = []
_NAMED_TEAMS = {"research", "frontend", "backend", "security", "qa", "pm", "ops"}


@pytest.fixture(autouse=True)
async def _close():
    yield
    while _OPEN:
        await _OPEN.pop().close()


async def _services(tmp_path: Path):
    db = await connect(tmp_path / "office.db")
    _OPEN.append(db)
    lock = asyncio.Lock()
    await ProjectStore(db, lock).create(name="P")  # id 1
    store, run_store = OrchestrationStore(db, lock), AgentRunStore(db, lock)
    return UiServices(orchestration=store, run_store=run_store), store, run_store


def _cfg(tmp_path: Path):
    return load_config(root=tmp_path, env_file=None)


async def test_rooms_head_and_stages(tmp_path: Path) -> None:
    services, _s, _r = await _services(tmp_path)
    ov = await office_overview(_cfg(tmp_path), services, 1)
    assert {r["team"] for r in ov["rooms"]} >= _NAMED_TEAMS  # a room per team
    assert ov["head"]["label"] == "Fable"  # head reviewer = planner route (synthesis + verdict)
    assert ov["stages"] == ["council", "synthesis", "execution", "review", "verdict"]
    research = next(r for r in ov["rooms"] if r["team"] == "research")
    assert research["nodes"], "a team room has member nodes"
    node = research["nodes"][0]
    assert {"member_id", "title", "role", "model", "provider", "tools", "services"} <= set(node)


async def test_empty_project_reads_idle(tmp_path: Path) -> None:
    services, _s, _r = await _services(tmp_path)
    ov = await office_overview(_cfg(tmp_path), services, 1)
    assert ov["live"] is None and ov["recent_runs"] == [] and ov["feed"] == []
    # every node is idle (no run has overlaid it)
    assert all(n["status"] == "idle" and n["stage"] is None
               for room in ov["rooms"] for n in room["nodes"])


async def test_latest_run_overlays_its_team_and_stays_bodies_free(tmp_path: Path) -> None:
    services, store, run_store = await _services(tmp_path)
    sec_team = next(t for t in teams_catalog() if t["id"] == "security")
    role = sec_team["members"][0]["route_role"]  # a role that really exists in the team
    rid = await store.begin_run(
        project_id=1, workflow="security_review", title="Security · review",
        config={"team": "security"}, context_manifest=[], estimated_cost_usd=0.4, budget_usd=2.0,
    )
    mid = await run_store.begin_run(
        parent_session_id=None, parent_trace_id=None, title="security:lead",
        prompt="SECRET-PROMPT-CANARY", tools_scope=["read_file"], project_id=1,
        orchestration_run_id=rid, role=role, stage="council",
    )
    await run_store.complete_run(mid, status="ok", result_text="SECRET-REPORT-CANARY")

    ov = await office_overview(_cfg(tmp_path), services, 1)
    assert ov["live"] and ov["live"]["team"] == "security"
    sec = next(r for r in ov["rooms"] if r["team"] == "security")
    overlaid = next(n for n in sec["nodes"] if n["role"] == role)
    assert overlaid["stage"] == "council" and overlaid["status"] == "ok"
    # A DIFFERENT team's nodes stay idle (only the live run's team is overlaid).
    research = next(r for r in ov["rooms"] if r["team"] == "research")
    assert all(n["status"] == "idle" for n in research["nodes"])
    # Bodies-free: neither the member prompt nor the report text can appear anywhere.
    blob = str(ov)
    assert "SECRET-PROMPT-CANARY" not in blob and "SECRET-REPORT-CANARY" not in blob


async def test_degrades_without_orchestration_service(tmp_path: Path) -> None:
    # No orchestration store composed ⇒ idle rooms + empty live/feed, never a crash.
    ov = await office_overview(_cfg(tmp_path), UiServices(), 1)
    assert ov["live"] is None and ov["recent_runs"] == []
    assert {r["team"] for r in ov["rooms"]} >= _NAMED_TEAMS
