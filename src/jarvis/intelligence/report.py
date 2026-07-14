"""Host-validated projection from an orchestration result to a publishable project report."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import TYPE_CHECKING

from jarvis.knowledge.secrets import scan_text

if TYPE_CHECKING:
    from jarvis.orchestration.store import OrchestrationRun
    from jarvis.projects.snapshot import ProjectSnapshot

_FINDING_ID = re.compile(r"finding-[0-9a-f]{16}")
_SOURCE_REF = re.compile(r"source\s*#\s*([1-9][0-9]*)", re.IGNORECASE)
_DRIVE_PATH = re.compile(r"^[a-zA-Z]:")
_CATEGORIES = {
    "strength": "strengths",
    "weakness": "weaknesses",
    "security_candidate": "security_candidates",
    "frontend_backend_gap": "fe_be_gaps",
    "test_reliability_gap": "test_gaps",
}
_SEVERITIES = {"info", "low", "medium", "high", "critical"}
_CONFIDENCES = {"low", "medium", "high"}
_REMEDIATION_WORKFLOWS: dict[str, set[str]] = {
    "research": {"research", "council_review"},
    "frontend": {"ux_critique", "implement", "review_diff"},
    "backend": {"implement", "review_diff", "refactor_proposal"},
    "security": {"security_review", "review_diff"},
    "qa": {"debug_eval", "review_diff"},
    "pm": {"plan_feature", "release_notes"},
    "ops": {"release_notes", "debug_eval"},
    "custom": {"council_review"},
}


@dataclass(frozen=True)
class ProjectReportDraft:
    summary: str
    coverage: dict
    strengths: list[dict]
    weaknesses: list[dict]
    security_candidates: list[dict]
    fe_be_gaps: list[dict]
    test_gaps: list[dict]
    recommendations: list[dict]
    evidence: list[dict]


def _plain(value: object, *, limit: int) -> str:
    """Accept actual text only, strip control/format characters, redact, then bound."""
    if not isinstance(value, str):
        return ""
    cleaned = "".join(
        character
        for character in value
        if unicodedata.category(character) not in {"Cc", "Cf"}
    )
    collapsed = " ".join(cleaned.split())
    redacted = scan_text(collapsed).redacted_text
    return " ".join(redacted.split())[:limit]


def _evidence(
    value: object,
    *,
    source_ids: set[int],
    logical_paths: set[str],
) -> dict[str, str] | None:
    reference = _plain(value, limit=240)
    if not reference:
        return None
    source_match = _SOURCE_REF.fullmatch(reference)
    if source_match is not None:
        source_id = int(source_match.group(1))
        if source_id in source_ids:
            return {"kind": "source", "ref": str(source_id), "trust": "model_cited"}
        return None
    normalized = reference.replace("\\", "/")
    path = PurePosixPath(normalized)
    if (
        path.is_absolute()
        or _DRIVE_PATH.match(normalized)
        or any(part in {"", ".", ".."} for part in path.parts)
        or normalized not in logical_paths
    ):
        return None
    return {"kind": "path", "ref": normalized, "trust": "model_cited"}


def build_report_draft(
    run: OrchestrationRun,
    snapshot: ProjectSnapshot,
    *,
    host_coverage: dict | None = None,
) -> ProjectReportDraft:
    """Build one safe draft; only accepted project-assessment runs are publishable."""
    if run.project_id != snapshot.project_id:
        raise ValueError("orchestration run and snapshot project mismatch")
    run_config = run.config if isinstance(run.config, dict) else {}
    if run_config.get("team") != "project_intelligence" or run.workflow != "project_assessment":
        raise ValueError("run is not a project assessment")
    if run.status != "ok" or run.verdict != "accept":
        raise ValueError("only accepted project assessments are publishable")
    summary = _plain(run.synthesis_summary, limit=2_000)
    if not summary:
        raise ValueError("accepted project assessment has no summary")

    source_ids = {source.source_id for source in snapshot.sources}
    logical_paths = {source.logical_path for source in snapshot.sources}
    buckets: dict[str, list[dict]] = {name: [] for name in _CATEGORIES.values()}
    retained_ids: set[str] = set()
    evidence_by_key: dict[tuple[str, str], dict] = {}
    dropped = 0
    seen: set[tuple[str, str]] = set()
    raw_findings = run.synthesis_findings if isinstance(run.synthesis_findings, list) else []
    for raw in raw_findings[:20]:
        if not isinstance(raw, dict):
            dropped += 1
            continue
        category = _plain(raw.get("category"), limit=40)
        target = _CATEGORIES.get(category)
        finding_id = _plain(raw.get("finding_id"), limit=40)
        title = _plain(raw.get("finding_title"), limit=160)
        detail = _plain(raw.get("finding"), limit=700)
        member = _plain(raw.get("member"), limit=80)
        pointer = _evidence(
            raw.get("evidence_ref"), source_ids=source_ids, logical_paths=logical_paths
        )
        key = (category, title.casefold())
        if (
            target is None
            or _FINDING_ID.fullmatch(finding_id) is None
            or not title
            or not detail
            or not member
            or pointer is None
            or key in seen
        ):
            dropped += 1
            continue
        severity = _plain(raw.get("severity"), limit=16).lower()
        confidence = _plain(raw.get("confidence"), limit=16).lower()
        row = {
            "finding_id": finding_id,
            "title": title,
            "detail": detail,
            "member": member,
            "severity": severity if severity in _SEVERITIES else "info",
            "confidence": confidence if confidence in _CONFIDENCES else "low",
            "evidence": [pointer],
        }
        if category == "strength":
            row["severity"] = "info"
        if category == "security_candidate":
            row["validated"] = False
            row["validation"] = "candidate"
        buckets[target].append(row)
        retained_ids.add(finding_id)
        evidence_by_key[(pointer["kind"], pointer["ref"])] = pointer
        seen.add(key)

    recommendations: list[dict] = []
    seen_recommendations: set[str] = set()
    raw_actions = run.action_items if isinstance(run.action_items, list) else []
    for raw in raw_actions[:5]:
        if not isinstance(raw, dict):
            continue
        title = _plain(raw.get("title"), limit=160)
        goal = _plain(raw.get("goal"), limit=500)
        key = title.casefold()
        if not title or not goal or key in seen_recommendations:
            continue
        priority = _plain(raw.get("priority"), limit=16).lower()
        row = {
            "title": title,
            "goal": goal,
            "priority": priority if priority in {"low", "medium", "high"} else "medium",
        }
        source_finding_id = _plain(raw.get("source_finding_id"), limit=40)
        if source_finding_id in retained_ids:
            row["source_finding_id"] = source_finding_id
        suggested_team = _plain(raw.get("suggested_team"), limit=40)
        suggested_workflow = _plain(raw.get("suggested_workflow"), limit=60)
        if suggested_workflow in _REMEDIATION_WORKFLOWS.get(suggested_team, set()):
            row["suggested_team"] = suggested_team
            row["suggested_workflow"] = suggested_workflow
        recommendations.append(row)
        seen_recommendations.add(key)

    retained = sum(len(items) for items in buckets.values())
    if retained == 0:
        raise ValueError("accepted assessment has no snapshot-supported findings")
    coverage = dict(host_coverage or {})
    coverage.update(snapshot.coverage)
    coverage["findings_retained"] = retained
    coverage["findings_dropped_unsupported"] = dropped
    return ProjectReportDraft(
        summary=summary,
        coverage=coverage,
        strengths=buckets["strengths"],
        weaknesses=buckets["weaknesses"],
        security_candidates=buckets["security_candidates"],
        fe_be_gaps=buckets["fe_be_gaps"],
        test_gaps=buckets["test_gaps"],
        recommendations=recommendations,
        evidence=[evidence_by_key[key] for key in sorted(evidence_by_key)],
    )
