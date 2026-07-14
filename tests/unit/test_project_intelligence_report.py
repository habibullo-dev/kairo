"""Snapshot-validated projection of Fable project-assessment output."""

from __future__ import annotations

from jarvis.intelligence import build_report_draft
from jarvis.orchestration.store import OrchestrationRun
from jarvis.projects import ProjectSnapshot, SnapshotSource


def _snapshot() -> ProjectSnapshot:
    return ProjectSnapshot(
        project_id=7,
        snapshot_hash="abc",
        graph_watermark=4,
        sources=(
            SnapshotSource(17, "repo/api/routes.py", "file", "a", "b", 10, "reviewed"),
            SnapshotSource(18, "repo/core.py", "file", "c", "d", 20, "reviewed"),
        ),
        coverage={"files_total": 2, "graph_edges": 4},
    )


def _run(*, findings: list, actions: list | None = None) -> OrchestrationRun:
    return OrchestrationRun(
        id=3,
        project_id=7,
        workflow="project_assessment",
        title="assessment",
        config={"team": "project_intelligence"},
        context_manifest=[],
        status="ok",
        stage="verdict",
        verdict="accept",
        synthesis_summary="Project is promising but needs focused work.",
        estimated_cost_usd=1.0,
        actual_cost_usd=0.8,
        budget_usd=2.0,
        session_id=None,
        trace_id=None,
        started_at="2026-07-14T00:00:00+00:00",
        finished_at="2026-07-14T00:01:00+00:00",
        created_at="2026-07-14T00:00:00+00:00",
        skills_manifest=[],
        verdict_rationale="grounded",
        synthesis_findings=findings,
        action_items=actions or [],
        resume_state="none",
        resume_checkpoint={},
    )


def _finding(
    finding_id: str,
    category: str,
    title: str,
    evidence_ref: str,
) -> dict:
    return {
        "finding_id": finding_id,
        "member": "security_risk" if category == "security_candidate" else "architecture_backend",
        "category": category,
        "finding_title": title,
        "finding": "Bounded evidence-oriented detail.",
        "severity": "high",
        "confidence": "medium",
        "evidence_ref": evidence_ref,
    }


def test_report_projection_buckets_and_validates_snapshot_evidence() -> None:
    strength_id = "finding-1111111111111111"
    security_id = "finding-2222222222222222"
    run = _run(
        findings=[
            _finding(strength_id, "strength", "Clear API", "repo/api/routes.py"),
            _finding(security_id, "security_candidate", "Guard needs review", "source #17"),
            _finding("finding-3333333333333333", "weakness", "Foreign", "source #999"),
            _finding("finding-4444444444444444", "weakness", "Traversal", "../secret.py"),
        ],
        actions=[
            {
                "title": "Validate the guard",
                "goal": "Review the cited authorization boundary.",
                "priority": "high",
                "source_finding_id": security_id,
                "suggested_team": "security",
                "suggested_workflow": "security_review",
            }
        ],
    )
    draft = build_report_draft(run, _snapshot(), host_coverage={"context_chars": 900})
    assert draft.coverage["files_total"] == 2
    assert draft.coverage["context_chars"] == 900
    assert draft.coverage["findings_retained"] == 2
    assert draft.coverage["findings_dropped_unsupported"] == 2
    assert draft.strengths[0]["severity"] == "info"
    assert draft.security_candidates[0]["validated"] is False
    assert draft.security_candidates[0]["validation"] == "candidate"
    assert draft.recommendations[0]["source_finding_id"] == security_id
    assert draft.evidence == [
        {"kind": "path", "ref": "repo/api/routes.py", "trust": "model_cited"},
        {"kind": "source", "ref": "17", "trust": "model_cited"},
    ]


def test_report_projection_rejects_control_text_and_unaccepted_run() -> None:
    token = "sk-proj-" + "a" * 30
    finding = _finding(
        "finding-1111111111111111",
        "strength",
        "Safe\u202esecret",
        "repo/core.py",
    )
    finding["finding"] = f"A leaked credential {token} must not persist."
    run = _run(
        findings=[finding]
    )
    draft = build_report_draft(run, _snapshot())
    assert draft.strengths[0]["title"] == "Safesecret"
    assert token not in draft.strengths[0]["detail"]
    assert "[REDACTED_SECRET:openai_key]" in draft.strengths[0]["detail"]
    rejected = OrchestrationRun(**{**run.__dict__, "status": "rejected", "verdict": "reject"})
    try:
        build_report_draft(rejected, _snapshot())
    except ValueError as exc:
        assert "accepted" in str(exc)
    else:  # pragma: no cover - explicit fail message without pytest dependency
        raise AssertionError("rejected assessment was publishable")
