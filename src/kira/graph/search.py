"""Unified semantic + graph search (Phase 15 Task 6).

One query over the project's world: keyword hits from the FTS domains + semantic (cosine) hits from
the graph entities and memories, merged into ranked, trust-badged node cards, with a 1-hop graph
expansion ("connected: …") on the top hits. Quarantine-aware at every layer — the FTS domains are
status/review-filtered in SQL, the entities index only holds live asserted nodes, and quarantined
suggestions are in a separate un-indexed table, so a pending/untrusted item can never surface as a
trusted result.

The embedder is injected (a CostAwareEmbedder in production; a fake in tests), so search is testable
without a live model. If no embedder is given (or the query is empty) it degrades to FTS only.
"""

from __future__ import annotations

import contextlib

import numpy as np

from kira.graph.service import _resolve
from kira.graph.store import GraphStore
from kira.memory.store import ANY_PROJECT, MemoryStore
from kira.persistence.fts import query_domain

# FTS domain -> the graph endpoint kind its row ids address directly.
_DOMAIN_KIND = {
    "chats": "chat", "memories": "memory", "tasks": "task", "orchestration": "run",
    "artifacts": "artifact", "digests": "digest",
}
_FTS_LIMIT = 12
_SEM_TOPK = 12


async def _fts_endpoints(db, query, project_id) -> dict[tuple, float]:
    """Keyword hits from every domain, mapped to graph endpoints. bm25 is lower-is-better, so we
    negate it into a positive 'fts score' for blending."""
    out: dict[tuple, float] = {}
    for domain, kind in _DOMAIN_KIND.items():
        for rid, score in await query_domain(db, domain, query, project_id=project_id,
                                             limit=_FTS_LIMIT):
            out[(kind, str(rid))] = max(out.get((kind, str(rid)), 0.0), -float(score))
    # entities: resolve each hit's real kind (person/decision/topic/…) from graph_nodes.
    ent = await query_domain(db, "entities", query, project_id=project_id, limit=_FTS_LIMIT)
    if ent:
        ids = [gid for gid, _ in ent]
        marks = ", ".join("?" for _ in ids)
        kinds = dict(await (await db.execute(
            f"SELECT id, kind FROM graph_nodes WHERE id IN ({marks})", ids)).fetchall())
        for gid, score in ent:
            if gid in kinds:
                key = (kinds[gid], str(gid))
                out[key] = max(out.get(key, 0.0), -float(score))
    # knowledge: FTS returns kb_chunks ids — map each to its source (or wiki page).
    kn = await query_domain(db, "knowledge", query, project_id=project_id, limit=_FTS_LIMIT)
    if kn:
        ids = [cid for cid, _ in kn]
        marks = ", ".join("?" for _ in ids)
        owners = {r[0]: (r[1], r[2]) for r in await (await db.execute(
            f"SELECT id, source_id, wiki_path FROM kb_chunks WHERE id IN ({marks})",
            ids)).fetchall()}
        for cid, score in kn:
            src_id, wiki = owners.get(cid, (None, None))
            ep = ("source", str(src_id)) if src_id is not None else ("wiki", wiki) if wiki else None
            if ep:
                out[ep] = max(out.get(ep, 0.0), -float(score))
    return out


async def _semantic_endpoints(store, embedder, query, project_id) -> dict[tuple, float]:
    """Cosine hits from graph entities + memories (same embedding model as the query)."""
    out: dict[tuple, float] = {}
    with contextlib.suppress(Exception):
        qv = np.asarray(await embedder.embed_query(query), dtype=np.float32)
        n = float(np.linalg.norm(qv))
        qunit = qv / n if n > 0 else qv
        # entities: live nodes (project + global) with an embedding in this model.
        for node in await store.list_nodes(project_id=project_id):
            if node.embedding is not None and node.embedding_model == embedder.model \
                    and node.embedding.shape == qunit.shape:
                out[(node.kind, str(node.id))] = float(node.embedding @ qunit)
        # memories via the store's own cosine search (model-filtered, scope-aware).
        mem = MemoryStore(store.db, store.lock)
        scope = project_id if project_id is not None else ANY_PROJECT
        for sm in await mem.search(qunit, embedder.model, top_k=_SEM_TOPK, min_similarity=0.0,
                                   project_id=scope):
            out[("memory", str(sm.memory.id))] = sm.score
    return out


async def unified_search(
    store: GraphStore, embedder, query: str, *, project_id=None, limit: int = 20,
    expand: bool = True,
) -> dict:
    """Ranked, trust-badged, quarantine-aware results with optional 1-hop graph expansion."""
    limit = max(1, min(100, int(limit)))
    db = store.db
    fts = await _fts_endpoints(db, query, project_id)
    sem = (await _semantic_endpoints(store, embedder, query, project_id)
           if (embedder and query.strip()) else {})

    endpoints = set(fts) | set(sem)
    cards = await _resolve(store, endpoints)  # bodies-free label + trust per endpoint
    results = []
    for ep in endpoints:
        card = cards.get(ep)
        if card is None:
            continue
        # blend: semantic dominates when present; fts breaks ties. (both already positive)
        score = sem.get(ep, 0.0) * 2.0 + fts.get(ep, 0.0)
        sources = [s for s, present in (("semantic", ep in sem), ("fts", ep in fts)) if present]
        results.append({**card, "score": round(score, 5), "sources": sources})
    results.sort(key=lambda r: (-r["score"], r["id"]))
    results = results[:limit]

    if expand:
        for r in results[:8]:  # 1-hop "connected" on the strongest hits
            connected = []
            for e in await store.neighbors(r["kind"], r["ref_id"]):
                outward = (e.src_kind, e.src_id) == (r["kind"], r["ref_id"])
                other = (e.dst_kind, e.dst_id) if outward else (e.src_kind, e.src_id)
                oc = cards.get(other) or (await _resolve(store, {other})).get(other)
                if oc:
                    connected.append({"edge_kind": e.edge_kind, "node": oc})
            r["connected"] = connected[:8]
    return {"query": query, "project_id": project_id, "results": results, "count": len(results)}
