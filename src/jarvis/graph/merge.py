"""Dedup candidate detection + merge/split orchestration (Phase 15 Task 9).

Two duplicate asserted entities (two ``person`` nodes for the same human, two ``topic`` nodes for
the same idea) should be one. This module *detects* candidates (report-only — it never mutates) and
*locates* a merge to reverse for ``split``; the actual fold/unfold is the atomic, reversible
:meth:`GraphStore.merge_nodes` / :meth:`GraphStore.undo_merge` (nodes are retracted, never deleted;
edges re-point and can be restored byte-for-byte). CLI-first this phase — there is no merge/split
route, so the graph UI gains no new authority.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from jarvis.graph.store import ANY_PROJECT, GraphStore

# Cosine floor for "these two entities look like the same thing". Deliberately high — this is a
# *suggestion* a human confirms with `kira graph merge`, never an auto-apply.
DEFAULT_THRESHOLD = 0.90


@dataclass
class Candidate:
    """A report-only "these two nodes may be duplicates" pair (never auto-merged)."""

    kind: str
    a_id: int
    a_title: str
    b_id: int
    b_title: str
    reason: str  # "exact-title" | "similar"
    score: float  # 1.0 for an exact title match; cosine for a semantic match


async def find_duplicates(
    store: GraphStore,
    *,
    project_id: object = ANY_PROJECT,
    threshold: float = DEFAULT_THRESHOLD,
    limit: int = 50,
) -> list[Candidate]:
    """Report likely-duplicate asserted node pairs within each kind: exact case-folded title match,
    plus embedding cosine ≥ ``threshold`` for nodes that carry an embedding. REPORT-ONLY — this
    returns candidates for a human to confirm; it performs no writes."""
    nodes = await store.list_nodes(project_id=project_id)
    out: list[Candidate] = []
    seen: set[tuple[int, int]] = set()
    by_kind: dict[str, list] = {}
    for n in nodes:
        by_kind.setdefault(n.kind, []).append(n)
    for kind, group in by_kind.items():
        # exact title-key duplicates (cheap, unambiguous)
        by_key: dict[str, list] = {}
        for n in group:
            by_key.setdefault(n.title.strip().casefold(), []).append(n)
        for members in by_key.values():
            for i in range(len(members)):
                for j in range(i + 1, len(members)):
                    a, b = members[i], members[j]
                    seen.add((a.id, b.id))
                    out.append(Candidate(kind, a.id, a.title, b.id, b.title, "exact-title", 1.0))
        # semantic near-duplicates among embedded nodes (unit vectors ⇒ cosine == dot)
        emb = [n for n in group if n.embedding is not None]
        for i in range(len(emb)):
            for j in range(i + 1, len(emb)):
                a, b = emb[i], emb[j]
                if (a.id, b.id) in seen:
                    continue
                score = float(np.dot(a.embedding, b.embedding))
                if score >= threshold:
                    seen.add((a.id, b.id))
                    out.append(Candidate(kind, a.id, a.title, b.id, b.title, "similar", score))
    out.sort(key=lambda c: (-c.score, c.kind, c.a_id, c.b_id))
    return out[:limit]


async def locate_merge(store: GraphStore, node_id: int) -> int | None:
    """The most recent non-undone merge that folded ``node_id`` (as merged) into a canonical — the
    target of ``split``. Returns its journal id, or None if the node was never merged away."""
    ref = None
    for m in await store.list_merges():  # ascending; last match wins ⇒ most recent
        if m.action == "merge" and m.merged_id == str(node_id):
            ref = m.id
    return ref


async def split(store: GraphStore, node_id: int) -> int | None:
    """Pull ``node_id`` back out of the canonical it was merged into: reverse the most recent merge
    that folded it away. Returns the reversed merge id, or None if there was nothing to split."""
    mid = await locate_merge(store, node_id)
    if mid is None:
        return None
    await store.undo_merge(mid)
    return mid
