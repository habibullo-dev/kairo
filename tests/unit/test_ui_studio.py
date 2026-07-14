"""Studio polish pins (Phase 11 T12) — the head-reviewer (Fable) badge, the shared escaper, the
model+provider route chip, and the no-new-authority invariant. Presentational polish over the
existing Phase 10B orchestration flow (unchanged)."""

from __future__ import annotations

from jarvis.ui.server import STATIC_DIR

STUDIO = (STATIC_DIR / "screens" / "studio.js").read_text(encoding="utf-8")
REPORT = (STATIC_DIR / "ui" / "project-report.js").read_text(encoding="utf-8")


def test_head_reviewer_visibly_badged() -> None:
    # The head synthesizer/verdict (Fable, an engine stage) is badged on the roster, the live
    # verdict, and a run's synthesis — three call sites.
    assert "head-badge" in STUDIO and "Fable" in STUDIO
    assert STUDIO.count("headBadge(S.head)") == 3


def test_uses_shared_escaper_no_local_dup() -> None:
    assert 'import { esc } from "../ui/dom.js"' in STUDIO
    assert "function esc(" not in STUDIO  # the local escaper was removed


def test_roster_shows_model_and_provider() -> None:
    assert "routeLabel" in STUDIO and "provider" in STUDIO


def test_studio_adds_no_new_authority() -> None:
    # Studio's only mutation is the existing gated orchestration run; no turn / no other route.
    assert "/api/orchestration/run" in STUDIO
    assert "/api/turn" not in STUDIO


def test_studio_does_not_claim_roi_before_review_acceptance() -> None:
    assert 'roi.outcome === "review_accepted"' in STUDIO
    assert "Time-saved value is not claimed." in STUDIO


def test_studio_describes_skill_manifest_as_recorded_evidence_only() -> None:
    assert "Skill packs recorded at run start" in STUDIO
    assert "recorded skills:" in STUDIO
    assert "Recorded metadata does not prove prompt injection" in STUDIO
    assert "Shadow mode records manifests without injecting guidance." in STUDIO


def test_project_recommendation_prefills_and_estimates_but_never_auto_runs() -> None:
    assert "Review with AI team" in REPORT
    assert "studio/report/${reportId}/${recommendationIndex}" in REPORT
    start = STUDIO.index("async function applyReportPrefill")
    end = STUDIO.index("\nfunction params", start)
    prefill = STUDIO[start:end]
    assert "S.task = prefill.task" in prefill
    assert "task.value = S.task" in prefill
    assert "Review scope and cost. Nothing has started." in prefill
    assert "await doEstimate(container, api, () => (" in prefill
    assert "doRun" not in prefill and "api.post" not in prefill
    assert "/studio-prefill?recommendation=" in STUDIO


def test_project_recommendation_route_and_payload_are_consumer_validated() -> None:
    assert "args.length !== 3" in STUDIO
    assert "/^[1-9]\\d{0,9}$/" in STUDIO
    assert "/^[0-4]$/" in STUDIO
    assert "prefill.report_id !== route.reportId" in STUDIO
    assert "prefill.recommendation !== route.recommendation" in STUDIO
    assert "team.default_workflows.includes(prefill.workflow)" in STUDIO
    assert "prefill.task.length > 720" in STUDIO


def test_project_prefill_draft_is_reset_scoped_and_race_guarded() -> None:
    assert "S.projectId !== cat.active_project_id || S.authorityToken !== authorityToken" in STUDIO
    assert "resetForProject(cat.active_project_id, authorityToken)" in STUDIO
    assert "S.prefillKey === key" in STUDIO
    assert 'S.task = ""' in STUDIO and 'S.budget = ""' in STUDIO
    assert "renderGeneration !== _renderGeneration" in STUDIO
    assert "estimateGeneration !== _estimateGeneration" in STUDIO
    assert "projectId !== S.projectId" in STUDIO
    assert "responseIsCurrent && !responseIsCurrent()" in STUDIO
    assert '#st-task")?.addEventListener("input"' in STUDIO
    assert "S.task = e.target.value;" in STUDIO
    assert '#st-budget")?.addEventListener("input"' in STUDIO
    assert "S.budget = e.target.value;" in STUDIO
    assert "invalidateEstimate(container);" in STUDIO
    assert 'api.getRequired("/api/studio")' in STUDIO
    assert "if (_runOperation) return;" in STUDIO
    assert "renderRunControls(container);" in STUDIO
