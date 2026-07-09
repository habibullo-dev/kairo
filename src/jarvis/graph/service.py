"""GraphService: the read models over the memory graph (Phase 15 Task 3).

Derived nodes are not stored (see builder.py), so the read model RESOLVES an edge endpoint — a
``(kind, ref_id)`` like ``project:3`` / ``run:9`` / ``person:2`` — into a bodies-free node card by
looking up the underlying row (or a code constant, or an asserted graph_nodes row).
Every card carries provenance metadata (trust_class / sensitivity), computed from the source row and
never upgraded. Labels are short (a title, or a truncated memory/source snippet) — never a prompt,
report body, secret, or key.

Read-only: nothing here mutates, reaches a tool, or feeds a model prompt. The subgraph is
project-scoped (only that project's edges), depth- and size-clamped, so it can never fan out into a
whole-corpus hairball or another project's data.
"""

from __future__ import annotations

import contextlib
from collections import Counter, defaultdict

from jarvis.graph.builder import _artifact_trust, _source_trust
from jarvis.graph.store import GraphStore

MAX_DEPTH = 2
MAX_NODES = 300
_MEM_LABEL = 80  # truncate a memory/source snippet to a short label (bodies-free)

# Endpoint kinds whose trust is trusted_local structure (first-party rows / code constants).
_LOCAL_KINDS = {"project", "chat", "run", "member", "task", "digest", "wiki", "team", "service"}
_ASSERTED_KINDS = {"person", "decision", "topic", "external_ref", "custom"}


def _clamp(value: object, lo: int, hi: int, default: int) -> int:
    try:
        return max(lo, min(hi, int(value)))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _ep(kind: str, ref_id: str) -> str:
    return f"{kind}:{ref_id}"


async def _resolve(store: GraphStore, endpoints: set[tuple[str, str]]) -> dict[tuple, dict]:
    """Resolve each endpoint to a bodies-free card {kind, ref_id, label, trust_class, sensitivity}.
    Unknown/deleted rows are simply omitted (a dangling edge draws no node)."""
    db = store.db
    by_kind: dict[str, list[str]] = defaultdict(list)
    for kind, rid in endpoints:
        by_kind[kind].append(rid)
    out: dict[tuple, dict] = {}

    def card(kind, rid, label, trust, sensitivity=None):
        out[(kind, rid)] = {
            "kind": kind, "ref_id": rid, "id": _ep(kind, rid), "label": label or f"{kind} {rid}",
            "trust_class": trust, "sensitivity": sensitivity,
        }

    async def int_rows(kind: str, sql: str):
        """Run an IN-query for int-id rows of `kind`; yields each row. Non-int ids are skipped."""
        ids = [r for r in by_kind.get(kind, []) if r.lstrip("-").isdigit()]
        if not ids:
            return []
        marks = ", ".join("?" for _ in ids)
        return await (await db.execute(sql.format(marks=marks), [int(i) for i in ids])).fetchall()

    for pid, name in await int_rows(
        "project", "SELECT id, name FROM projects WHERE id IN ({marks})"):
        card("project", str(pid), name, "trusted_local")
    for sid, title in await int_rows(
        "chat", "SELECT id, title FROM sessions WHERE id IN ({marks})"):
        card("chat", str(sid), title or "Chat", "trusted_local")
    for rid, title in await int_rows(
        "run", "SELECT id, title FROM orchestration_runs WHERE id IN ({marks})"):
        card("run", str(rid), title, "trusted_local")
    for mid, title in await int_rows(
        "member", "SELECT id, title FROM agent_runs WHERE id IN ({marks})"):
        card("member", str(mid), title, "trusted_local")
    for tid, title in await int_rows("task", "SELECT id, title FROM tasks WHERE id IN ({marks})"):
        card("task", str(tid), title, "trusted_local")
    for mid, content in await int_rows(
        "memory", "SELECT id, content FROM memories WHERE id IN ({marks})"
    ):
        card("memory", str(mid), (content or "")[:_MEM_LABEL], "trusted_local")
    for sid, title, origin, kind, review in await int_rows(
        "source",
        "SELECT id, title, origin, kind, review_status FROM kb_sources WHERE id IN ({marks})"):
        card("source", str(sid), title or (origin or "")[:_MEM_LABEL], _source_trust(kind, review))
    for aid, title, prov, sens in await int_rows(
        "artifact",
        "SELECT id, title, provenance_class, sensitivity FROM artifacts WHERE id IN ({marks})",
    ):
        card("artifact", str(aid), title, _artifact_trust(prov), sens)
    for did, in await int_rows("digest", "SELECT id FROM digests WHERE id IN ({marks})"):
        card("digest", str(did), f"Digest {did}", "trusted_local")

    # Constant / path kinds: label from the ref itself (no row lookup).
    for rid in by_kind.get("wiki", []):
        card("wiki", rid, rid.rsplit("/", 1)[-1], "trusted_local")
    for rid in by_kind.get("team", []):
        card("team", rid, rid.replace("_", " ").title(), "trusted_local")
    for rid in by_kind.get("service", []):
        card("service", rid, rid.replace("_", " ").title(), "trusted_local")

    # Asserted graph_nodes (people/decisions/topics/refs) — trust from the row, live only.
    asserted_ids = [rid for k in _ASSERTED_KINDS for rid in by_kind.get(k, []) if rid.isdigit()]
    if asserted_ids:
        marks = ", ".join("?" for _ in asserted_ids)
        rows = await (await db.execute(
            f"SELECT id, kind, title, trust_class, sensitivity FROM graph_nodes "
            f"WHERE id IN ({marks}) AND status='live'", [int(i) for i in asserted_ids])).fetchall()
        for gid, kind, title, trust, sens in rows:
            card(kind, str(gid), title, trust, sens)
    return out


def _edge_dict(e) -> dict:
    return {
        "src": _ep(e.src_kind, e.src_id), "dst": _ep(e.dst_kind, e.dst_id),
        "edge_kind": e.edge_kind, "origin": e.origin, "trust_class": e.trust_class,
        "created_at": e.created_at,
    }


async def subgraph(
    store: GraphStore,
    project_id: int,
    *,
    focus: tuple[str, str] | None = None,
    depth: object = 1,
    kinds: set[str] | None = None,
    trust: set[str] | None = None,
    since: str | None = None,
    limit: object = MAX_NODES,
) -> dict:
    """A project-scoped, depth/size-clamped neighborhood around ``focus`` (default the project
    node). Returns resolved nodes + the edges among them + filter counts. Read-only."""
    depth = _clamp(depth, 0, MAX_DEPTH, 1)
    limit = _clamp(limit, 1, MAX_NODES, MAX_NODES)
    focus = focus or ("project", str(project_id))

    edges = await store.list_edges(project_id=project_id, include_global=False)
    if since:
        edges = [e for e in edges if e.created_at >= since]
    adj: dict[tuple, list] = defaultdict(list)
    for e in edges:
        adj[(e.src_kind, e.src_id)].append(e)
        adj[(e.dst_kind, e.dst_id)].append(e)

    # BFS from focus out to `depth` hops.
    seen: set[tuple] = {focus}
    frontier = [focus]
    kept: dict[int, object] = {}
    for _ in range(depth):
        nxt: list[tuple] = []
        for node in frontier:
            for e in adj.get(node, []):
                kept[e.id] = e
                for end in ((e.src_kind, e.src_id), (e.dst_kind, e.dst_id)):
                    if end not in seen:
                        seen.add(end)
                        nxt.append(end)
        frontier = nxt

    resolved = await _resolve(store, seen)
    nodes = [dict(resolved[ep], degree=len(adj.get(ep, []))) for ep in resolved]
    if kinds:
        nodes = [n for n in nodes if n["kind"] in kinds]
    if trust:
        nodes = [n for n in nodes if n["trust_class"] in trust]
    nodes.sort(key=lambda n: (-n["degree"], n["id"]))  # most-connected first (stable)
    truncated = len(nodes) > limit
    nodes = nodes[:limit]

    keys = {(n["kind"], n["ref_id"]) for n in nodes}
    out_edges = [
        _edge_dict(e) for e in kept.values()
        if (e.src_kind, e.src_id) in keys and (e.dst_kind, e.dst_id) in keys
    ]
    return {
        "project_id": project_id, "focus": _ep(*focus), "depth": depth, "nodes": nodes,
        "edges": out_edges, "truncated": truncated,
        "counts": {
            "by_kind": dict(Counter(n["kind"] for n in nodes)),
            "by_trust": dict(Counter(n["trust_class"] for n in nodes)),
        },
    }


async def node_card(
    store: GraphStore, kind: str, ref_id: str, *, neighbor_cap: int = 60
) -> dict | None:
    """One node's card: its resolved metadata + capped neighbors (each a resolved mini-card with the
    connecting edge). Bodies-free. None if the node cannot be resolved."""
    resolved = await _resolve(store, {(kind, ref_id)})
    self_card = resolved.get((kind, ref_id))
    if self_card is None:
        return None
    edges = await store.neighbors(kind, ref_id)
    ends = {(e.dst_kind, e.dst_id) if (e.src_kind, e.src_id) == (kind, ref_id)
            else (e.src_kind, e.src_id) for e in edges}
    end_cards = await _resolve(store, ends)
    neighbors = []
    for e in edges[:neighbor_cap]:
        other = ((e.dst_kind, e.dst_id) if (e.src_kind, e.src_id) == (kind, ref_id)
                 else (e.src_kind, e.src_id))
        card = end_cards.get(other)
        if card:
            outward = (e.src_kind, e.src_id) == (kind, ref_id)
            neighbors.append({"edge_kind": e.edge_kind, "origin": e.origin,
                              "direction": "out" if outward else "in", "node": card})
    return {**self_card, "neighbor_count": len(edges), "neighbors": neighbors}


async def suggestions_view(store: GraphStore, project_id: int, *, status: str = "pending") -> dict:
    """The review queue for a project: bodies-free suggestion rows (short preview + evidence
    POINTERS + trust badge) for the Memory-tab 'Suggested' section / `jarvis graph review`."""
    rows = []
    for s in await store.list_suggestions(project_id=project_id, status=status):
        payload = s.payload or {}
        preview = str(payload.get("content") or payload.get("title") or "")[:_MEM_LABEL]
        rows.append({
            "id": s.id, "kind": s.kind, "trust_class": s.trust_class, "sensitivity": s.sensitivity,
            "preview": preview, "evidence": s.evidence, "extractor_model": s.extractor_model,
            "created_at": s.created_at,
        })
    return {
        "project_id": project_id, "suggestions": rows,
        "counts": {"by_trust": dict(Counter(r["trust_class"] for r in rows))},
    }


async def counts_for_project(store: GraphStore, project_id: int) -> dict:
    """Bare node/edge counts for a project (a cheap header stat). Read-only, degrades to zero."""
    out = {"nodes": 0, "edges": 0}
    with contextlib.suppress(Exception):
        sg = await subgraph(store, project_id, depth=MAX_DEPTH)
        out = {"nodes": len(sg["nodes"]), "edges": len(sg["edges"])}
    return out
