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

from jarvis.reporting.repo import RepoReader

if TYPE_CHECKING:
    from jarvis.agents.store import AgentRun, AgentRunStore
    from jarvis.config import Config
    from jarvis.digest.store import DigestStore
    from jarvis.knowledge.service import KnowledgeService
    from jarvis.memory.service import MemoryService
    from jarvis.memory.store import Memory
    from jarvis.persistence.sessions import SessionMeta, SessionStore
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
    # Phase 10: the session store backs the chats list / search / pin / resume.
    sessions: SessionStore | None = None


# --- memory ----------------------------------------------------------------


def serialize_memory(memory: Memory) -> dict:
    """A memory row for the Memory screen — WITHOUT the embedding vector (never shipped)."""
    return {
        "id": memory.id,
        "type": memory.type,
        "content": memory.content,
        "source": memory.source,
        "status": memory.status,
        "provenance": dataclasses.asdict(memory.provenance),
        "created_at": memory.created_at,
        "access_count": memory.access_count,
    }


async def list_memories(memory: MemoryService, *, type_filter: str | None = None) -> list[dict]:
    rows = await memory.store.all_live()
    if type_filter:
        rows = [m for m in rows if m.type == type_filter]
    return [serialize_memory(m) for m in rows]


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


async def list_tasks(tasks: TaskService, *, include_finished: bool = True) -> list[dict]:
    return [serialize_task(t) for t in await tasks.store.list(include_finished=include_finished)]


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


# --- hub: connector status (PRESENCE BOOLEANS ONLY — never a key value) -----


def _empty_connectors() -> dict:
    return {"demo": False, "google": None, "notifiers": {}}


def hub_status(
    config: Config, *, egress: dict | None = None, connectors: dict | None = None
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
        "command": "jarvis eval gate",  # a terminal ritual — shown to copy, never a run button
    }


async def _tasks_today(tasks: TaskService) -> list[dict]:
    now = _dt.datetime.now().astimezone()
    out: list[dict] = []
    for t in await tasks.store.list(include_finished=False):
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
    tasks_today = await _tasks_today(services.tasks) if services.tasks is not None else []
    kb_review = len(await services.knowledge.unreviewed_sources()) if services.knowledge else 0
    latest = await services.digests.latest() if services.digests is not None else None
    connectors = (
        services.connectors.status() if services.connectors is not None else _empty_connectors()
    )
    total_dirty = sum(r["state"]["dirty_files"] for r in repos if r["state"])
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
