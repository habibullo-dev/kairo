"""Read models for the workstation screens (Phase 8, Task 5).

Every screen is a *view* over an existing service — the UI adds no storage and no new
authority. These functions serialize the domain objects to JSON-safe dicts (deliberately
selecting fields, so nothing sensitive leaks by accident — e.g. a memory's embedding vector
is never shipped, and Hub reports provider **presence booleans only**, never a key value).

The service-backed models take a service/store and are tested against a temp DB with a
``FakeEmbedder``; Hub and Lab are pure over config + files (fully keyless).
"""

from __future__ import annotations

import dataclasses
import datetime as _dt
import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from jarvis.memory.store import ANY_PROJECT as _MEM_ANY_PROJECT
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


# --- costs -----------------------------------------------------------------


async def costs_overview(budgets: Any, *, project_id: int | None = None) -> dict:
    """The Costs screen: today/week/month spend + limits + the 'why this cost' breakdown (by
    purpose, role, model, team, and service). Unpriced calls/services are surfaced separately,
    never summed as $0."""
    status = await budgets.status(project_id=project_id)
    month_start = _period_start_iso("month")
    return {
        **status,
        "by_purpose": await budgets.grouped("purpose", project_id=project_id, since=month_start),
        "by_role": await budgets.grouped("agent_role", project_id=project_id, since=month_start),
        "by_model": await budgets.grouped("model", project_id=project_id, since=month_start),
        "by_team": await budgets.grouped("team", project_id=project_id, since=month_start),
        "by_service": await budgets.grouped_services(
            "service", project_id=project_id, since=month_start
        ),
    }


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
    limit: int = 50,
) -> dict:
    """The chats list (or a search over titles + message text). Interactive sessions only."""
    if query:
        rows = await sessions.search_sessions(query, limit=limit)
    else:
        rows = await sessions.list_sessions(pinned=pinned, limit=limit)
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


async def vault_overview(knowledge: KnowledgeService) -> dict:
    stats = await knowledge.stats()
    unreviewed = await knowledge.unreviewed_sources()
    items = []
    for s in unreviewed:
        entry = serialize_source(s)
        # A capped markdown preview so approving a quarantined source is INFORMED, not blind.
        entry["preview"] = await knowledge.source_markdown(s.id, max_chars=1200)
        items.append(entry)
    return {"stats": stats, "unreviewed": items}


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
    return {
        "ever_run": bool(history),
        "last_gate_at": last.get("timestamp") if last else None,
        "last_gate_rev": last_rev,
        "verdict": last.get("verdict") if last else None,
        "head_rev": head,
        # stale = HEAD has moved past the last gated revision (freshness chip goes gray).
        "stale": bool(head and last_rev and head != last_rev),
        "command": "jarvis eval gate",  # a terminal ritual — shown to copy, never a run button
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


def _latest_report(evals_dir: Path) -> str | None:
    if not evals_dir.exists():
        return None
    reports = sorted(evals_dir.glob("*/report.md"))
    return reports[-1].read_text(encoding="utf-8") if reports else None


def lab_overview(config: Config, *, baselines_path: Path | None = None) -> dict:
    """Eval history (one gate per line), the committed baselines contract, and the latest
    rendered report — all file reads, view-only. Running evals stays a terminal ritual."""
    evals_dir = config.data_dir / "evals"
    history = _read_history(evals_dir / "history.jsonl")
    bpath = baselines_path or (config.root / "tests" / "evals" / "baselines.yaml")
    baselines = bpath.read_text(encoding="utf-8") if bpath.exists() else None
    return {
        "history": history[-50:],
        "gate_runs": len(history),
        "baselines": baselines,
        "latest_report": _latest_report(evals_dir),
        "note": "Run evals from the terminal: `jarvis eval gate` (a deliberate, recorded ritual).",
    }
