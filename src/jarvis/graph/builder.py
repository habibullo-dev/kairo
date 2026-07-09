"""Deterministic derivation of the graph's DERIVED edge cache from existing stores.

The graph's structural relationships already live as rows and foreign keys; this builder reads them
and writes the ``origin='derived'`` edge cache. It is a rebuildable projection, not a source of
truth:

* :meth:`rebuild` first clears the derived cache (``delete_derived_edges`` — asserted rows are never
  touched), then re-derives every edge. Running it twice yields the same edge CONTENT (identity +
  metadata + timestamps); only the surrogate autoincrement ids differ.
* **Timestamps are the SOURCE row's ``created_at``, never wall-clock** (the Phase-14 ``7bb5f4f``
  determinism lesson) — so a rebuild tomorrow is identical to one today.
* **Trust is never upgraded.** A structural edge between two local rows is ``trusted_local``; an
  edge whose content endpoint is a KB source or artifact carries THAT endpoint's provenance-mapped
  trust (a web/unreviewed source ⇒ ``untrusted_external``), so the graph can never present
  untrusted content as trusted.

Derived NODES are not stored — an endpoint like ``project:3`` / ``run:9`` / ``team:security`` /
``wiki:pages/x.md`` references the existing row (or a code constant); the read model (Task 3)
resolves an endpoint to its card. This module writes only edges.
"""

from __future__ import annotations

import json
from collections import Counter

from jarvis.graph.store import GraphStore

_DERIVED_BY = "system"

# artifact.origin_type -> the graph node kind its origin_id addresses (only the producers that map
# to a real node kind; others — openai_image / eval_report / meeting / connector_write — have no
# node kind, so no provenance edge is drawn, though the artifact node itself still exists).
_ARTIFACT_ORIGIN_KIND = {"orchestration": "run", "wiki": "wiki", "digest": "digest"}


def _source_trust(kind: str, review_status: str) -> str:
    """A KB source's trust: web URLs and anything unreviewed are untrusted_external; a reviewed
    file/note is 'reviewed'. (Never 'trusted_local' — a source is ingested content, not first-party
    structure.)"""
    if kind == "url" or review_status != "reviewed":
        return "untrusted_external"
    return "reviewed"


def _artifact_trust(provenance_class: str | None) -> str:
    """Map an artifact's provenance_class to the graph's 4-value trust vocabulary."""
    if provenance_class in ("untrusted_external_content", "security_finding_untrusted"):
        return "untrusted_external"
    if provenance_class in ("untrusted_model_generated", "derived_summary"):
        return "model_generated"
    return "trusted_local"


async def rebuild(store: GraphStore) -> dict[str, int]:
    """Delete + re-derive the whole derived edge cache. Returns a count per edge_kind. Asserted
    rows are never touched. Deterministic: every query is ORDER BY id and every edge carries its
    source row's created_at."""
    db = store.db
    await store.delete_derived_edges()
    counts: Counter[str] = Counter()

    async def edge(sk, si, dk, di, ek, created_at, *, trust="trusted_local", project_id=None,
                   team=None, evidence=None):
        await store.upsert_edge(
            src_kind=sk, src_id=str(si), dst_kind=dk, dst_id=str(di), edge_kind=ek,
            origin="derived", trust_class=trust, created_by=_DERIVED_BY, created_at=created_at,
            project_id=project_id, team=team, evidence=evidence,
        )
        counts[ek] += 1

    async def rows(sql: str):
        return await (await db.execute(sql)).fetchall()

    # 1. project -> chat (interactive sessions only)
    for sid, pid, ts in await rows(
        "SELECT id, project_id, created_at FROM sessions "
        "WHERE kind='interactive' AND project_id IS NOT NULL ORDER BY id"
    ):
        await edge("project", pid, "chat", sid, "has_chat", ts, project_id=pid)

    # 2. project -> run   3. run -> team (config_json.team)
    for rid, pid, cfg, ts in await rows(
        "SELECT id, project_id, config_json, created_at FROM orchestration_runs ORDER BY id"
    ):
        if pid is not None:
            await edge("project", pid, "run", rid, "has_run", ts, project_id=pid)
        team = (json.loads(cfg) if cfg else {}).get("team") if cfg else None
        if team:
            await edge("run", rid, "team", team, "uses_team", ts, project_id=pid, team=team)

    # 4. run -> member (agent runs spawned under an orchestration run)
    for mid, run_id, pid, role, ts in await rows(
        "SELECT id, orchestration_run_id, project_id, role, created_at FROM agent_runs "
        "WHERE orchestration_run_id IS NOT NULL ORDER BY id"
    ):
        await edge("run", run_id, "member", mid, "has_member", ts, project_id=pid,
                   evidence=[{"role": role}] if role else None)

    # 5. project -> task
    for tid, pid, ts in await rows(
        "SELECT id, project_id, created_at FROM tasks WHERE project_id IS NOT NULL ORDER BY id"
    ):
        await edge("project", pid, "task", tid, "has_task", ts, project_id=pid)

    # 6. project -> memory (live only)
    for mid, pid, ts in await rows(
        "SELECT id, project_id, created_at FROM memories "
        "WHERE status='live' AND project_id IS NOT NULL ORDER BY id"
    ):
        await edge("project", pid, "memory", mid, "has_memory", ts, project_id=pid)

    # 7. project -> source (live only; edge carries the source's provenance-mapped trust)
    for sid, pid, kind, review, ts in await rows(
        "SELECT id, project_id, kind, review_status, created_at FROM kb_sources "
        "WHERE status='live' AND project_id IS NOT NULL ORDER BY id"
    ):
        await edge("project", pid, "source", sid, "has_source", ts,
                   trust=_source_trust(kind, review), project_id=pid)

    # 8. project -> artifact   9. artifact -> origin (produced_by, for mapped producers)
    for aid, pid, otype, oid, prov, ts in await rows(
        "SELECT id, project_id, origin_type, origin_id, provenance_class, created_at "
        "FROM artifacts ORDER BY id"
    ):
        atrust = _artifact_trust(prov)
        if pid is not None:
            await edge("project", pid, "artifact", aid, "has_artifact", ts, trust=atrust,
                       project_id=pid)
        dst_kind = _ARTIFACT_ORIGIN_KIND.get(otype)
        if dst_kind and oid:
            await edge("artifact", aid, dst_kind, oid, "produced_by", ts, trust=atrust,
                       project_id=pid)

    # 10. wiki -> wiki (resolved links only; wiki content is human-curated ⇒ trusted_local)
    for frm, to, ts in await rows(
        "SELECT from_path, to_path, created_at FROM kb_wiki_links "
        "WHERE to_path IS NOT NULL ORDER BY id"
    ):
        await edge("wiki", frm, "wiki", to, "links_to", ts)

    return dict(counts)
