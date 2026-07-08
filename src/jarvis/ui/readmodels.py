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
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

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
    # Phase 11: the artifact store backs the Artifacts Library + global search + content route;
    # the saved-view store backs smart collections on Projects/Artifacts/Search.
    artifacts: Any = None  # an ArtifactStore; None when artifacts aren't composed
    views: Any = None  # a SavedViewStore; None when the DB isn't composed
    # Phase 12: the intent store backs the approval queue; the write journal backs the outbox
    # read model + undo. Both None when the write substrate isn't composed.
    intents: Any = None  # an IntentStore
    write_journal: Any = None  # a ConnectorWriteJournal


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
    """Live memories, optionally scoped to a project ("what Kairo knows about this project"
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
            "open_tasks": None, "sessions_week": None, "last_run": None, "month_spend_usd": None,
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


async def costs_overview(
    budgets: Any, *, project_id: int | None = None, projects: Any = None
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


def serialize_view(v) -> dict:
    return {
        "id": v.id,
        "name": v.name,
        "scope": v.scope,
        "query": v.query,
        "project_id": v.project_id,
        "created_by": v.created_by,
        "created_at": v.created_at,
        "updated_at": v.updated_at,
    }


async def views_list(
    store: Any, *, scope: str | None = None, project_id: int | None = None
) -> dict:
    """Saved views / smart collections. A None project_id lists every view; a concrete id lists
    that project's views + global ones."""
    p: object = _ANY_PROJECT if project_id is None else project_id
    return {"views": [serialize_view(v) for v in await store.list(scope=scope, project_id=p)]}


async def workspace_overview(services: UiServices, project_id: int) -> dict:
    """The Project Workspace Overview tab: the project + a few health chips + recent artifacts +
    recent runs, scoped to the project. Read-only; each piece degrades if its service is off."""
    out: dict = {
        "project_id": project_id, "project": None,
        "recent_artifacts": [], "recent_runs": [], "health": {},
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
                events.append({"type": "artifact", "title": a.title, "kind": a.kind,
                               "ts": a.created_at, "ref_id": a.id})
    with contextlib.suppress(Exception):
        if services.orchestration is not None:
            for r in await services.orchestration.list(project_id=project_id, limit=limit):
                events.append({"type": "run", "title": r.title or r.workflow, "status": r.status,
                               "ts": r.finished_at or r.started_at, "ref_id": r.id})
    with contextlib.suppress(Exception):
        if services.sessions is not None:
            for m in await services.sessions.list_sessions(project_id=project_id, limit=limit):
                events.append(
                    {"type": "chat", "title": m.title, "ts": m.updated_at, "ref_id": m.id}
                )
    events = [e for e in events if e.get("ts")]
    events.sort(key=lambda e: e["ts"], reverse=True)
    return {"events": events[:limit], "project_id": project_id}


async def orchestration_roi(
    store: Any, budgets: Any, *, project_id: int | None = None, limit: int = 20
) -> list[dict]:
    """Per-run ROI for the Studio/Costs surfaces: for each recent completed run, the human-time
    value its workflow stood in for (baseline_minutes × hourly rate) minus its actual cost. Net
    is None when the cost is unpriced (fail-closed)."""
    from jarvis.orchestration import WORKFLOWS

    runs = await store.list(project_id=project_id, limit=limit)
    out: list[dict] = []
    for r in runs:
        wf = WORKFLOWS.get(r.workflow)
        if wf is None:
            continue
        roi = budgets.roi(wf.baseline_minutes, r.actual_cost_usd)
        out.append(
            {
                "run_id": r.id,
                "team": r.config.get("team"),
                "workflow": r.workflow,
                "status": r.status,
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
    limit: int = 50,
) -> dict:
    """The chats list (or a search over titles + message text). Interactive sessions only.
    ``project_id`` scopes to one project's chats (the Workspace Chats tab); absent ⇒ every chat
    (the global list). Passing None as a value would mean 'global-only', so it is only forwarded
    when a concrete id is given."""
    scope = {} if project_id is None else {"project_id": project_id}
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


async def session_transcript(sessions: SessionStore, session_id: int) -> dict:
    """One chat's transcript for the history view — the user's own conversation, rendered
    to {role, text} (no tool-result plumbing). ``ok: False`` if the session is unknown."""
    meta = await sessions.get_meta(session_id)
    if meta is None:
        return {"ok": False, "message": "no such session"}
    messages = await sessions.load_messages(session_id)
    rendered = [
        {"role": m.get("role"), "text": text}
        for m in messages
        if (text := _message_text(m.get("content")))
    ]
    return {"ok": True, "session": serialize_session_meta(meta), "messages": rendered}


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


async def vault_overview(knowledge: KnowledgeService, *, project_id: int | None = None) -> dict:
    stats = await knowledge.stats()
    unreviewed = await knowledge.unreviewed_sources()
    items = []
    for s in unreviewed:
        # Workspace Vault tab: scope the review queue to this project's sources (a Source carries
        # project_id; None == global). The global Vault screen passes no project_id (all sources).
        if project_id is not None and s.project_id != project_id:
            continue
        entry = serialize_source(s)
        # A capped markdown preview so approving a quarantined source is INFORMED, not blind.
        entry["preview"] = await knowledge.source_markdown(s.id, max_chars=1200)
        items.append(entry)
    return {"stats": stats, "unreviewed": items, "project_id": project_id}


async def vault_lint(knowledge: KnowledgeService) -> dict:
    report = await knowledge.lint()
    return dataclasses.asdict(report)


# --- agents (Trace) --------------------------------------------------------


def serialize_agent_run(run: AgentRun) -> dict:
    return {
        "id": run.id,
        "title": run.title,
        "status": run.status,
        "tools_scope": run.tools_scope,
        "iterations": run.iterations,
        "denied_count": run.denied_count,
        "cost_usd": run.cost_usd,
        "parent_trace_id": run.parent_trace_id,
        "child_trace_id": run.child_trace_id,
        "started_at": run.started_at,
    }


async def list_agent_runs(run_store: AgentRunStore, *, limit: int = 50) -> list[dict]:
    return [serialize_agent_run(r) for r in await run_store.list(limit=limit)]


# --- orchestration (Studio): runs + team/workflow catalog (metadata only) ---


def serialize_orchestration_run(run: Any) -> dict:
    """One orchestration run for the Studio history/detail. Summary + manifest + costs only —
    the store never holds a verbatim prompt or child report, so nothing sensitive is here."""
    return {
        "id": run.id,
        "project_id": run.project_id,
        "workflow": run.workflow,
        "title": run.title,
        "team": run.config.get("team"),
        "status": run.status,
        "stage": run.stage,
        "verdict": run.verdict,
        "synthesis_summary": run.synthesis_summary,
        "estimated_cost_usd": run.estimated_cost_usd,
        "actual_cost_usd": run.actual_cost_usd,
        "budget_usd": run.budget_usd,
        "context_manifest": run.context_manifest,  # refs/hashes/token-est only (bodies-free)
        "started_at": run.started_at,
        "finished_at": run.finished_at,
    }


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
    detail = {"run": serialize_orchestration_run(run), "members": members}
    if budgets is not None:
        from jarvis.orchestration import WORKFLOWS

        detail["cost_breakdown"] = await budgets.run_breakdown(run_id)
        wf = WORKFLOWS.get(run.workflow)
        if wf is not None:
            detail["roi"] = budgets.roi(wf.baseline_minutes, run.actual_cost_usd)
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
    """All 8 team profiles (code constants) for the Studio team picker."""
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
    last_cost = (last.get("totals") or {}).get("cost_usd") if isinstance(last, dict) else None
    return {
        "ever_run": bool(history),
        "last_gate_at": last.get("timestamp") if last else None,
        "last_gate_rev": last_rev,
        "verdict": last.get("verdict") if last else None,
        "head_rev": head,
        # stale = HEAD has moved past the last gated revision (freshness chip goes gray).
        "stale": bool(head and last_rev and head != last_rev),
        "command": "jarvis eval gate",  # a terminal ritual — shown to copy, never a run button
        # Cost projection (eval cost-control layer). The default eval mode is keyless replay
        # ($0, no API calls); a live gate is the phase-closeout ritual whose cost is estimated
        # from the last live run. Shown so the human sees the $ before running anything.
        "default_mode": "replay",
        "last_gate_cost_usd": last_cost,
        "projected_replay_usd": 0.0,
        "cost_note": (
            "default `jarvis eval` is keyless replay = $0; a live gate "
            + (f"last cost ${last_cost:.2f}" if isinstance(last_cost, int | float) else
               "has no prior cost recorded")
            + ". Use `jarvis eval plan --live` for a projection."
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


async def daily_overview(
    config: Config,
    services: UiServices,
    *,
    notices: Any = None,
    gate_pending: int = 0,
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
        "notices": notices.tail(20) if notices is not None else [],
        "connectors": connectors,
        "demo": bool(connectors.get("demo")),
        "what_changed": {"repos": len(repos), "dirty_files": total_dirty},
        "projects": projects,
        "active_project": active_project,
        "recent_artifacts": recent_artifacts,
        "latest_run": latest_run,
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
        "note": "Run evals from the terminal: `jarvis eval gate` (a deliberate, recorded ritual).",
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
