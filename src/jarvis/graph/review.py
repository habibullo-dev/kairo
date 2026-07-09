"""Suggestion review (Phase 15 Task 5) — the ONLY door from a quarantined proposal to durable graph
truth, and it requires an explicit human action (the approve/reject routes or ``jarvis graph
review``). There is no automatic path.

* **reject** flips the suggestion to ``rejected`` (terminal). Nothing is materialized.
* **approve** CLAIMS the suggestion first (the atomic ``pending -> approved`` transition; a second
  approve is a no-op, so a proposal can never materialize twice), then materializes it:
    - ``memory`` -> a real ``memories`` row (``source='reviewed_suggestion'``). It is stored with a
      placeholder embedding + the ``unindexed`` model sentinel, so it is immediately FTS-searchable
      but excluded from semantic recall until ``jarvis graph reindex`` (Task 6) embeds it — approve
      stays fast, keyless, and never makes a surprise model call.
    - ``node`` -> an asserted ``graph_nodes`` row (``created_by='user'``, trust from suggestion).
    - ``edge`` -> an asserted ``graph_edges`` row (``origin='asserted'``).
  Trust is carried through from the suggestion (worst-of-evidence) and never upgraded on approval.
"""

from __future__ import annotations

import datetime as _dt

from jarvis.graph.store import GraphStore
from jarvis.memory.store import MemoryStore

UNINDEXED = "unindexed"  # embedding_model sentinel: a memory awaiting a real embedding (Task 6)


def _now() -> str:
    return _dt.datetime.now(_dt.UTC).isoformat()


async def _materialize(store: GraphStore, s) -> dict:
    """Turn an approved suggestion into a durable row. Trust flows from the suggestion unchanged."""
    payload = s.payload or {}
    if s.kind == "memory":
        mem = MemoryStore(store.db, store.lock)
        mid = await mem.add(
            type=str(payload.get("type") or "fact"), content=str(payload.get("content") or ""),
            embedding=[0.0], embedding_model=UNINDEXED, source="reviewed_suggestion",
            project_id=s.project_id,
        )
        return {"materialized": "memory", "id": mid}
    if s.kind == "node":
        nid = await store.add_node(
            kind=str(payload.get("kind") or "topic"), title=str(payload.get("title") or "untitled"),
            summary=str(payload.get("summary") or ""), trust_class=s.trust_class, created_by="user",
            project_id=s.project_id, sensitivity=s.sensitivity, source_kind="reviewed_suggestion",
        )
        return {"materialized": "node", "id": nid}
    if s.kind == "edge":
        src, dst = str(payload.get("src") or ""), str(payload.get("dst") or "")
        if ":" not in src or ":" not in dst:
            return {"materialized": None, "reason": "edge payload needs src/dst as 'kind:id'"}
        sk, si = src.split(":", 1)
        dk, di = dst.split(":", 1)
        await store.upsert_edge(
            src_kind=sk, src_id=si, dst_kind=dk, dst_id=di,
            edge_kind=str(payload.get("edge_kind") or "relates_to"), origin="asserted",
            trust_class=s.trust_class, created_by="user", created_at=_now(),
            project_id=s.project_id, sensitivity=s.sensitivity,
        )
        return {"materialized": "edge", "src": src, "dst": dst}
    return {"materialized": None, "reason": f"unknown suggestion kind {s.kind!r}"}


async def approve(store: GraphStore, sugg_id: int, *, resolved_by: str) -> dict:
    """Claim + materialize a pending suggestion. Idempotent: a re-approve of an already-resolved
    suggestion does nothing (``ok=False``), so nothing is ever materialized twice."""
    s = await store.get_suggestion(sugg_id)
    if s is None:
        return {"ok": False, "reason": "not found"}
    if s.status != "pending":
        return {"ok": False, "reason": f"already {s.status}"}
    claimed = await store.resolve_suggestion(sugg_id, status="approved", resolved_by=resolved_by)
    if not claimed:  # lost a race — someone else just resolved it
        return {"ok": False, "reason": "already resolved"}
    result = await _materialize(store, s)
    return {"ok": True, "id": sugg_id, **result}


async def reject(store: GraphStore, sugg_id: int, *, resolved_by: str) -> dict:
    s = await store.get_suggestion(sugg_id)
    if s is None:
        return {"ok": False, "reason": "not found"}
    ok = await store.resolve_suggestion(sugg_id, status="rejected", resolved_by=resolved_by)
    return {"ok": ok, "id": sugg_id, "reason": None if ok else f"already {s.status}"}
