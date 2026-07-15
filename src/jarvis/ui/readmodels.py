"""Read models for the workstation screens (Phase 8, Task 5).

Every screen is a *view* over an existing service — the UI adds no storage and no new
authority. These functions serialize the domain objects to JSON-safe dicts (deliberately
selecting fields, so nothing sensitive leaks by accident — e.g. a memory's embedding vector
is never shipped, and Hub reports provider **presence booleans only**, never a key value).

The service-backed models take a service/store and are tested against a temp DB with a
``FakeEmbedder``; Hub and Lab are pure over config + files (fully keyless).
"""

from __future__ import annotations

import contextlib
import dataclasses
import datetime as _dt
import json
import math
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any

from jarvis.intelligence.report import recommendation_studio_prefill
from jarvis.memory.store import ANY_PROJECT as _MEM_ANY_PROJECT
from jarvis.persistence.fts import ANY_PROJECT as _ANY_PROJECT
from jarvis.reporting.repo import RepoReader
from jarvis.scheduler.store import ANY_PROJECT as _TASK_ANY_PROJECT

if TYPE_CHECKING:
    from jarvis.agents.store import AgentRun, AgentRunStore
    from jarvis.config import Config
    from jarvis.digest.store import DigestStore
    from jarvis.knowledge.service import KnowledgeService
    from jarvis.memory.service import MemoryService
    from jarvis.memory.store import Memory
    from jarvis.persistence.sessions import SessionMeta, SessionStore
    from jarvis.projects import Project, ProjectService
    from jarvis.scheduler.service import TaskService
    from jarvis.scheduler.store import Task, TaskRun


@dataclass
class UiServices:
    """The services the workstation reads/mutates — all pre-existing, host-composed (Task 9).
    Any may be None (a screen then reports "unavailable" rather than crashing)."""

    memory: MemoryService | None = None
    tasks: TaskService | None = None
    knowledge: KnowledgeService | None = None
    run_store: AgentRunStore | None = None
    # Phase 9: the connector registry and the digest store back the Daily/Hub read models.
    connectors: Any = None
    digests: DigestStore | None = None
    # Phase 10: the session store backs the chats list / search / pin / resume; the project
    # service backs the Projects screen + the active-project switcher; the cost ledger backs
    # the Costs screen + the A5 ledger-degraded status.
    sessions: SessionStore | None = None
    projects: ProjectService | None = None
    ledger: Any = None  # a CostLedger; None when cost tracking isn't composed
    budgets: Any = None  # a BudgetService; None when cost tracking isn't composed
    # Phase 10B: the orchestration run store backs the Studio history + run detail read models.
    orchestration: Any = None  # an OrchestrationStore; None when orchestration isn't composed
    # Snapshot-bound project assessment state remains host-owned and append-only.  These stores
    # are read surfaces only; the coordinator itself lives separately on app.state.
    analysis_jobs: Any = None  # an AnalysisJobStore
    project_reports: Any = None  # a ProjectReportStore
    # Phase 11: the artifact store backs the Artifacts Library + global search + content route.
    artifacts: Any = None  # an ArtifactStore; None when artifacts aren't composed
    # Phase 12: the intent store backs the approval queue; the write journal backs the outbox
    # read model + undo. Both None when the write substrate isn't composed.
    intents: Any = None  # an IntentStore
    write_journal: Any = None  # a ConnectorWriteJournal
    # Phase 15: the graph store backs the memory-graph read models (subgraph / node card /
    # suggestions review); the embedder (shared with memory) backs unified semantic search — None
    # when unavailable, in which case search degrades to keyword (FTS) only.
    graph: Any = None  # a GraphStore
    embedder: Any = None  # an Embedder (memory.embedder); None ⇒ FTS-only search
    # Phase 16: the ONE attention queue (proposals/alerts/reviews). The Notification Center unions
    # this with live approvals + write-intents + graph suggestions at read time.
    attention: Any = None  # an AttentionStore


_PROJECT_REPORT_BUCKETS = (
    "strengths",
    "weaknesses",
    "security_candidates",
    "fe_be_gaps",
    "test_gaps",
)

_EVAL_REPLAY_COMMAND = "uv run kira eval gate --suite core"
_EVAL_SMALL_LIVE_COMMAND = (
    "uv run kira eval gate --suite core --scenario permission_denied --runs 1 "
    "--no-judge --live --max-cost-usd 1.00"
)
_PROJECT_REPORT_COVERAGE = frozenset(
    {
        "files_total",
        "files_reviewed",
        "files_unreviewed",
        "bytes_total",
        "graph_edges",
        "import_edges",
        "files_listed",
        "files_omitted",
        "structure_nodes",
        "structure_edges",
        "dependency_nodes",
        "dependency_edges",
        "dependency_edges_omitted",
        "context_secret_hits",
        "context_truncated",
        "context_chars",
        "findings_retained",
        "findings_dropped_unsupported",
    }
)


def _report_text(value: object, *, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    clean = "".join(
        character
        for character in value
        if unicodedata.category(character) not in {"Cc", "Cf"}
    )
    return " ".join(clean.split())[:limit]


def project_report_counts(report: Any) -> dict[str, int]:
    return {
        "strengths": len(report.strengths),
        "weaknesses": len(report.weaknesses),
        "security_candidates": len(report.security_candidates),
        "frontend_backend_gaps": len(report.fe_be_gaps),
        "test_reliability_gaps": len(report.test_gaps),
        "recommendations": len(report.recommendations),
    }


def _report_evidence(value: object) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    if not isinstance(value, list):
        return out
    for raw in value[:4]:
        if not isinstance(raw, dict) or raw.get("kind") != "path":
            continue
        ref = _report_text(raw.get("ref"), limit=240).replace("\\", "/")
        path = PurePosixPath(ref)
        if (
            not ref
            or path.is_absolute()
            or re.match(r"^[a-zA-Z]:", ref)
            or any(part in {"", ".", ".."} for part in path.parts)
        ):
            continue
        out.append({"kind": "path", "ref": ref, "trust": "model_cited"})
    return out


def _report_findings(value: object, *, security: bool = False) -> list[dict]:
    out: list[dict] = []
    if not isinstance(value, list):
        return out
    for raw in value[:3]:
        if not isinstance(raw, dict):
            continue
        title = _report_text(raw.get("title"), limit=160)
        detail = _report_text(raw.get("detail"), limit=700)
        evidence = _report_evidence(raw.get("evidence"))
        if not title or not detail or not evidence:
            continue
        severity = _report_text(raw.get("severity"), limit=16).lower()
        confidence = _report_text(raw.get("confidence"), limit=16).lower()
        row = {
            "title": title,
            "detail": detail,
            "member": _report_text(raw.get("member"), limit=80),
            "severity": (
                severity
                if severity in {"info", "low", "medium", "high", "critical"}
                else "info"
            ),
            "confidence": confidence if confidence in {"low", "medium", "high"} else "low",
            "evidence": evidence,
        }
        if security:
            row.update({"validated": False, "validation": "candidate"})
        out.append(row)
    return out


def serialize_project_report(report: Any, *, effective_status: str) -> dict:
    """Bounded project-private report view; no snapshot/run/source/local-path identifiers."""
    coverage = {
        key: value
        for key, value in report.coverage.items()
        if key in _PROJECT_REPORT_COVERAGE
        and (type(value) is bool or (type(value) is int and value >= 0))
    }
    recommendations: list[dict] = []
    for index, raw in enumerate(report.recommendations[:5]):
        if not isinstance(raw, dict):
            continue
        title = _report_text(raw.get("title"), limit=160)
        goal = _report_text(raw.get("goal"), limit=500)
        if not title or not goal:
            continue
        priority = _report_text(raw.get("priority"), limit=16).lower()
        recommendations.append(
            {
                "index": index,
                "title": title,
                "goal": goal,
                "priority": priority if priority in {"low", "medium", "high"} else "medium",
                "studio_available": effective_status == "current"
                and recommendation_studio_prefill(report, index) is not None,
            }
        )
    return {
        "id": report.id,
        "status": effective_status,
        "trust_class": "model_generated",
        "created_at": report.created_at,
        "summary": _report_text(report.summary, limit=2_000),
        "coverage": coverage,
        "counts": project_report_counts(report),
        "strengths": _report_findings(report.strengths),
        "weaknesses": _report_findings(report.weaknesses),
        "security_candidates": _report_findings(report.security_candidates, security=True),
        "frontend_backend_gaps": _report_findings(report.fe_be_gaps),
        "test_reliability_gaps": _report_findings(report.test_gaps),
        "recommendations": recommendations,
        "evidence": _report_evidence(report.evidence),
    }


# --- memory ----------------------------------------------------------------


def serialize_memory(memory: Memory) -> dict:
    """A memory row for the Memory screen — WITHOUT the embedding vector (never shipped)."""
    return {
        "id": memory.id,
        "type": memory.type,
        "content": memory.content,
        "source": memory.source,
        "status": memory.status,
        "project_id": memory.project_id,  # Phase 10: scope (None == global)
        "provenance": dataclasses.asdict(memory.provenance),
        "created_at": memory.created_at,
        "access_count": memory.access_count,
    }


async def list_memories(
    memory: MemoryService,
    *,
    type_filter: str | None = None,
    project_id: object = _MEM_ANY_PROJECT,
) -> list[dict]:
    """Live memories, optionally scoped to a project ("what Kira knows about this project"
    = the project's own + global memories). Default is unscoped (every live memory)."""
    rows = await memory.store.all_live(project_id=project_id)
    if type_filter:
        rows = [m for m in rows if m.type == type_filter]
    return [serialize_memory(m) for m in rows]


# --- projects --------------------------------------------------------------


def serialize_project(project: Project) -> dict:
    """A project row for the Projects screen / switcher. Settings are surfaced as-is
    (overrides only — model routes/budgets/roster; never keys, enforced at write time)."""
    return {
        "id": project.id,
        "name": project.name,
        "slug": project.slug,
        "description": project.description,
        "status": project.status,
        "color": project.color,
        "icon": project.icon,
        "repos": list(project.repos),
        "settings": project.settings,
        "pinned": project.pinned,
        "label": project.settings.get("label"),  # Phase 11: user-editable category chip
        "created_at": project.created_at,
        "updated_at": project.updated_at,
    }


async def projects_view(service: ProjectService) -> dict:
    """The Projects screen: active projects + the currently-active scope (id, or None for
    global) so the UI can badge the switcher."""
    rows = await service.store.list(status="active")
    return {
        "projects": [serialize_project(p) for p in rows],
        "active_project_id": service.current().project_id,
    }


async def projects_overview(services: UiServices) -> dict:
    """The Projects grid: active projects (pinned first, then most-recent) each with health chips
    — open tasks, sessions this week, last run status/verdict, month spend — plus the archived
    list (collapsed in the UI). Read-only; every chip degrades to None when its store is absent."""
    if services.projects is None:
        return {"projects": [], "archived": [], "active_project_id": None}
    active = await services.projects.store.list(status="active")
    archived = await services.projects.store.list(status="archived")
    week_ago = (_dt.datetime.now(_dt.UTC) - _dt.timedelta(days=7)).isoformat()
    out: list[dict] = []
    for p in active:
        health: dict = {
            "open_tasks": None,
            "sessions_week": None,
            "last_run": None,
            "month_spend_usd": None,
        }
        # Each chip degrades independently: an absent store leaves it None, and a composed store
        # that errors leaves just that one chip None rather than failing the whole grid.
        with contextlib.suppress(Exception):
            if services.tasks is not None:
                open_tasks = await services.tasks.store.list(project_id=p.id, include_global=False)
                health["open_tasks"] = len(open_tasks)
        with contextlib.suppress(Exception):
            if services.sessions is not None:
                health["sessions_week"] = await services.sessions.count_since(
                    week_ago, project_id=p.id
                )
        with contextlib.suppress(Exception):
            if services.orchestration is not None:
                runs = await services.orchestration.list(project_id=p.id, limit=1)
                if runs:
                    health["last_run"] = {"status": runs[0].status, "verdict": runs[0].verdict}
        with contextlib.suppress(Exception):
            if services.budgets is not None:
                st = await services.budgets.status(project_id=p.id)
                health["month_spend_usd"] = (st.get("month") or {}).get("cost_usd")
        row = serialize_project(p)
        row["health"] = health
        out.append(row)
    out.sort(key=lambda r: r["updated_at"] or "", reverse=True)  # newest first…
    out.sort(key=lambda r: not r["pinned"])  # …then stable pinned-first
    return {
        "projects": out,
        "archived": [serialize_project(p) for p in archived],
        "active_project_id": services.projects.current().project_id,
    }


# --- costs -----------------------------------------------------------------


# S7 Context Reuse: the aggregate cache columns summed over model_calls. Metadata only — token
# counts + estimated savings, never prompt content. NULL cache fields sum as 0 (an aggregate over
# calls, most of which had no cache), which is honest here (unlike a per-call fabricated 0).
_CACHE_SUMS = (
    "COALESCE(SUM(input_tokens),0), COALESCE(SUM(provider_cache_hit_tokens),0), "
    "COALESCE(SUM(cache_write_tokens),0), COALESCE(SUM(cached_input_tokens),0), "
    "COALESCE(SUM(estimated_cache_savings_usd),0.0), COUNT(*)"
)


def _cache_rec(vals: tuple) -> dict:
    inp, hit, write, cached, savings, calls = vals
    return {
        "input_tokens": inp,
        "hit_tokens": hit,
        "cache_write_tokens": write,
        "cached_input_tokens": cached,
        "estimated_savings_usd": savings,
        "calls": calls,
        "hit_rate": round(hit / inp, 4) if inp else 0.0,
    }


async def cache_reuse_overview(
    db: Any, *, project_id: int | None = None, since: str | None = None
) -> dict:
    """Context-reuse rollup over model_calls: aggregate cache-hit / write / cached tokens +
    estimated savings + hit-rate, overall and by provider/model, plus the routes that benefit
    most. Metadata only (token counts + a mode label) — never prompt content."""
    clauses: list[str] = []
    params: list[object] = []
    if project_id is not None:
        clauses.append("project_id = ?")
        params.append(project_id)
    if since is not None:
        clauses.append("ts >= ?")
        params.append(since)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    tot = await db.execute(f"SELECT {_CACHE_SUMS} FROM model_calls {where}", tuple(params))
    totals = _cache_rec(await tot.fetchone())

    async def grouped(col: str) -> list[dict]:  # col is a trusted constant, never caller input
        cur = await db.execute(
            f"SELECT {col}, {_CACHE_SUMS} FROM model_calls {where} GROUP BY {col}", tuple(params)
        )
        return [{col: r[0], **_cache_rec(r[1:])} for r in await cur.fetchall() if r[0] is not None]

    by_provider = await grouped("provider")
    top = sorted(by_provider, key=lambda x: x["estimated_savings_usd"], reverse=True)
    return {
        "totals": totals,
        "by_provider": by_provider,
        "by_model": await grouped("model"),
        "top_routes": top[:5],
    }


def _request_health_summary(
    successes: list[float | None], failures: int, *, telemetry_complete: bool = True
) -> dict:
    """Aggregate safe per-attempt health without treating unknown latency as zero."""
    measured = sorted(
        float(latency)
        for latency in successes
        if isinstance(latency, (int, float)) and not isinstance(latency, bool) and latency >= 0
    )

    def percentile(fraction: float) -> float | None:
        if not measured:
            return None
        return round(measured[math.ceil(fraction * len(measured)) - 1], 2)

    completed = len(successes)
    attempts = completed + failures
    summary = {
        "attempts": attempts,
        "completed_requests": completed,
        "failed_requests": failures,
        "error_rate": round(failures / attempts, 4) if attempts and telemetry_complete else None,
        "measured_completed_latency_requests": len(measured),
        "unmeasured_completed_latency_requests": completed - len(measured),
        "p50_completed_latency_ms": percentile(0.50) if telemetry_complete else None,
        "p95_completed_latency_ms": percentile(0.95) if telemetry_complete else None,
    }
    return summary


async def model_request_health_overview(
    db: Any,
    *,
    project_id: int | None = None,
    since: str | None = None,
    until: str | None = None,
    ledger: Any = None,
) -> dict:
    """Read-only model-request completion/failure health from metadata-only ledgers.

    ``model_calls`` remains the successful-completion ledger; ``model_failures`` is a separate
    stream because failures do not have tokens or a cost.  Completed latency is therefore only
    the provider-measured completion latency, never a fabricated end-to-end turn duration.
    """
    clauses: list[str] = []
    params: list[object] = []
    if project_id is not None:
        clauses.append("project_id = ?")
        params.append(project_id)
    if since is not None:
        clauses.append("ts >= ?")
        params.append(since)
    if until is not None:
        clauses.append("ts < ?")
        params.append(until)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    success_rows = await (
        await db.execute(
            f"SELECT substr(ts, 1, 10), provider, model, latency_ms FROM model_calls {where}",
            tuple(params),
        )
    ).fetchall()
    failure_rows = await (
        await db.execute(
            f"SELECT substr(ts, 1, 10), provider, model, error_class FROM model_failures {where}",
            tuple(params),
        )
    ).fetchall()

    by_route: dict[tuple[str, str], dict[str, Any]] = {}
    by_day: dict[str, dict[str, Any]] = {}
    for day, provider, model, latency_ms in success_rows:
        route = by_route.setdefault((provider, model), {"latencies": [], "failures": 0})
        route["latencies"].append(latency_ms)
        day_row = by_day.setdefault(
            day, {"latencies": [], "failures": 0, "routes": {}, "error_classes": {}}
        )
        day_row["latencies"].append(latency_ms)
        day_route = day_row["routes"].setdefault(
            (provider, model), {"latencies": [], "failures": 0}
        )
        day_route["latencies"].append(latency_ms)
    error_classes: dict[str, int] = {}
    for day, provider, model, error_class in failure_rows:
        route = by_route.setdefault((provider, model), {"latencies": [], "failures": 0})
        route["failures"] += 1
        error_classes[error_class] = error_classes.get(error_class, 0) + 1
        day_row = by_day.setdefault(
            day, {"latencies": [], "failures": 0, "routes": {}, "error_classes": {}}
        )
        day_row["failures"] += 1
        day_route = day_row["routes"].setdefault(
            (provider, model), {"latencies": [], "failures": 0}
        )
        day_route["failures"] += 1
        day_errors = day_row["error_classes"]
        day_errors[error_class] = day_errors.get(error_class, 0) + 1

    failure_generation = 0
    ledger_status = None
    if ledger is not None:
        ledger_status = ledger.status()
        marker = getattr(ledger, "failure_generation", None)
        if callable(marker):
            failure_generation = max(0, int(marker()))
    telemetry_complete = failure_generation == 0
    recording = (
        {
            **(ledger_status or {}),
            # A later successful write clears the live A5 warning, but cannot reconstruct a
            # model request that was lost while SQLite was unavailable.  Keep health fail-closed
            # for this process rather than presenting a falsely exact error/latency metric.
            "telemetry_complete": telemetry_complete,
            "lost_records": failure_generation,
        }
        if ledger is not None
        else None
    )
    routes = [
        {
            "provider": provider,
            "model": model,
            **_request_health_summary(
                row["latencies"], row["failures"], telemetry_complete=telemetry_complete
            ),
        }
        for (provider, model), row in sorted(by_route.items())
    ]
    daily = [
        {
            "day": day,
            "totals": _request_health_summary(
                row["latencies"], row["failures"], telemetry_complete=telemetry_complete
            ),
            "by_provider_model": [
                {
                    "provider": provider,
                    "model": model,
                    **_request_health_summary(
                        route["latencies"],
                        route["failures"],
                        telemetry_complete=telemetry_complete,
                    ),
                }
                for (provider, model), route in sorted(row["routes"].items())
            ],
            "error_classes": [
                {"error_class": error_class, "failed_requests": count}
                for error_class, count in sorted(
                    row["error_classes"].items(), key=lambda item: (-item[1], item[0])
                )
            ],
        }
        for day, row in sorted(by_day.items())
    ]
    return {
        "period": {"since": since, "until": until, "timezone": "UTC"},
        "totals": _request_health_summary(
            [row[3] for row in success_rows],
            len(failure_rows),
            telemetry_complete=telemetry_complete,
        ),
        "by_provider_model": routes,
        "by_day": daily,
        "error_classes": [
            {"error_class": error_class, "failed_requests": count}
            for error_class, count in sorted(
                error_classes.items(), key=lambda item: (-item[1], item[0])
            )
        ],
        "recording_degraded": recording,
    }


async def costs_overview(
    budgets: Any, *, project_id: int | None = None, projects: Any = None, ledger: Any = None
) -> dict:
    """The Costs screen: today/week/month spend + limits + the 'why this cost' breakdown (by
    purpose, role, model, provider, stage, team, service). Unpriced calls/services are surfaced
    separately, never summed as $0. A global view (project_id None) also attributes spend BY
    project (ids resolved to names) and computes a monthly-cap warning level (ok/soft/hard)."""
    status = await budgets.status(project_id=project_id)
    month_start = _period_start_iso("month")
    overview: dict = {
        **status,
        "by_purpose": await budgets.grouped("purpose", project_id=project_id, since=month_start),
        "by_role": await budgets.grouped("agent_role", project_id=project_id, since=month_start),
        "by_model": await budgets.grouped("model", project_id=project_id, since=month_start),
        "by_provider": await budgets.grouped("provider", project_id=project_id, since=month_start),
        "by_stage": await budgets.grouped("stage", project_id=project_id, since=month_start),
        "by_team": await budgets.grouped("team", project_id=project_id, since=month_start),
        "by_service": await budgets.grouped_services(
            "service", project_id=project_id, since=month_start
        ),
        # S7: prompt/context-cache reuse this month (aggregate; empty until caching is enabled).
        "context_reuse": await cache_reuse_overview(
            budgets.db, project_id=project_id, since=month_start
        ),
        "model_request_health": await model_request_health_overview(
            budgets.db, project_id=project_id, since=month_start, ledger=ledger
        ),
    }
    if project_id is None:  # the global cost center also attributes spend by project
        rows = await budgets.grouped("project_id", since=month_start)
        names = {p.id: p.name for p in await projects.store.list()} if projects is not None else {}
        for row in rows:
            pid = row.get("project_id")
            row["project"] = names.get(pid) or ("Global" if pid is None else f"#{pid}")
        overview["by_project"] = rows
    month_spend = (status.get("month") or {}).get("cost_usd") or 0.0
    cap = (status.get("limits") or {}).get("project_monthly_usd")
    level = "ok"
    if cap:
        level = "hard" if month_spend >= cap else "soft" if month_spend >= 0.8 * cap else "ok"
    overview["budget_warning"] = {"level": level, "month_spend_usd": month_spend, "cap_usd": cap}
    return overview


def serialize_artifact(a) -> dict:
    """An artifact row for the Library / search / workspace — metadata only. The raw local_path
    is deliberately NOT shipped (an internal path); `has_content` tells the UI whether the
    /content route will serve a file."""
    return {
        "id": a.id,
        "project_id": a.project_id,
        "kind": a.kind,
        "title": a.title,
        "origin_type": a.origin_type,
        "origin_id": a.origin_id,
        "external_uri": a.external_uri,
        "has_content": a.local_path is not None,
        "created_by": a.created_by,
        "team": a.team,
        "role": a.role,
        "model": a.model,
        "sensitivity": a.sensitivity,
        "provenance_class": a.provenance_class,
        "content_hash": a.content_hash,
        "labels": list(a.labels),
        "pinned": a.pinned,
        "created_at": a.created_at,
        "updated_at": a.updated_at,
    }


async def artifacts_list(
    store: Any,
    *,
    project_id: int | None = None,
    include_global: bool = True,
    kind: str | None = None,
    pinned: bool | None = None,
    limit: int = 50,
) -> dict:
    """The Artifacts Library: newest-first, pinned surfaced first. A None project_id means the
    global library (all projects); a concrete id scopes to that project (+ global)."""
    scope: object = _ANY_PROJECT if project_id is None else project_id
    rows = await store.list(
        project_id=scope, include_global=include_global, kind=kind, pinned=pinned, limit=limit
    )
    return {"artifacts": [serialize_artifact(a) for a in rows]}


async def workspace_overview(services: UiServices, project_id: int) -> dict:
    """The Project Workspace Overview tab: the project + a few health chips + recent artifacts +
    recent runs, scoped to the project. Read-only; each piece degrades if its service is off."""
    out: dict = {
        "project_id": project_id,
        "project": None,
        "recent_artifacts": [],
        "recent_runs": [],
        "health": {},
    }
    if services.projects is not None:
        p = await services.projects.store.get(project_id)
        out["project"] = serialize_project(p) if p is not None else None
    if services.artifacts is not None:
        arts = await services.artifacts.list(project_id=project_id, include_global=False, limit=10)
        out["recent_artifacts"] = [serialize_artifact(a) for a in arts]
    if services.orchestration is not None:
        runs = await services.orchestration.list(project_id=project_id, limit=5)
        out["recent_runs"] = [serialize_orchestration_run(r) for r in runs]
    if services.budgets is not None:
        st = await services.budgets.status(project_id=project_id)
        out["health"]["month_spend_usd"] = (st.get("month") or {}).get("cost_usd")
    return out


async def activity_feed(services: UiServices, project_id: int, *, limit: int = 30) -> dict:
    """The Workspace Activity tab: a derived, METADATA-ONLY, time-ordered feed of what happened in
    this project — artifacts filed, orchestration runs, and chats. Titles only (never bodies or
    secrets); this is the replayable substrate the Phase-14 office view will render. Each source
    degrades independently."""
    events: list[dict] = []
    with contextlib.suppress(Exception):
        if services.artifacts is not None:
            for a in await services.artifacts.list(
                project_id=project_id, include_global=False, limit=limit
            ):
                events.append(
                    {
                        "type": "artifact",
                        "title": a.title,
                        "kind": a.kind,
                        "ts": a.created_at,
                        "ref_id": a.id,
                    }
                )
    with contextlib.suppress(Exception):
        if services.orchestration is not None:
            for r in await services.orchestration.list(project_id=project_id, limit=limit):
                events.append(
                    {
                        "type": "run",
                        "title": r.title or r.workflow,
                        "status": r.status,
                        "ts": r.finished_at or r.started_at,
                        "ref_id": r.id,
                    }
                )
    with contextlib.suppress(Exception):
        if services.sessions is not None:
            for m in await services.sessions.list_sessions(project_id=project_id, limit=limit):
                events.append(
                    {"type": "chat", "title": m.title, "ts": m.updated_at, "ref_id": m.id}
                )
    events = [e for e in events if e.get("ts")]
    events.sort(key=lambda e: e["ts"], reverse=True)
    return {"events": events[:limit], "project_id": project_id}


#: The canonical orchestration stage machine the Office stage-map renders (Phase 14). The head
#: reviewer (Fable, planner route) owns synthesis + the final verdict — an engine stage, not a room.
_OFFICE_STAGES = ("council", "synthesis", "execution", "review", "verdict")


def _office_nodes(
    members: list[dict], member_runs: list[dict], routes: dict, svc_state: dict
) -> list[dict]:
    """Build a room's member nodes: the static roster (model/provider/tools/services derived from
    the route + service catalogs) overlaid, when a run is live/recent, with each member's live
    stage/status/cost/iterations. Overlay is matched per route_role, consuming member_runs in order
    so duplicate roles map deterministically. Metadata only (member_runs are bodies-free)."""
    by_role: dict[str, list[dict]] = {}
    for mr in member_runs:
        by_role.setdefault(mr.get("role"), []).append(mr)
    nodes: list[dict] = []
    for m in members:
        route = routes.get(m["route_role"], {})
        queue = by_role.get(m["route_role"])
        mr = queue.pop(0) if queue else None
        svcs = [{"name": s, "state": svc_state.get(s, "unknown")} for s in m["services"]]
        nodes.append(
            {
                "member_id": m["id"],
                "title": m["title"],
                "role": m["route_role"],
                "capability": m["capability"],
                "model": route.get("model"),
                "provider": route.get("provider"),
                "tools": m["tools"],
                "services": svcs,
                "stage": mr.get("stage") if mr else None,
                "status": mr.get("status") if mr else "idle",
                "cost_usd": mr.get("cost_usd") if mr else None,
                "iterations": mr.get("iterations") if mr else None,
            }
        )
    return nodes


async def office_overview(
    config: Config, services: UiServices, project_id: int, *, limit: int = 30
) -> dict:
    """The AI Team Office (Phase 14): a pure ASSEMBLER over existing read models — teams as rooms
    of member nodes, the head reviewer (Fable), the canonical stage map, the latest run's live
    summary + per-member overlay, recent runs, and the metadata-only activity feed. No new storage;
    the client patches further live updates from the orchestration WS bus on top. Presence /
    metadata / short summaries ONLY — never a prompt, report body, or key value (secret-swept).
    Each source degrades independently: a missing service ⇒ idle rooms / empty feed, not a crash."""
    routes = {r["role"]: r for r in model_routes_status(config)}
    svc_state = {s["name"]: s["state"] for s in services_status(config)}
    hr = routes.get("planner") or {}
    head = {"label": "Fable", "model": hr.get("model"), "provider": hr.get("provider")}

    live: dict | None = None
    overlay: dict[str, list[dict]] = {}
    if services.orchestration is not None:
        with contextlib.suppress(Exception):
            runs = await services.orchestration.list(project_id=project_id, limit=1)
            if runs:
                live = serialize_orchestration_run(runs[0])
                if services.run_store is not None:
                    overlay[live["team"]] = await services.run_store.member_runs(runs[0].id)

    rooms = [
        {
            "team": team["id"],
            "name": team["name"],
            "icon": team["icon"],
            "accent": team["color"],
            "description": team["description"],
            "nodes": _office_nodes(team["members"], overlay.get(team["id"], []), routes, svc_state),
        }
        for team in teams_catalog()
    ]

    recent: list[dict] = []
    if services.orchestration is not None:
        with contextlib.suppress(Exception):
            view = await orchestration_runs_view(
                services.orchestration, project_id=project_id, limit=10
            )
            recent = view["runs"]
    feed = (await activity_feed(services, project_id, limit=limit))["events"]
    return {
        "project_id": project_id,
        "head": head,
        "stages": list(_OFFICE_STAGES),
        "rooms": rooms,
        "live": live,
        "recent_runs": recent,
        "feed": feed,
    }


def _orchestration_outcome(status: str, verdict: str | None) -> str:
    """Stable display/accounting outcome derived from the persisted terminal state."""
    if status == "ok":
        if verdict == "accept":
            return "review_accepted"
        if verdict == "reject":
            return "review_rejected"
        if verdict == "revise":
            return "needs_revision"
        return "completed_unreviewed"
    if status == "rejected":
        return "review_rejected"
    if status == "revise":
        return "needs_revision"
    if status == "error":
        return "failed"
    if status in {"cancelled", "aborted", "budget_stopped"}:
        return status
    if status == "running":
        return "in_progress"
    return "unknown"


def _outcome_roi(
    budgets: Any, *, baseline_minutes: int, actual_cost_usd: float | None, outcome: str
) -> dict:
    """Credit time-saved value only after the reviewer has accepted the run."""
    if outcome == "review_accepted":
        return budgets.roi(baseline_minutes, actual_cost_usd)
    return {
        "baseline_minutes": baseline_minutes,
        "value_usd": None,
        "actual_cost_usd": actual_cost_usd,
        "net_usd": None,
    }


def orchestration_outcome_accounting(rows: list[dict]) -> dict:
    """Terminal-run model-cost accounting for the Cost Center.

    ``actual_cost_usd`` is populated from the orchestration model-call ledger only; it excludes
    service estimates.  Any terminal run with unknown actual cost makes the cost-per-accepted-run
    metric unknown rather than presenting an incomplete cohort as a precise result.
    """
    from jarvis.orchestration.store import TERMINAL

    terminal = [row for row in rows if row.get("status") in TERMINAL]
    known_costs = [
        float(row["actual_cost_usd"])
        for row in terminal
        if row.get("actual_cost_usd") is not None
    ]
    accepted = sum(row.get("outcome") == "review_accepted" for row in terminal)
    unknown = len(terminal) - len(known_costs)
    known_total = round(sum(known_costs), 4)
    return {
        "completed_runs": len(terminal),
        "review_accepted_runs": accepted,
        "known_actual_model_cost_usd": known_total,
        "unknown_actual_model_cost_runs": unknown,
        "known_model_cost_per_review_accepted_run": (
            round(known_total / accepted, 4) if accepted and not unknown else None
        ),
    }


def _calibration_percentile(values: list[float], fraction: float) -> float | None:
    """Nearest-rank percentile for a small, append-only run sample."""
    if not values:
        return None
    ordered = sorted(values)
    return round(ordered[math.ceil(fraction * len(ordered)) - 1], 4)


async def orchestration_estimate_accuracy(
    store: Any, *, project_id: int | None = None, limit: int = 200
) -> dict:
    """Read-only estimate calibration from terminal orchestration runs.

    This deliberately measures historical model-cost accuracy only: it does not tune prices,
    routing, or budgets. A row is comparable only with a known non-negative actual cost and a
    positive estimate; unknown and zero estimates stay visible instead of becoming fake zeroes.
    """
    from jarvis.orchestration.store import TERMINAL

    runs = await store.list(project_id=project_id, limit=limit)
    terminal = [run for run in runs if run.status in TERMINAL]
    comparable: list[tuple[float, float]] = []
    unknown_actual = 0
    missing_estimate = 0
    zero_or_invalid_estimate = 0
    for run in terminal:
        estimated = run.estimated_cost_usd
        actual = run.actual_cost_usd
        if not isinstance(actual, (int, float)) or isinstance(actual, bool) or actual < 0:
            unknown_actual += 1
            continue
        if not isinstance(estimated, (int, float)) or isinstance(estimated, bool):
            missing_estimate += 1
            continue
        if estimated <= 0:
            zero_or_invalid_estimate += 1
            continue
        comparable.append((float(estimated), float(actual)))

    estimated_total = sum(estimate for estimate, _actual in comparable)
    actual_total = sum(actual for _estimate, actual in comparable)
    ratios = [actual / estimate for estimate, actual in comparable]
    return {
        "sample_limit": limit,
        "terminal_runs": len(terminal),
        "comparable_runs": len(comparable),
        "unknown_actual_cost_runs": unknown_actual,
        "missing_estimate_runs": missing_estimate,
        "zero_or_invalid_estimate_runs": zero_or_invalid_estimate,
        "estimated_cost_usd": round(estimated_total, 4),
        "actual_cost_usd": round(actual_total, 4),
        "delta_usd": round(actual_total - estimated_total, 4),
        "actual_to_estimate_ratio": (
            round(actual_total / estimated_total, 4) if estimated_total else None
        ),
        "p50_actual_to_estimate_ratio": _calibration_percentile(ratios, 0.50),
        "p95_actual_to_estimate_ratio": _calibration_percentile(ratios, 0.95),
        "underestimated_runs": sum(ratio > 1 for ratio in ratios),
        "overestimated_runs": sum(ratio < 1 for ratio in ratios),
    }


async def orchestration_roi(
    store: Any, budgets: Any, *, project_id: int | None = None, limit: int = 20
) -> list[dict]:
    """Per-run ROI with outcome-gated value for the Studio and Cost Center surfaces."""
    from jarvis.orchestration import WORKFLOWS

    runs = await store.list(project_id=project_id, limit=limit)
    out: list[dict] = []
    for r in runs:
        wf = WORKFLOWS.get(r.workflow)
        if wf is None:
            continue
        outcome = _orchestration_outcome(r.status, r.verdict)
        roi = _outcome_roi(
            budgets,
            baseline_minutes=wf.baseline_minutes,
            actual_cost_usd=r.actual_cost_usd,
            outcome=outcome,
        )
        out.append(
            {
                "run_id": r.id,
                "team": r.config.get("team"),
                "workflow": r.workflow,
                "status": r.status,
                "verdict": r.verdict,
                "outcome": outcome,
                **roi,
            }
        )
    return out


def _period_start_iso(period: str) -> str:
    from jarvis.observability.budget import _local_now, _period_start

    return _period_start(_local_now(), period).astimezone(_dt.UTC).isoformat()


# --- sessions (chats) ------------------------------------------------------


def serialize_session_meta(meta: SessionMeta) -> dict:
    """A chat summary — metadata only, no message bodies. ``reflected`` is a boolean (the
    timestamp itself isn't useful to the UI)."""
    return {
        "id": meta.id,
        "title": meta.title,
        "kind": meta.kind,
        "project_id": meta.project_id,
        "pinned": meta.pinned,
        "created_at": meta.created_at,
        "updated_at": meta.updated_at,
        "reflected": meta.reflected_at is not None,
        "message_count": meta.message_count,
    }


async def list_sessions_view(
    sessions: SessionStore,
    *,
    query: str | None = None,
    pinned: bool | None = None,
    project_id: int | None = None,
    scope_project: bool = False,
    limit: int = 50,
) -> dict:
    """The chats list (or a search over titles + message text). Interactive sessions only.
    ``project_id`` scopes to one project's chats (the Workspace Chats tab); absent ⇒ every chat
    (the legacy/global list). ``scope_project`` preserves the distinction between an omitted
    project (all chats) and a live workspace deliberately scoped to Global (``project_id=None``)."""
    scope = {"project_id": project_id} if scope_project or project_id is not None else {}
    if query:
        rows = await sessions.search_sessions(query, limit=limit, **scope)
    else:
        rows = await sessions.list_sessions(pinned=pinned, limit=limit, **scope)
    return {"sessions": [serialize_session_meta(m) for m in rows]}


def _message_text(content: object) -> str:
    """Render one stored message's content to display text: plain string, or the text
    blocks of a block list, with tool calls noted compactly (tool *results* are plumbing
    and are dropped from the human-readable transcript)."""
    if isinstance(content, str):
        return content
    parts: list[str] = []
    if isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                parts.append(str(block.get("text", "")))
            elif block.get("type") == "tool_use":
                parts.append(f"[used {block.get('name')}]")
    return "\n".join(p for p in parts if p)


_DELEGATION_STATUSES = frozenset({"running", "ok", "error", "timeout", "cancelled", "aborted"})


def _delegation_label(value: object) -> str:
    """A bounded, display-only delegation title — never a prompt or child report."""
    if not isinstance(value, str):
        return "sub-agent"
    label = " ".join(value.split())
    if not label:
        return "sub-agent"
    return label if len(label) <= 120 else f"{label[:119].rstrip()}…"


def serialize_session_delegation(run: AgentRun) -> dict[str, str]:
    """The terminal lifecycle summary a parent transcript may safely rehydrate.

    This deliberately does not expose the child prompt, result, error, trace/session identifiers,
    tool payloads, or child transcript.  A row proves a delegation was recorded for this parent
    chat; it does not recreate the child's live tool/text timeline.
    """
    return {
        "agent_id": str(run.id),
        "title": _delegation_label(run.title),
        "status": run.status if run.status in _DELEGATION_STATUSES else "aborted",
    }


async def session_transcript(
    sessions: SessionStore, session_id: int, *, run_store: AgentRunStore | None = None
) -> dict:
    """One chat's transcript for the history view — the user's own conversation, rendered
    to {role, text} (no tool-result plumbing), plus terminal-only summaries of delegations
    recorded for that exact parent session. ``ok: False`` if the session is unknown."""
    meta = await sessions.get_meta(session_id)
    if meta is None:
        return {"ok": False, "message": "no such session"}
    messages = await sessions.load_messages(session_id)
    rendered = [
        {"role": m.get("role"), "text": text}
        for m in messages
        if (text := _message_text(m.get("content")))
    ]
    delegations = (
        [
            serialize_session_delegation(run)
            for run in await run_store.list(parent_session_id=session_id)
        ]
        if run_store is not None
        else []
    )
    return {
        "ok": True,
        "session": serialize_session_meta(meta),
        "messages": rendered,
        "delegations": delegations,
    }


# --- tasks -----------------------------------------------------------------


def serialize_task(task: Task) -> dict:
    return dataclasses.asdict(task)


def serialize_task_run(run: TaskRun) -> dict:
    return dataclasses.asdict(run)


async def list_tasks(
    tasks: TaskService, *, include_finished: bool = True, project_id: object = _TASK_ANY_PROJECT
) -> list[dict]:
    """Tasks for the Tasks screen / a project page. ``project_id`` scopes to a project
    (P + global); the default is unscoped (every task). Project A's tasks (project_id=A)
    never appear when scoped to project B."""
    rows = await tasks.store.list(include_finished=include_finished, project_id=project_id)
    return [serialize_task(t) for t in rows]


async def task_runs(tasks: TaskService, task_id: int, *, limit: int = 20) -> list[dict]:
    return [serialize_task_run(r) for r in await tasks.store.runs_for(task_id, limit=limit)]


# --- knowledge / vault -----------------------------------------------------


def serialize_source(source) -> dict:
    """A KB source's provenance for the Vault list (no file bytes — paths + metadata only)."""
    return {
        "id": source.id,
        "kind": source.kind,
        "origin": source.origin,
        "title": source.title,
        "status": source.status,
        "review_status": source.review_status,
        "created_by": source.created_by,
        "created_at": source.created_at,
        "byte_size": source.byte_size,
        "mime": source.mime,
        "markdown_path": source.markdown_path,
    }


def serialize_chat_file(source) -> dict:
    """Inert metadata for the active chat's Files shelf.

    Unlike the Vault inspector, this intentionally omits origin and managed paths.  The browser
    only needs a recognisable title, type, size, review state, and time — never local storage
    topology or raw document bytes.
    """
    return {
        "id": source.id,
        "title": source.title,
        "kind": source.kind,
        "mime": source.mime,
        "byte_size": source.byte_size,
        "review_status": source.review_status,
        "created_at": source.created_at,
    }


async def vault_overview(
    knowledge: KnowledgeService, *, project_id: int | None = None, graph: Any = None
) -> dict:
    """Vault counts plus a bodies-free readiness view for one active project.

    The readiness projection is deliberately metadata-only: it proves that semantic retrieval and
    the derived local code graph have material to use, without returning a source body, graph
    evidence, managed path, or a cross-project count.
    """
    if project_id is None:
        stats = await knowledge.stats()
    else:
        # Workspace Vault renders these beside project-only readiness/tree data. Keep the count
        # local too, so a project tab cannot silently present a global knowledge total as its own.
        live_sources = await knowledge.store.list_sources(status="live", project_id=project_id)
        unreviewed_sources = await knowledge.store.list_sources(
            review_status="unreviewed", project_id=project_id
        )
        cursor = await knowledge.store.db.execute(
            "SELECT COUNT(*) FROM kb_chunks c JOIN kb_sources s ON s.id=c.source_id "
            "WHERE s.project_id=? AND s.status='live'",
            (project_id,),
        )
        (chunks,) = await cursor.fetchone()
        stats = {
            "sources": len(live_sources),
            "unreviewed": len(unreviewed_sources),
            "chunks": int(chunks),
        }
    unreviewed = await (
        knowledge.unreviewed_sources(project_id=project_id)
        if project_id is not None
        else knowledge.unreviewed_sources()
    )
    items = []
    for s in unreviewed:
        entry = serialize_source(s)
        # A capped markdown preview so approving a quarantined source is INFORMED, not blind.
        entry["preview"] = await knowledge.source_markdown(s.id, max_chars=1200)
        items.append(entry)
    readiness = None
    if project_id is not None:
        rows = await knowledge.store.list_sources(status="live", project_id=project_id)
        reviewed_source_ids = {
            source.id for source in rows if source.review_status == "reviewed"
        }
        cursor = await knowledge.store.db.execute(
            "SELECT COUNT(*) FROM kb_chunks c JOIN kb_sources s ON s.id=c.source_id "
            "WHERE s.project_id=? AND s.status='live' AND s.review_status='reviewed'",
            (project_id,),
        )
        (indexed_chunks,) = await cursor.fetchone()
        edge_counts: dict[str, int] = {}
        if graph is not None:
            for edge in await graph.list_edges(
                project_id=project_id, include_global=False, origin="derived"
            ):
                if edge.edge_kind == "imports":
                    try:
                        if (
                            int(edge.src_id) not in reviewed_source_ids
                            or int(edge.dst_id) not in reviewed_source_ids
                        ):
                            continue
                    except (TypeError, ValueError):
                        # A malformed derived import is not verified evidence for readiness.
                        continue
                edge_counts[edge.edge_kind] = edge_counts.get(edge.edge_kind, 0) + 1
        source_count = len(rows)
        imports = edge_counts.get("imports", 0)
        readiness = {
            "project_id": project_id,
            "sources": source_count,
            "indexed_chunks": int(indexed_chunks),
            "graph_available": graph is not None,
            "folder_links": edge_counts.get("contains", 0),
            "import_links": imports,
            "ready": source_count > 0 and int(indexed_chunks) > 0,
            "detail": (
                "Relevant sections and verified local dependencies are available to project chat."
                if source_count and int(indexed_chunks) and imports
                else (
                    "Relevant file sections are indexed; no local imports were resolved yet."
                    if source_count and int(indexed_chunks)
                    else (
                        "Project files are awaiting review or could not be indexed; "
                        "chat will not use them yet."
                        if source_count
                        else "Add project files to make project chat grounded in your work."
                    )
                )
            ),
        }
    return {
        "stats": stats,
        "unreviewed": items,
        "project_id": project_id,
        "project_readiness": readiness,
    }


async def vault_lint(knowledge: KnowledgeService) -> dict:
    report = await knowledge.lint()
    return dataclasses.asdict(report)


# --- agents (Trace) --------------------------------------------------------


def serialize_agent_run(run: AgentRun) -> dict:
    """Metadata-only delegation history for the Notifications screen.

    The audit row contains prompts, results, errors, and trace identifiers for local debug
    tooling. None of those belong in this browser projection: they can carry untrusted text or
    become cross-surface correlation identifiers. Keep this allowlist intentionally narrow.
    """
    return {
        "id": run.id,
        "title": run.title,
        "status": run.status,
        "tools_scope": run.tools_scope,
        "iterations": run.iterations,
        "denied_count": run.denied_count,
        "cost_usd": run.cost_usd,
        "started_at": run.started_at,
    }


async def list_agent_runs(
    run_store: AgentRunStore, *, project_id: int | None = None, limit: int = 50
) -> list[dict]:
    return [
        serialize_agent_run(r)
        for r in await run_store.list(project_id=project_id, limit=limit)
    ]


# --- orchestration (Studio): runs + team/workflow catalog (metadata only) ---


_SKILL_HASH_RE = re.compile(r"[0-9a-f]{12}\Z")
_SKILL_TEXT_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}\Z")
_SKILL_MANIFEST_KEYS = (
    "pack",
    "version",
    "sha256",
    "compiled_sha256",
    "member",
    "stage",
)


def recorded_skills_manifest(raw: Any) -> list[dict[str, str]]:
    """Fail-closed projection of persisted Skill Forge audit metadata.

    Run and member rows are historical JSON, so they are not trusted merely because they live in
    our database.  Studio receives only the six bounded identifiers emitted by
    :class:`~jarvis.skills.catalog.SkillCatalog`; pack text, unknown keys, and malformed entries
    are discarded.  A manifest records resolution at run start, not prompt injection.
    """
    if not isinstance(raw, list):
        return []
    projected: list[dict[str, str]] = []
    for entry in raw[:64]:
        if not isinstance(entry, dict):
            continue
        values = {key: entry.get(key) for key in _SKILL_MANIFEST_KEYS}
        valid_text = all(
            isinstance(value, str) and _SKILL_TEXT_RE.fullmatch(value)
            for value in values.values()
        )
        if (
            not valid_text
            or not _SKILL_HASH_RE.fullmatch(values["sha256"])
            or not _SKILL_HASH_RE.fullmatch(values["compiled_sha256"])
        ):
            continue
        projected.append(values)
    return projected


def serialize_orchestration_run(run: Any, *, include_skills_manifest: bool = False) -> dict:
    """One orchestration run for the Studio history/detail. Summary + manifest + costs only —
    the store never holds a verbatim prompt or child report, so nothing sensitive is here."""
    serialized = {
        "id": run.id,
        "project_id": run.project_id,
        "workflow": run.workflow,
        "title": run.title,
        "team": run.config.get("team"),
        "status": run.status,
        "stage": run.stage,
        "verdict": run.verdict,
        "synthesis_summary": run.synthesis_summary,
        "verdict_rationale": run.verdict_rationale,
        "synthesis_findings": run.synthesis_findings,
        # Inert, bounded head-synthesis follow-ups. They are deliberately distinct from
        # Scheduler tasks: no schedule, payload, execution path, or new authority crosses here.
        "action_items": run.action_items,
        "estimated_cost_usd": run.estimated_cost_usd,
        "actual_cost_usd": run.actual_cost_usd,
        "budget_usd": run.budget_usd,
        "context_manifest": run.context_manifest,  # refs/hashes/token-est only (bodies-free)
        # Deliberately expose only eligibility.  The bounded synthesis checkpoint itself never
        # leaves the store through a read model, just as prompts and child reports never do.
        "can_resume": run.status == "aborted" and run.resume_state == "ready",
        "started_at": run.started_at,
        "finished_at": run.finished_at,
    }
    # This evidence belongs only in the expanded Studio detail.  Run lists, Workspace, Office,
    # and `/api/agents` remain compact metadata surfaces.
    if include_skills_manifest:
        serialized["skills_manifest"] = recorded_skills_manifest(run.skills_manifest)
    return serialized


async def orchestration_runs_view(
    store: Any, *, project_id: int | None = None, limit: int = 50
) -> dict:
    runs = await store.list(project_id=project_id, limit=limit)
    return {"runs": [serialize_orchestration_run(r) for r in runs]}


async def orchestration_run_detail(
    store: Any, run_store: Any, run_id: int, *, budgets: Any = None
) -> dict:
    """A run + its per-member metadata (role/stage/status/cost — never bodies) + (when the
    budget service is composed) the per-run cost breakdown by role/stage/service and the run's
    ROI. ``run_store`` (the AgentRunStore) may be None ⇒ members omitted."""
    run = await store.get(run_id)
    if run is None:
        return {"run": None, "members": []}
    members = await run_store.member_runs(run_id) if run_store is not None else []
    # Model-call rows are bodies-free accounting metadata.  They let the results surface say
    # which route actually participated without reconstructing a prompt or child transcript.
    db = getattr(budgets, "db", None) if budgets is not None else None
    if db is not None and members:
        cursor = await db.execute(
            "SELECT agent_role, stage, provider, model FROM model_calls "
            "WHERE orchestration_run_id=? AND agent_role IS NOT NULL AND model IS NOT NULL "
            "ORDER BY id",
            (run_id,),
        )
        models: dict[tuple[str | None, str | None], list[str]] = {}
        for role, stage, provider, model in await cursor.fetchall():
            key = (role, stage)
            label = " · ".join(part for part in (provider, model) if part)
            if label and label not in models.setdefault(key, []):
                models[key].append(label)
        for member in members:
            member["models"] = models.get((member.get("role"), member.get("stage")), [])
    for member in members:
        member["skills_manifest"] = recorded_skills_manifest(member.get("skills_manifest"))
    detail = {
        "run": serialize_orchestration_run(run, include_skills_manifest=True),
        "members": members,
    }
    if budgets is not None:
        from jarvis.orchestration import WORKFLOWS

        detail["cost_breakdown"] = await budgets.run_breakdown(run_id)
        wf = WORKFLOWS.get(run.workflow)
        if wf is not None:
            outcome = _orchestration_outcome(run.status, run.verdict)
            detail["roi"] = {
                "outcome": outcome,
                **_outcome_roi(
                    budgets,
                    baseline_minutes=wf.baseline_minutes,
                    actual_cost_usd=run.actual_cost_usd,
                    outcome=outcome,
                ),
            }
    return detail


def serialize_team(team: Any) -> dict:
    """A team profile for the Studio roster cards. Code-constant metadata: each member's role,
    tools, services, capability, and output — no secrets, no runtime state."""
    return {
        "id": team.id,
        "name": team.name,
        "description": team.description,
        "icon": team.icon,
        "color": team.color,
        "default_workflows": list(team.default_workflows),
        "team_budget_usd": team.team_budget_usd,
        "members": [
            {
                "id": m.id,
                "title": m.title,
                "route_role": m.route_role,
                "capability": m.capability.value,
                "tools": sorted(m.tools),
                "services": sorted(m.services),
                "output": m.output,
                "max_cost_usd": m.max_cost_usd,
            }
            for m in team.members
        ],
    }


def teams_catalog() -> list[dict]:
    """All fixed team profiles (code constants) for the Studio team picker."""
    from jarvis.orchestration import TEAM_PROFILES

    return [serialize_team(t) for t in TEAM_PROFILES.values()]


def workflows_catalog() -> list[dict]:
    """All workflow templates (code constants): stages + ROI baseline minutes."""
    from jarvis.orchestration import WORKFLOWS

    return [
        {
            "id": w.id,
            "title": w.title,
            "stages": [{"name": s.name, "kind": s.kind} for s in w.stages],
            "baseline_minutes": w.baseline_minutes,
            "has_execution": any(s.kind == "execution" for s in w.stages),
        }
        for w in WORKFLOWS.values()
    ]


# --- hub: connector status (PRESENCE BOOLEANS ONLY — never a key value) -----


def _empty_connectors() -> dict:
    return {"demo": False, "google": None, "notifiers": {}}


_GOOGLE_SCOPE_LABELS = {
    "https://www.googleapis.com/auth/calendar.readonly": "Read calendar events",
    "https://www.googleapis.com/auth/calendar.events": "Create and update calendar events",
    "https://www.googleapis.com/auth/gmail.readonly": "Read Gmail",
    "https://www.googleapis.com/auth/gmail.compose": "Create and update Gmail drafts",
    "https://www.googleapis.com/auth/drive.readonly": "Read Drive files",
    "https://www.googleapis.com/auth/drive.file": "Create and update Kira-created Docs",
}

_PROVIDER_LABELS = {
    "anthropic": "Anthropic",
    "openai": "OpenAI",
    "gemini": "Gemini",
    "qwen": "Qwen",
    "deepseek": "DeepSeek",
    "zai": "Z.ai",
}


def _hub_state(value: str) -> str:
    """Normalize catalog state into the small, user-facing Hub vocabulary.

    The returned values are display labels only. They deliberately do not surface a provider
    exception, token metadata beyond the existing safe snapshot, or the name/value of a secret.
    """
    return {
        "missing_credentials": "missing_key",
        "not_configured": "disabled",
        "unpriced": "deferred",
    }.get(value, value)


def connector_hub_overview(config: Config, *, connectors: dict | None = None) -> dict:
    """Actionable, read-only connector status for the Hub.

    This is intentionally an assembler over configuration presence and ``ConnectorRegistry``'s
    existing safe snapshot. It does not probe an account, read a token file directly, initiate
    OAuth, or add an execution path. Secret values, recipient/chat IDs, provider bodies, and
    token values are excluded structurally.
    """
    from urllib.parse import urlsplit, urlunsplit

    from jarvis.config import resolve_kakao_redirect_uri, resolve_telegram_chat_id

    snapshot = connectors or _empty_connectors()
    google = snapshot.get("google") if isinstance(snapshot, dict) else None
    notifiers = snapshot.get("notifiers") if isinstance(snapshot, dict) else {}
    notifiers = notifiers if isinstance(notifiers, dict) else {}
    sec = config.secrets

    if isinstance(google, dict) and google.get("needs_reconnect"):
        google_state = "needs_reconnect"
    elif isinstance(google, dict) and google.get("connected"):
        google_state = "connected"
    elif config.connectors.google.enabled:
        has_google_client = bool(sec.google_client_id and sec.google_client_secret)
        google_state = "configured" if has_google_client else "missing_key"
    else:
        google_state = "disabled"

    raw_scopes = google.get("scopes", []) if isinstance(google, dict) else []
    google_scopes = [
        {"name": _GOOGLE_SCOPE_LABELS.get(str(scope), "Additional approved scope")}
        for scope in raw_scopes
        if isinstance(scope, str)
    ]
    # The configured loopback URI is helpful setup context, but a query/fragment is not needed to
    # register it and could accidentally carry sensitive data. Display only the safe URI identity.
    kakao_redirect = urlsplit(resolve_kakao_redirect_uri(config))
    kakao_redirect_display = urlunsplit(
        (kakao_redirect.scheme, kakao_redirect.netloc, kakao_redirect.path, "", "")
    )

    def notifier(name: str) -> dict:
        status = notifiers.get(name)
        enabled = bool(getattr(config.connectors, name).enabled)
        chat_id_set = (
            bool(
                status.get("chat_id_set")
                if isinstance(status, dict)
                else resolve_telegram_chat_id(config)
            )
            if name == "telegram"
            else False
        )
        if isinstance(status, dict) and status.get("needs_reconnect"):
            state = "needs_reconnect"
        elif isinstance(status, dict) and status.get("connected", status.get("configured", False)):
            state = "connected" if name == "kakao" else "configured"
        elif not enabled:
            state = "disabled"
        elif name == "telegram" and not sec.telegram_bot_token:
            state = "missing_key"
        elif name == "telegram" and not resolve_telegram_chat_id(config):
            # A destination is required but never disclosed in the UI.
            state = "configured"
        elif name == "kakao" and not sec.kakao_rest_api_key:
            state = "missing_key"
        else:
            state = "configured"
        if name == "telegram":
            return {"state": state, "chat_id_set": chat_id_set}
        return {"state": state}

    telegram = notifier("telegram")
    remote = config.connectors.telegram.remote_control
    remote_ready = bool(remote.enabled and sec.telegram_bot_token)
    # Telegram's Hub card represents both independent capabilities: the legacy outbound
    # notifier and the new narrowly allowlisted remote channel.  A ready remote channel must
    # not look "disabled" merely because notification delivery has no destination configured.
    if remote.enabled:
        telegram["state"] = "configured" if remote_ready else "missing_key"
    telegram["remote_control"] = {
        "enabled": remote.enabled,
        "ready": remote_ready,
        "max_model_messages_per_hour": remote.max_model_messages_per_hour,
    }

    return {
        "google": {
            "state": google_state,
            "scopes": google_scopes,
            "services": [
                {
                    "name": "Calendar",
                    "state": google_state,
                    "can": "Read calendar; create and update events; create Meet links.",
                    "cannot": "Writes always require a preview and on-screen approval.",
                },
                {
                    "name": "Gmail",
                    "state": google_state,
                    "can": "Read Gmail; create and update drafts.",
                    "cannot": "Kira cannot send email.",
                },
                {
                    "name": "Drive & Docs",
                    "state": google_state,
                    "can": "Read Drive; create and update Kira-created Docs.",
                    "cannot": "Kira has no broad Drive access.",
                },
            ],
            "command": "uv run kira connect google",
            "status_command": "uv run kira connect status",
            "disconnect_note": (
                "Disconnect is intentionally not a UI action. Revoke Kira in your Google account "
                "permissions, then use the status command to confirm."
            ),
        },
        "telegram": {**telegram, "command": "uv run kira connect telegram --test"},
        "kakao": {
            **notifier("kakao"),
            "redirect_uri": kakao_redirect_display,
            "command": "uv run kira connect kakao",
            "test_command": "uv run kira connect kakao --test",
        },
        "providers": [
            {
                "id": row["name"],
                "name": _PROVIDER_LABELS.get(row["name"], row["name"]),
                "state": _hub_state(row["state"]),
                "enabled": bool(row["enabled"]),
                "key_present": bool(row["credentials_present"]),
                "priced": bool(row["priced"]),
                # Only the trusted Anthropic manual picker is selectable today. Auto routing is
                # surfaced elsewhere and remains subject to the same private-context gate.
                "selectable": row["name"] == "anthropic" and row["state"] == "available",
                "private_ok": bool(row["private_ok"]),
                "trusted_authority": bool(row["trusted_authority"]),
                "note": row["note"],
            }
            for row in providers_status(config)
        ],
        "services": [
            {
                "name": row["name"],
                "state": _hub_state(row["state"]),
                "kind": row["kind"],
                "note": row["note"],
                "local": not bool(row["egress"]),
            }
            for row in services_status(config)
        ],
    }


def hub_status(
    config: Config,
    *,
    egress: dict | None = None,
    connectors: dict | None = None,
    ledger_status: dict | None = None,
) -> dict:
    """Connector status. Providers are reported as key-*presence* booleans — a secret value
    must never cross the wire (asserted by the secret-absence sweep). ``connectors`` is the
    registry's presence-only status (Phase 9: google scopes/expiry, notifier configured flags —
    never a token). MCP is honestly 'not connected — a future phase'."""
    secrets = config.secrets
    return {
        "providers": {
            "anthropic": bool(secrets.anthropic_api_key),
            "voyage": bool(secrets.voyage_api_key),
            "tavily": bool(secrets.tavily_api_key),
            "openai": bool(secrets.openai_api_key),
            "elevenlabs": bool(secrets.elevenlabs_api_key),
        },
        "voice": {
            "cloud_providers": config.voice.cloud_providers,
            "stt_provider": config.voice.stt_provider,
            "tts_provider": config.voice.tts_provider,
        },
        "egress": egress or {"audio_bytes": 0, "text_chars": 0},
        "connectors": connectors or _empty_connectors(),
        # Hub-specific readable connector/provider/service cards. Same source-of-truth inputs as
        # the raw status above; all information remains presence/state/policy-only.
        "connector_overview": connector_hub_overview(config, connectors=connectors),
        "mcp": {"connected": False, "note": "not connected — future phase"},
        "model_routes": model_routes_status(config),
        "services": services_status(config),
        # A5: cost-tracking health. degraded=True means ledger writes are failing (surfaced,
        # never silent). None ⇒ not composed (a bare app / cost tracking off).
        "cost_ledger": ledger_status or {"degraded": False, "unrecorded": 0},
    }


def services_status(config: Config, *, project_services: list[str] | None = None) -> list[dict]:
    """Team-service availability for the Hub/Studio (Phase 10B). Presence-only: each catalog
    service + its derived state (available/disabled/deferred/missing_credentials/unpriced) and
    whether its credential env vars are set — NEVER a key value. ``project_services`` narrows
    per project page."""
    from jarvis.observability.cost import load_pricing
    from jarvis.services import ServiceRegistry

    pricing = load_pricing(config.root / "config" / "pricing.yaml")
    registry = ServiceRegistry(
        enabled=config.services.enabled,
        priced_services=pricing.priced_services(),
        project_services=project_services,
    )
    return registry.availability()


def model_routes_status(config: Config) -> list[dict]:
    """The resolved role → route registry for the Hub/Studio (Phase 10; provider states 10C).
    Reports provider + model + effort, a ``configured`` boolean (is the provider's key present),
    and the provider's fail-closed availability ``provider_state`` — NEVER a key value. A route
    that violates the authority pin or a text-only-on-tool-role rule reports an ``error`` string.
    Note: authority is validated here, but availability is NOT gated (no provider_registry), so a
    disabled/unpriced provider still resolves and its ``provider_state`` shows why it's unusable."""
    from jarvis.models import ModelRegistry
    from jarvis.models.providers import ProviderRegistry
    from jarvis.models.registry import RouteError
    from jarvis.models.roles import ROLES

    registry = ModelRegistry(config.models.routes)
    preg = ProviderRegistry.from_config(config)
    present = {row["name"]: row["credentials_present"] for row in preg.availability()}
    out: list[dict] = []
    for role in ROLES:
        try:
            route = registry.route(role)
        except RouteError as exc:
            out.append({"role": role, "error": str(exc)})
            continue
        out.append(
            {
                "role": role,
                "provider": route.provider,
                "model": route.model,
                "effort": route.effort,
                "text_only": route.text_only,
                "configured": present.get(route.provider, False),
                "provider_state": preg.state(route.provider).value,
            }
        )
    return out


def providers_status(config: Config) -> list[dict]:
    """Presence-only availability of every catalog provider for the Studio providers panel
    (Phase 10C). Shows available / disabled / missing_credentials / unpriced + the classification
    (tool_capable, trusted_authority, private_ok) and the credential env-var NAMES — never a key
    value. Mirrors the Phase 10B services availability view."""
    from jarvis.models.providers import ProviderRegistry

    return ProviderRegistry.from_config(config).availability()


def configured_policy_overrides(policy: Any = None) -> dict:
    """A small, read-only view of the active gate's *configured* policy for Settings.

    The global default and explicit decisions that differ from it are enough to make an egress
    posture such as ``web_search: allow`` visible without implying a comparison to an unstored
    historical/shipped file. It is not an effective per-call decision: intrinsic tool defaults and
    path/shell/sensitive/taint safety floors still apply. This view never changes a gate decision
    or exposes tool arguments.
    """
    if policy is None:
        return {
            "state": "unavailable",
            "scope": "configured_policy_only",
            "global_default": None,
            "overrides": [],
        }

    def value(decision: Any) -> str:
        return getattr(decision, "value", str(decision))

    default = value(policy.default)
    overrides = [
        {"tool": name, "decision": value(decision)}
        for name, decision in sorted(policy.tools.items())
        if value(decision) != default
    ]
    return {
        "state": "available",
        "scope": "configured_policy_only",
        "global_default": default,
        "overrides": overrides,
    }


def settings_overview(
    config: Config,
    *,
    connectors: dict | None = None,
    ledger_status: dict | None = None,
    policy: Any = None,
) -> dict:
    """The Settings screen's read-only policy surface (Phase 13). Aggregates the provider /
    service / route / budget / connector / context-reuse state so a human can review what is
    enabled and WHY — presence booleans, states, and env-var NAMES ONLY, never a key value or a
    token (the secret-absence sweep covers this route). It grants NO authority and mutates
    nothing: global service flags stay YAML-only, so ``enable_hint`` shows the exact settings.yaml
    line to add. ``connectors`` (scopes + expiry, never a token), ``ledger_status``, and ``policy``
    are the stateful bits the route passes in, mirroring :func:`hub_status`."""
    b = config.budgets
    attention_channels = {
        "urgent": list(config.attention.urgent_channels),
        "normal": list(config.attention.normal_channels),
        "low": list(config.attention.low_channels),
    }
    selected_attention_channels = {
        channel for channels in attention_channels.values() for channel in channels
    }
    connector_status = connectors or {}
    notifier_status = connector_status.get("notifiers", {})
    connected_notifiers = set(notifier_status)
    live_attention_channels = sorted(selected_attention_channels & connected_notifiers)
    demo_attention = bool(connector_status.get("demo")) or any(
        bool(status.get("demo"))
        for status in notifier_status.values()
        if isinstance(status, dict)
    )
    if not selected_attention_channels:
        attention_routing = {
            "state": "disabled",
            "reason": "No count-only attention push channels are configured.",
            "channels": attention_channels,
        }
    elif not live_attention_channels:
        attention_routing = {
            "state": "configured_not_connected",
            "reason": (
                "Count-only attention routes are configured but no selected notifier is connected."
            ),
            "channels": attention_channels,
        }
    elif demo_attention:
        attention_routing = {
            "state": "demo",
            "reason": (
                "Demo attention routing records count-only nudges locally; no Telegram/Kakao "
                "message leaves this machine."
            ),
            "channels": attention_channels,
            "live_channels": live_attention_channels,
        }
    else:
        attention_routing = {
            "state": "active",
            "reason": (
                "Count-only pushes are active for scheduler dead-letter alerts, parked "
                "unattended approvals, and attended Dreaming. Telegram/Kakao cannot approve "
                "actions; the local Gate remains the resolver."
            ),
            "channels": attention_channels,
            "live_channels": live_attention_channels,
        }
    return {
        "providers": providers_status(config),  # 10C: state / authority / private_ok + env names
        "model_routes": model_routes_status(config),
        "services": services_status(config),  # availability + egress/policy/trust + env names
        "services_enabled": list(config.services.enabled),
        "enable_hint": (
            "Global service flags are file-only. To enable one, add it to settings.yaml:\n"
            "services:\n  enabled: [firecrawl, exa, searxng, openai_image]"
        ),
        "context_reuse": {"enabled": config.context_reuse.enabled},
        "configured_policy": configured_policy_overrides(policy),
        "attention_routing": attention_routing,
        # Skill Forge status is CONFIGURATION ONLY.  Do not construct SkillCatalog here: that
        # would read local packs from a read-only status endpoint and make "off" observably
        # different from the pre-Skill-Forge runtime.  These are human-pinned identifiers, not
        # evidence that a pack exists, passed validation, or was injected into a prompt.
        "skills": {
            "mode": config.skills.mode,
            "configured_packs": [
                {
                    "pack": activation.pack,
                    "version": activation.version,
                    "sha256_prefix": activation.sha256[:12],
                }
                for activation in config.skills.enabled
            ],
        },
        "budgets": {
            "soft_warn_usd_per_run": b.soft_warn_usd_per_run,
            "hard_stop_usd_per_run": b.hard_stop_usd_per_run,
            "project_monthly_usd": b.project_monthly_usd,
            "confirm_above_usd": b.confirm_above_usd,
            "per_role_max_usd": b.per_role_max_usd,
            # Per-service cost caps live on ServicesConfig (Task 8); None until that task adds them.
            "service_max_usd_per_run": getattr(config.services, "max_usd_per_run", None),
            "service_max_usd_per_day": getattr(config.services, "max_usd_per_day", None),
        },
        "connectors": connectors or _empty_connectors(),
        "cost_ledger": ledger_status or {"degraded": False, "unrecorded": 0},
    }


def interactive_models(
    config: Config,
    *,
    current: str | None = None,
    efforts: dict[str, str] | None = None,
    current_effort: str | None = None,
    policy: str = "manual",
    routed: dict | None = None,
) -> dict:
    """The composer's model picker (Phase 15.5 + 15.6). The Anthropic ``INTERACTIVE_MODELS`` are the
    SELECTABLE manual picks (trusted, tool-capable, private_ok); the other providers are listed
    visible-but-DISABLED with a plain reason (text-only / not-allowed-for-private / unavailable),
    plus their fail-closed state. Presence/state only — never a key value.

    Phase 15.6: ``policy`` (auto|manual) is the routing mode; the returned ``auto`` option is the
    recommended default (cheap-first, escalate-when-needed) and ``routed`` is what Auto picked last
    turn. ``efforts`` / ``current_effort`` / ``effort_levels`` drive the per-model effort selector
    (a MANUAL-mode cost control; Auto uses the client default)."""
    from jarvis.models.providers import PROVIDER_CATALOG, ProviderRegistry, ProviderState
    from jarvis.ui.state import EFFORT_LEVELS, EXTERNAL_CHAT_PROVIDERS, INTERACTIVE_MODELS

    cur = current or config.models.main
    eff_by_model = efforts or {}
    default_effort = current_effort or config.limits.effort
    # Resolve provider availability DEFENSIVELY: a pricing/config hiccup must never empty the model
    # picker. The Anthropic interactive models are ALWAYS listed (the app is already running on the
    # anthropic key); only the external-provider *states* depend on the registry.
    reg: ProviderRegistry | None = None
    keyed = True
    try:
        reg = ProviderRegistry.from_config(config)
        keyed = reg.state("anthropic") is not ProviderState.MISSING_CREDENTIALS
    except Exception:  # noqa: BLE001 - degrade to listed+selectable, never a 500 / empty select
        reg = None
    models: list[dict] = [
        {
            "id": mid,
            "label": label,
            "provider": "anthropic",
            "selectable": keyed,
            "current": mid == cur,
            "effort": eff_by_model.get(mid, default_effort),
            # The Haiku tier rejects BOTH adaptive thinking and the effort parameter (400), so the
            # UI hides extended-reasoning + disables the effort selector for it. The reasoning tier
            # supports both. Surfaced per-model so the composer stays honest.
            "thinking": "haiku" not in mid.lower(),
            "supports_effort": "haiku" not in mid.lower(),
            "reason": "" if keyed else "set ANTHROPIC_API_KEY to use the main chat",
        }
        for mid, label in INTERACTIVE_MODELS
    ]
    external: list[dict] = []
    for name in EXTERNAL_CHAT_PROVIDERS:
        spec = PROVIDER_CATALOG.get(name)
        if spec is None or reg is None:
            continue
        st = reg.state(name).value
        # Phase 15.6: honest reasons. private_ok providers (gemini/openai) are text-only here, so
        # they aren't a MANUAL pick for the tool-using chat — Auto uses Gemini for cheap tool-free
        # turns. Non-private providers (qwen/deepseek/zai) never receive the private main chat.
        if spec.private_ok:
            note = "text-only — not a manual pick; Auto uses Gemini for cheap simple turns"
        else:
            note = "not allowed for private context (used only as a scoped worker)"
        external.append(
            {
                "id": name,
                "label": (spec.default_models[0] if spec.default_models else name),
                "provider": name,
                "selectable": False,  # never a manual main-chat pick this phase
                "current": False,
                "state": st,
                "reason": note if st == "available" else f"{note} ({st})",
            }
        )
    return {
        "current": cur,
        "models": models,
        "external": external,
        "current_effort": eff_by_model.get(cur, default_effort),
        "effort_levels": [{"id": v, "label": label} for v, label in EFFORT_LEVELS],
        # Phase 15.6 cost-aware routing: the policy (auto|manual), the recommended Auto option, and
        # what Auto picked last turn (so the composer can show "Auto → Sonnet 5").
        "policy": policy,
        "auto": {
            "recommended": True,
            "label": "Auto",
            "description": "uses cheap models first, escalates only when needed",
            "current": policy == "auto",
        },
        "routed": routed,
    }


# --- capability truth: ONE availability read model, rendered by every surface (Phase 15.5) ---

#: Substring needles that mark a connector's chat tools present in the loop's registered set.
_CAP_NEEDLES = {"gmail": ("gmail",), "drive": ("drive",), "calendar": ("calendar",)}


def _exposed(registered: set[str] | None, needles: tuple[str, ...], *, connected: bool) -> bool:
    """Is this capability actually usable in chat? With the loop's ``registered`` tool names, it is
    exact (a connected connector whose tool failed to register reads as NOT exposed — the 'why'
    case). Without them, fall back to connected-implies-exposed (today's behavior)."""
    if not connected:
        return False
    if registered is None:
        return True
    return any(any(n in t for n in needles) for t in registered)


def capability_truth(
    config: Config,
    *,
    connectors: dict | None = None,
    voice: dict | None = None,
    registered_tools: set[str] | None = None,
    project_services: tuple[str, ...] | list[str] | None = None,
) -> dict:
    """THE one availability truth (Phase 15.5) — connectors / providers / services / voice / MCP as
    ``{name, state, exposed_to_chat, reason}`` rows. Daily, Hub, Settings, and the conversation
    header all render THIS, so they can never disagree about what's connected or usable. Every field
    is presence / state / plain reason — never a key value (secret-swept). ``exposed_to_chat`` is
    whether the capability is actually wired into the chat (precise when ``registered_tools`` is
    passed; connected-implies-exposed otherwise)."""
    from jarvis.models.providers import PROVIDER_CATALOG, ProviderRegistry
    from jarvis.ui.state import EXTERNAL_CHAT_PROVIDERS

    c = connectors or _empty_connectors()
    demo = bool(c.get("demo"))
    google = c.get("google")
    g_connected = (
        bool(google)
        and bool(google.get("connected", True))
        and not (isinstance(google, dict) and google.get("needs_reconnect"))
    )
    g_reconnect = isinstance(google, dict) and bool(google.get("needs_reconnect"))

    def _google_row(label: str, key: str) -> dict:
        if google is None:
            return {
                "name": label,
                "state": "not_configured",
                "exposed_to_chat": False,
                "reason": "Connect Google in the Hub to use it here.",
            }
        if g_reconnect:
            return {
                "name": label,
                "state": "needs_reconnect",
                "exposed_to_chat": False,
                "reason": "Google sign-in expired — reconnect in the Hub.",
            }
        exposed = _exposed(registered_tools, _CAP_NEEDLES[key], connected=g_connected)
        reason = "" if exposed else "Connected, but not available as a tool in this chat."
        if demo:
            reason = "Demo data — not your real account."
        return {"name": label, "state": "connected", "exposed_to_chat": exposed, "reason": reason}

    conn_rows = [
        _google_row("Google Calendar", "calendar"),
        _google_row("Gmail", "gmail"),
        _google_row("Google Drive", "drive"),
    ]
    for name, label in (("telegram", "Telegram"), ("kakao", "Kakao")):
        st = (c.get("notifiers") or {}).get(name)
        configured = bool(st) and bool(st.get("configured", st.get("connected", False)))
        conn_rows.append(
            {
                "name": label,
                "state": "connected" if configured else "not_configured",
                "exposed_to_chat": False,  # a notifier delivers messages OUT; it is not a chat tool
                "reason": (
                    "Delivers notifications; not a chat tool."
                    if configured
                    else "Not configured. Approved sends and digest delivery are separately "
                    "configured."
                ),
            }
        )

    # Providers + services depend on the pricing table / provider registry. Resolve them
    # DEFENSIVELY: a hiccup there must never blank the whole grid — the connector rows above (and
    # voice/MCP below) always render, and anthropic (the main chat) is shown available by default.
    prov_rows = [{"name": "Anthropic", "state": "available", "exposed_to_chat": True, "reason": ""}]
    try:
        reg = ProviderRegistry.from_config(config)
        prov_rows[0]["state"] = reg.state("anthropic").value
        for name in EXTERNAL_CHAT_PROVIDERS:
            if name not in PROVIDER_CATALOG:
                continue
            prov_rows.append(
                {
                    "name": name,
                    "state": reg.state(name).value,
                    "exposed_to_chat": False,
                    "reason": "Not enabled for the main chat (would receive private context).",
                }
            )
    except Exception:  # noqa: BLE001 - keep the grid rendering; just show anthropic
        pass

    svc_rows = []
    try:
        services = services_status(
            config,
            project_services=list(project_services) if project_services is not None else None,
        )
    except Exception:  # noqa: BLE001 - services availability needs pricing; degrade to none
        services = []
    enabled_services = set(config.services.enabled)
    project_service_set = set(project_services) if project_services is not None else None
    for s in services:
        avail = s.get("state") == "available"
        narrowed = (
            project_service_set is not None
            and s.get("state") == "disabled"
            and s.get("name") in enabled_services
            and s.get("name") not in project_service_set
        )
        svc_rows.append(
            {
                "name": s.get("name"),
                "state": s.get("state"),
                "exposed_to_chat": avail,
                "reason": (
                    ""
                    if avail
                    else (
                        "Not enabled for this project."
                        if narrowed
                        else f"Service {s.get('state')}."
                    )
                ),
            }
        )

    v = voice or {}
    v_on = bool(v.get("enabled"))
    voice_row = {
        "state": "on" if v_on else "off",
        "exposed_to_chat": v_on,
        "reason": "" if v_on else (v.get("reason") or "Voice is off — enable it in settings.yaml."),
    }
    mcp_row = {
        "state": "not_configured",
        "exposed_to_chat": False,
        "reason": "No MCP client yet — a future phase.",
    }

    exposed_conns = [r["name"] for r in conn_rows if r["exposed_to_chat"]]
    exposed_svcs = sum(1 for r in svc_rows if r["exposed_to_chat"])
    bits = []
    bits.append(", ".join(exposed_conns) if exposed_conns else "no connectors")
    if exposed_svcs:
        bits.append(f"{exposed_svcs} service{'s' if exposed_svcs != 1 else ''}")
    bits.append("voice on" if v_on else "voice off")
    return {
        "connectors": conn_rows,
        "providers": prov_rows,
        "services": svc_rows,
        "voice": voice_row,
        "mcp": mcp_row,
        "summary": " · ".join(bits),
    }


# --- daily: the bootstrap read model (Phase 9) ------------------------------


async def _repo_states(config: Config) -> list[dict]:
    out: list[dict] = []
    for spec in config.connectors.repos:
        root = Path(spec) if Path(spec).is_absolute() else (config.root / spec)
        state = await RepoReader(root).state()
        out.append({"path": spec, "state": dataclasses.asdict(state) if state else None})
    return out


def _eval_freshness(config: Config, repos: list[dict]) -> dict:
    history = _read_history(config.data_dir / "evals" / "history.jsonl")
    last = history[-1] if history else None
    head = next((r["state"]["head_rev"] for r in repos if r["path"] == "." and r["state"]), None)
    last_rev = last.get("git_rev") if last else None
    return {
        "ever_run": bool(history),
        "last_gate_at": last.get("timestamp") if last else None,
        "last_gate_rev": last_rev,
        "verdict": last.get("verdict") if last else None,
        "head_rev": head,
        # stale = HEAD has moved past the last gated revision (freshness chip goes gray).
        "stale": bool(head and last_rev and head != last_rev),
        "replay_command": _EVAL_REPLAY_COMMAND,  # shown to copy, never a run button
        "live_command": _EVAL_SMALL_LIVE_COMMAND,
        "default_mode": "replay",
        "projected_replay_usd": 0.0,
        "cost_note": (
            "the default command is keyless replay = $0 incremental API spend; "
            "live/record modes require an explicit positive finite --max-cost-usd LLM "
            "spend stop threshold. The small live scenario is a partial signal, not "
            "closeout evidence. "
            "Stop the running Kira process before either gate."
        ),
    }


async def _tasks_today(tasks: TaskService, *, project_id: object = _TASK_ANY_PROJECT) -> list[dict]:
    now = _dt.datetime.now().astimezone()
    out: list[dict] = []
    # Global Daily (project_id=ANY) shows every due task; when a project is active it scopes to
    # that project + global (the user's "aggregate global + active project" rule).
    for t in await tasks.store.list(include_finished=False, project_id=project_id):
        if not t.next_run_at:
            continue
        try:
            when = _dt.datetime.fromisoformat(t.next_run_at).astimezone(now.tzinfo)
        except ValueError:
            continue
        if when.date() == now.date():
            out.append({"id": t.id, "title": t.title, "kind": t.kind, "next_run_at": t.next_run_at})
    return out


def _digest_dict(record) -> dict:
    return {
        "date_local": record.date_local,
        "generated_at": record.generated_at,
        "summary": record.summary,
        "suggested_actions": record.suggested_actions,
        "sections": record.sections,
        "delivered_to": record.delivered_to,
    }


async def _project_assessment_daily(
    config: Config,
    services: UiServices,
    project_id: int | None,
) -> dict | None:
    """Compact exact-project assessment state; never a full report or provider error."""
    if project_id is None:
        return None
    if not (
        config.project_intelligence.enabled
        and config.project_intelligence.analyze_after_import
    ):
        return {"state": "disabled", "report": None}
    jobs = services.analysis_jobs
    reports = services.project_reports
    if jobs is None or reports is None:
        return {"state": "unavailable", "report": None}
    job = await jobs.latest(project_id)
    job_state = str(job.state) if job is not None else None
    if job_state in {"queued", "running"}:
        return {"state": job_state, "report": None}
    if job_state == "failed":
        return {"state": "failed", "report": None}

    report = await reports.latest(project_id, current_only=False)
    if report is None:
        return {"state": "failed" if job_state == "published" else "idle", "report": None}
    if services.knowledge is None or services.graph is None:
        return {"state": "unavailable", "report": None}
    try:
        from jarvis.projects import seal_snapshot

        snapshot = await seal_snapshot(services.knowledge.store, services.graph, project_id)
    except Exception:
        return {"state": "unavailable", "report": None}
    if (
        report.status != "current"
        or report.snapshot_hash != snapshot.snapshot_hash
        or (job is not None and job.snapshot_hash != snapshot.snapshot_hash)
    ):
        return {"state": "idle", "report": None}
    view = serialize_project_report(report, effective_status="current")
    return {
        "state": "ready",
        "report": {
            "id": view["id"],
            "summary_preview": _report_text(view["summary"], limit=240),
            "created_at": view["created_at"],
            "trust_class": view["trust_class"],
            "counts": view["counts"],
            "coverage": view["coverage"],
        },
    }


async def daily_overview(
    config: Config,
    services: UiServices,
    *,
    notices: Any = None,
    notice_project_id: int | None = None,
    scope_notices: bool = False,
    gate_pending: int = 0,
    assessment_project_id: int | None = None,
) -> dict:
    """The Daily screen's bootstrap: repo state, eval freshness, today's tasks, the review
    queue count, the latest digest, notices, and connector status — all read-only views."""
    repos = await _repo_states(config)
    # Daily scopes "today's tasks" to the active project (+ global); global scope shows all.
    active_pid: object = _TASK_ANY_PROJECT
    if services.projects is not None and services.projects.current().project_id is not None:
        active_pid = services.projects.current().project_id
    tasks_today = (
        await _tasks_today(services.tasks, project_id=active_pid)
        if services.tasks is not None
        else []
    )
    kb_review = len(await services.knowledge.unreviewed_sources()) if services.knowledge else 0
    latest = await services.digests.latest() if services.digests is not None else None
    connectors = (
        services.connectors.status() if services.connectors is not None else _empty_connectors()
    )
    total_dirty = sum(r["state"]["dirty_files"] for r in repos if r["state"])
    # Phase 10: project cards + the active project (a calm summary; the Projects screen is full).
    projects: list[dict] = []
    active_project: int | None = None
    if services.projects is not None:
        projects = [
            {"id": p.id, "name": p.name, "slug": p.slug, "color": p.color, "status": p.status}
            for p in await services.projects.store.list(status="active")
        ]
        active_project = services.projects.current().project_id
    # Phase 11: a calm "recent artifacts" strip (newest across projects, pinned first).
    recent_artifacts: list[dict] = []
    if services.artifacts is not None:
        recent_artifacts = [serialize_artifact(a) for a in await services.artifacts.list(limit=6)]
    # Phase 11: the latest orchestration run (status + cost) — a calm link into Studio.
    latest_run: dict | None = None
    if services.orchestration is not None:
        runs = await services.orchestration.list(limit=1)
        if runs:
            latest_run = serialize_orchestration_run(runs[0])
    return {
        "repos": repos,
        "evals": _eval_freshness(config, repos),
        "tasks_today": tasks_today,
        "pending_approvals": gate_pending,
        "kb_review_count": kb_review,
        "digest": _digest_dict(latest) if latest else None,
        "notices": (
            notices.tail(20, project_id=notice_project_id)
            if notices is not None and scope_notices
            else (notices.tail(20) if notices is not None else [])
        ),
        "connectors": connectors,
        "demo": bool(connectors.get("demo")),
        "what_changed": {"repos": len(repos), "dirty_files": total_dirty},
        "projects": projects,
        "active_project": active_project,
        "recent_artifacts": recent_artifacts,
        "latest_run": latest_run,
        "project_assessment": await _project_assessment_daily(
            config, services, assessment_project_id
        ),
    }


# --- lab: eval history + baselines + latest report (view-only) --------------


def _read_history(history_path: Path) -> list[dict]:
    if not history_path.exists():
        return []
    out: list[dict] = []
    for line in history_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def _latest_report_path(evals_dir: Path) -> Path | None:
    if not evals_dir.exists():
        return None
    reports = sorted(evals_dir.glob("*/report.md"))
    return reports[-1] if reports else None


def _latest_report(evals_dir: Path) -> str | None:
    path = _latest_report_path(evals_dir)
    return path.read_text(encoding="utf-8") if path is not None else None


async def lab_overview(
    config: Config, *, baselines_path: Path | None = None, artifacts: Any = None
) -> dict:
    """Eval history (one gate per line), the committed baselines contract, and the latest
    rendered report — all file reads, view-only. Running evals stays a terminal ritual. The
    single latest report is registered (idempotently, by its run-dir name) as an artifact so it
    surfaces in the Library — forward-only: the current latest only, never a backfill of history."""
    evals_dir = config.data_dir / "evals"
    history = _read_history(evals_dir / "history.jsonl")
    bpath = baselines_path or (config.root / "tests" / "evals" / "baselines.yaml")
    baselines = bpath.read_text(encoding="utf-8") if bpath.exists() else None
    latest_path = _latest_report_path(evals_dir)
    if artifacts is not None and latest_path is not None:
        # Fail-soft: artifact bookkeeping must never break the (read-only) Lab view.
        with contextlib.suppress(Exception):
            await artifacts.register(
                origin_type="eval_report",
                origin_id=latest_path.parent.name,  # "<ts>-<rev>" — stable identity
                kind="eval_report",
                title=f"Eval gate {latest_path.parent.name}",
                created_by="system",
                local_path=latest_path,
            )
    report_text = latest_path.read_text(encoding="utf-8") if latest_path is not None else None
    return {
        "history": history[-50:],
        "gate_runs": len(history),
        "baselines": baselines,
        "latest_report": report_text,
        "replay_command": _EVAL_REPLAY_COMMAND,
        "live_command": _EVAL_SMALL_LIVE_COMMAND,
        "note": (
            "Stop the running Kira UI or terminal first because evals require exclusive "
            "maintenance authority. Start with keyless replay. The live example runs one "
            "unjudged scenario and uses a $1.00 LLM spend stop threshold. It is a partial "
            "signal, not full closeout evidence."
        ),
    }


# --- write intents (Phase 12): the approval queue + write journal ----------


def serialize_intent(intent: Any) -> dict:
    """Metadata + the rendered preview for the approval queue. Ships NO secret and NO raw request:
    the rendered preview (the user's own event/doc content, meant to be reviewed) plus a short
    result handle only — never the stored ``prior`` event body kept server-side for undo, never a
    token or scope value."""
    result = intent.result or {}
    return {
        "id": intent.id,
        "kind": intent.kind,
        "state": intent.state.value,
        "summary": intent.summary,
        "project_id": intent.project_id,
        "created_at": intent.created_at,
        "updated_at": intent.updated_at,
        "preview": intent.preview,
        "link": result.get("link"),
        "remote_id": result.get("remote_id"),
        "error": intent.error,
    }


async def intents_queue(intents: Any, *, project_id: int | None = None, limit: int = 50) -> dict:
    """The write approval queue: ``pending`` (previewed, awaiting approval) + ``recent`` (executed
    / failed / undone / rejected) — the outbox view with undo affordances."""
    from jarvis.actions.intents import IntentState

    cap = max(1, min(limit, 200))
    pending = await intents.list(state=IntentState.PREVIEWED, project_id=project_id, limit=cap)
    settled = {
        IntentState.EXECUTED,
        IntentState.FAILED,
        IntentState.UNDONE,
        IntentState.REJECTED,
    }
    recent = [
        i for i in await intents.list(project_id=project_id, limit=cap) if i.state in settled
    ][:cap]
    return {
        "pending": [serialize_intent(i) for i in pending],
        "recent": [serialize_intent(i) for i in recent],
    }


def serialize_connector_write(write: Any) -> dict:
    """Metadata-only outward-write evidence safe for the Notifications audit surface.

    Deliberately omit remote/rollback/egress/trace handles.  They may be useful to internal
    recovery code, but the browser needs only proof of the provider action, its outcome, scope,
    and time — never content or correlation identifiers.
    """
    return {
        "id": write.id,
        "provider": write.provider,
        "verb": write.verb,
        "scope": write.scope,
        "project_id": write.project_id,
        "status": write.status,
        "at": write.ts,
    }


async def connector_write_history(
    journal: Any, *, project_id: int | None = None, limit: int = 50
) -> dict:
    """Newest-first, metadata-only connector writes for a scope or the legacy/global UI."""
    cap = max(1, min(limit, 100))
    writes = await journal.list(project_id=project_id, limit=cap)
    return {"writes": [serialize_connector_write(write) for write in writes]}
