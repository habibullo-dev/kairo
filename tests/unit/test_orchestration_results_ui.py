"""Run-result navigation is inspect-only and keeps raw child reports private."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
STUDIO = (ROOT / "src/jarvis/ui/static/screens/studio.js").read_text(encoding="utf-8")
OVERVIEW = (ROOT / "src/jarvis/ui/static/screens/workspace/overview.js").read_text(encoding="utf-8")
WORKSPACE_ARTIFACTS = (
    ROOT / "src/jarvis/ui/static/screens/workspace/artifacts.js"
).read_text(encoding="utf-8")
ARTIFACTS = (ROOT / "src/jarvis/ui/static/screens/artifacts.js").read_text(encoding="utf-8")
READMODELS = (ROOT / "src/jarvis/ui/readmodels.py").read_text(encoding="utf-8")


def test_orchestration_artifacts_open_the_existing_read_only_run_detail() -> None:
    for source in (OVERVIEW, WORKSPACE_ARTIFACTS, ARTIFACTS):
        assert 'origin_type === "orchestration"' in source
        assert "studio/${" in source
    assert "api.get(`/api/orchestration/${runId}`)" in STUDIO
    assert "api.post(`/api/orchestration" not in OVERVIEW
    assert "api.post(`/api/orchestration" not in WORKSPACE_ARTIFACTS


def test_studio_renders_only_head_syntheses_not_raw_child_report_text() -> None:
    assert "What the team found" in STUDIO
    assert "What each member found" in STUDIO
    assert "Final rationale" in STUDIO
    assert "synthesis_findings" in STUDIO and "verdict_rationale" in STUDIO
    assert "Recommended next steps" in STUDIO and "action_items" in STUDIO
    assert "not scheduled or run automatically" in STUDIO
    assert "result_text" not in STUDIO
    assert "result_text" not in READMODELS


def test_workspace_tasks_reads_inert_follow_ups_without_creating_scheduler_tasks() -> None:
    tasks = (ROOT / "src/jarvis/ui/static/screens/workspace/tasks.js").read_text(encoding="utf-8")
    assert 'api.get("/api/orchestration?project_id=" + ctx.projectId)' in tasks
    assert "Team follow-ups" in tasks
    assert "never schedule work automatically" in tasks
    assert "/api/tasks/create" not in tasks
