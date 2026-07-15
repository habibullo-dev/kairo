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
from pathlib import PurePosixPath

from kira.graph.code_dependencies import SourceHead, local_import_pairs
from kira.graph.store import GraphStore

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


def _source_folder_parts(title: str | None) -> tuple[str, ...]:
    """Return a browser-supplied *logical* source path's folder components.

    Chat folder ingestion stores a relative path as the source title.  It is useful
    structure, but it is not a server filesystem path and must never be treated as
    one.  Single-file uploads and malformed/legacy titles simply have no folders.
    """
    raw = str(title or "").replace("\\", "/").strip("/")
    if not raw:
        return ()
    path = PurePosixPath(raw)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        return ()
    return tuple(path.parts[:-1])


def _folder_ref(project_id: int, parts: tuple[str, ...]) -> str:
    """A project-qualified, stable derived-folder endpoint (never a disk path)."""
    return f"{project_id}:{'/'.join(parts)}"


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

    # 7. project -> source, with a deterministic folder hierarchy for browser folder imports.
    # Every edge is a rebuildable structural cache.  ``folder`` endpoints are logical labels from
    # the user-selected relative path, not real filesystem paths and not inferred dependencies.
    source_meta: dict[int, tuple[int, str | None, str, str, str]] = {}
    for sid, pid, title, kind, review, ts in await rows(
        "SELECT id, project_id, title, kind, review_status, created_at FROM kb_sources "
        "WHERE status='live' AND project_id IS NOT NULL ORDER BY id"
    ):
        source_meta[sid] = (pid, title, kind, review, ts)
        source_trust = _source_trust(kind, review)
        folders = _source_folder_parts(title)
        if not folders:
            await edge("project", pid, "source", sid, "has_source", ts,
                       trust=source_trust, project_id=pid)
            continue
        parent_kind, parent_id = "project", str(pid)
        for index in range(1, len(folders) + 1):
            folder_id = _folder_ref(pid, folders[:index])
            await edge(parent_kind, parent_id, "folder", folder_id, "contains", ts,
                       project_id=pid)
            parent_kind, parent_id = "folder", folder_id
        await edge("folder", parent_id, "source", sid, "contains", ts,
                   trust=source_trust, project_id=pid)

    # 7b. source -> source for deterministic local code imports.  Uploaded code is treated only
    # as inert text: the parser inspects a bounded first chunk, executes nothing, and emits an
    # edge only when an import resolves to another live source in the *same* project.  Dynamic,
    # external, and alias-based imports remain unresolved rather than guessed.
    heads_by_project: dict[int, list[SourceHead]] = {}
    for sid, title, text in await rows(
        "SELECT s.id, s.title, c.text FROM kb_sources s "
        "LEFT JOIN kb_chunks c ON c.source_id=s.id AND c.seq=0 "
        "WHERE s.status='live' AND s.project_id IS NOT NULL ORDER BY s.id"
    ):
        meta = source_meta.get(sid)
        if meta is not None:
            heads_by_project.setdefault(meta[0], []).append(SourceHead(sid, title, text))
    for pid, heads in sorted(heads_by_project.items()):
        for importer, imported in local_import_pairs(heads):
            importer_meta, imported_meta = source_meta[importer], source_meta[imported]
            # Both endpoints are project-local by construction.  The relationship cannot be more
            # trusted than either source; a reviewed local upload stays "reviewed", not upgraded
            # to trusted_local merely because it has a structural edge.
            trust = (
                "untrusted_external"
                if "untrusted_external" in {
                    _source_trust(importer_meta[2], importer_meta[3]),
                    _source_trust(imported_meta[2], imported_meta[3]),
                }
                else "reviewed"
            )
            await edge("source", importer, "source", imported, "imports", importer_meta[4],
                       trust=trust, project_id=pid)

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
