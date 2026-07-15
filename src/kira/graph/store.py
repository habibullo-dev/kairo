"""GraphStore: the SQLite persistence layer for the memory graph (schema v12).

Mirrors :class:`~kira.memory.store.MemoryStore`'s discipline — plain SQL, one shared aiosqlite
connection + write lock, numpy embeddings as unit-vector BLOBs — with the graph's own invariants:

* **Asserted rows are NEVER deleted.** :meth:`retract_node` / :meth:`retract_edge` flip a status;
  the row (and its lineage) stays for audit. Only ``status='live'`` participates in reads.
* **Derived edges are a rebuildable cache.** :meth:`delete_derived_edges` is the ONE sanctioned
  bulk delete (``origin='derived'`` only — it can never touch an asserted row), used by the builder
  to re-derive. Derived rows carry their SOURCE row's ``created_at`` (never wall-clock), so a
  rebuild is byte-identical (the Phase-14 determinism lesson).
* **Suggestions are quarantined.** They live in their own table with no FTS index and no retrieval
  path; :meth:`resolve_suggestion` is the only transition out of ``pending``, and materialization
  into a real memory/asserted node|edge happens in the review layer, never here automatically.

Trust is provenance, not decoration: ``trust_class`` on a node/edge is set by its creator from the
underlying source and can never exceed it (enforced by the builder / suggestion pipeline, pinned by
tests). This layer stores it faithfully; it does not upgrade it.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import json
from dataclasses import dataclass, field

import aiosqlite
import numpy as np

# Scope sentinel (mirrors MemoryStore.ANY_PROJECT): distinct from None (== global only).
ANY_PROJECT: object = object()

TRUST_CLASSES = ("trusted_local", "reviewed", "untrusted_external", "model_generated")

_NODE_COLS = (
    "id, kind, title, summary, embedding, embedding_model, content_hash, project_id, "
    "trust_class, sensitivity, source_kind, created_by, model, status, labels_json, "
    "created_at, updated_at"
)
_EDGE_COLS = (
    "id, src_kind, src_id, dst_kind, dst_id, edge_kind, origin, project_id, trust_class, "
    "sensitivity, created_by, model, team, evidence_json, status, created_at"
)
_SUGG_COLS = (
    "id, kind, payload_json, evidence_json, project_id, trust_class, sensitivity, "
    "extractor_model, est_cost_usd, status, created_at, resolved_at, resolved_by"
)
_MERGE_COLS = (
    "id, action, canonical_kind, canonical_id, merged_kind, merged_id, undo_json, "
    "created_by, created_at, undone_at"
)


def _now() -> str:
    return _dt.datetime.now(_dt.UTC).isoformat()


def _to_blob(vec: np.ndarray | list[float] | None) -> bytes | None:
    """Unit-normalize to a float32 BLOB (cosine = dot product later). None stays None."""
    if vec is None:
        return None
    arr = np.asarray(vec, dtype=np.float32)
    norm = float(np.linalg.norm(arr))
    unit = arr / norm if norm > 0 else arr
    return unit.tobytes()


def _from_blob(blob: bytes | None) -> np.ndarray | None:
    return np.frombuffer(blob, dtype=np.float32) if blob else None


def _loads(text: str | None, default):
    if not text:
        return default
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return default


def _scope(project_col: str, project_id: object, *, include_global: bool) -> tuple[str, list]:
    """`` AND …`` project-scope fragment (mirrors MemoryStore._scope_clause)."""
    if project_id is ANY_PROJECT:
        return "", []
    if project_id is None:
        return f" AND {project_col} IS NULL", []
    if include_global:
        return f" AND ({project_col} = ? OR {project_col} IS NULL)", [project_id]
    return f" AND {project_col} = ?", [project_id]


@dataclass
class GraphNode:
    """An asserted graph entity (people/decisions/topics/refs) — a ``graph_nodes`` row."""

    id: int
    kind: str
    title: str
    summary: str
    embedding: np.ndarray | None
    embedding_model: str | None
    content_hash: str | None
    project_id: int | None
    trust_class: str
    sensitivity: str | None
    source_kind: str | None
    created_by: str
    model: str | None
    status: str
    labels: list
    created_at: str
    updated_at: str

    @property
    def ref(self) -> str:
        """Its endpoint address on an edge, e.g. ``person:2``."""
        return f"{self.kind}:{self.id}"


@dataclass
class GraphEdge:
    """A relationship between two endpoints (derived cache row or asserted)."""

    id: int
    src_kind: str
    src_id: str
    dst_kind: str
    dst_id: str
    edge_kind: str
    origin: str
    project_id: int | None
    trust_class: str
    sensitivity: str | None
    created_by: str
    model: str | None
    team: str | None
    evidence: list
    status: str
    created_at: str


@dataclass
class GraphSuggestion:
    """A quarantined proposal awaiting human review (a ``graph_suggestions`` row)."""

    id: int
    kind: str
    payload: dict
    evidence: list
    project_id: int | None
    trust_class: str
    sensitivity: str | None
    extractor_model: str | None
    est_cost_usd: float | None
    status: str
    created_at: str
    resolved_at: str | None
    resolved_by: str | None


@dataclass
class GraphMerge:
    """One reversible dedup action (a ``graph_merges`` journal row)."""

    id: int
    action: str
    canonical_kind: str
    canonical_id: str
    merged_kind: str
    merged_id: str
    undo: dict
    created_by: str
    created_at: str
    undone_at: str | None


@dataclass
class GraphStore:
    db: aiosqlite.Connection
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)  # share with the other stores

    # --- asserted nodes (never-DELETE) ------------------------------------
    async def add_node(
        self,
        *,
        kind: str,
        title: str,
        summary: str = "",
        trust_class: str,
        created_by: str,
        project_id: int | None = None,
        sensitivity: str | None = None,
        source_kind: str | None = None,
        model: str | None = None,
        labels: list | None = None,
        embedding: np.ndarray | list[float] | None = None,
        embedding_model: str | None = None,
        content_hash: str | None = None,
    ) -> int:
        now = _now()
        async with self.lock:
            cur = await self.db.execute(
                f"INSERT INTO graph_nodes ({_NODE_COLS}) VALUES "
                "(NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'live', ?, ?, ?)",
                (
                    kind, title, summary, _to_blob(embedding), embedding_model, content_hash,
                    project_id, trust_class, sensitivity, source_kind, created_by, model,
                    json.dumps(labels or []), now, now,
                ),
            )
            await self.db.commit()
        assert cur.lastrowid is not None
        return cur.lastrowid

    async def get_node(self, node_id: int) -> GraphNode | None:
        cur = await self.db.execute(
            f"SELECT {_NODE_COLS} FROM graph_nodes WHERE id=?", (node_id,)
        )
        row = await cur.fetchone()
        return _row_to_node(row) if row else None

    async def list_nodes(
        self,
        *,
        project_id: object = ANY_PROJECT,
        include_global: bool = True,
        kind: str | None = None,
        status: str = "live",
    ) -> list[GraphNode]:
        scope_sql, params = _scope("project_id", project_id, include_global=include_global)
        kind_sql = " AND kind=?" if kind else ""
        if kind:
            params.append(kind)
        cur = await self.db.execute(
            f"SELECT {_NODE_COLS} FROM graph_nodes WHERE status=?{kind_sql}{scope_sql} ORDER BY id",
            (status, *params),
        )
        return [_row_to_node(r) for r in await cur.fetchall()]

    async def update_node(
        self, node_id: int, *, title: str | None = None, summary: str | None = None
    ) -> None:
        sets, params = [], []
        if title is not None:
            sets.append("title=?")
            params.append(title)
        if summary is not None:
            sets.append("summary=?")
            params.append(summary)
        if not sets:
            return
        sets.append("updated_at=?")
        params.extend([_now(), node_id])
        async with self.lock:
            await self.db.execute(
                f"UPDATE graph_nodes SET {', '.join(sets)} WHERE id=?", tuple(params)
            )
            await self.db.commit()

    async def set_embedding(
        self, node_id: int, vec: np.ndarray | list[float], model: str, content_hash: str
    ) -> None:
        """Cache a node's embedding (Task 6 indexing). Re-embed only when content_hash changes."""
        async with self.lock:
            await self.db.execute(
                "UPDATE graph_nodes SET embedding=?, embedding_model=?, content_hash=?, "
                "updated_at=? WHERE id=?",
                (_to_blob(vec), model, content_hash, _now(), node_id),
            )
            await self.db.commit()

    async def retract_node(self, node_id: int) -> bool:
        """Retract (never delete) a live asserted node. True if a live node was retracted."""
        async with self.lock:
            cur = await self.db.execute(
                "UPDATE graph_nodes SET status='retracted', updated_at=? "
                "WHERE id=? AND status='live'",
                (_now(), node_id),
            )
            await self.db.commit()
        return cur.rowcount > 0

    # --- edges (derived cache + asserted) ---------------------------------
    async def upsert_edge(
        self,
        *,
        src_kind: str,
        src_id: str,
        dst_kind: str,
        dst_id: str,
        edge_kind: str,
        origin: str,
        trust_class: str,
        created_by: str,
        created_at: str,
        project_id: int | None = None,
        sensitivity: str | None = None,
        model: str | None = None,
        team: str | None = None,
        evidence: list | None = None,
    ) -> None:
        """Idempotent by the identity index (src,dst,edge_kind,origin). ``created_at`` is passed in
        (the builder passes the SOURCE row's time — never wall-clock — so rebuilds are stable);
        it is preserved on conflict. Metadata is refreshed; status returns to 'live'."""
        async with self.lock:
            await self.db.execute(
                f"INSERT INTO graph_edges ({_EDGE_COLS}) VALUES "
                "(NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'live', ?) "
                "ON CONFLICT(src_kind, src_id, dst_kind, dst_id, edge_kind, origin) DO UPDATE SET "
                "trust_class=excluded.trust_class, sensitivity=excluded.sensitivity, "
                "model=excluded.model, team=excluded.team, evidence_json=excluded.evidence_json, "
                "status='live'",
                (
                    src_kind, src_id, dst_kind, dst_id, edge_kind, origin, project_id, trust_class,
                    sensitivity, created_by, model, team, json.dumps(evidence or []), created_at,
                ),
            )
            await self.db.commit()

    async def neighbors(self, kind: str, ref_id: str, *, status: str = "live") -> list[GraphEdge]:
        """Live edges touching (kind, ref_id) on EITHER endpoint."""
        cur = await self.db.execute(
            f"SELECT {_EDGE_COLS} FROM graph_edges WHERE status=? AND "
            "((src_kind=? AND src_id=?) OR (dst_kind=? AND dst_id=?)) ORDER BY id",
            (status, kind, ref_id, kind, ref_id),
        )
        return [_row_to_edge(r) for r in await cur.fetchall()]

    async def list_edges(
        self,
        *,
        project_id: object = ANY_PROJECT,
        include_global: bool = True,
        origin: str | None = None,
        status: str = "live",
    ) -> list[GraphEdge]:
        scope_sql, scope_params = _scope(
            "project_id", project_id, include_global=include_global
        )
        origin_sql = " AND origin=?" if origin else ""
        params: list[object] = [status]
        if origin:
            params.append(origin)
        params.extend(scope_params)
        cur = await self.db.execute(
            f"SELECT {_EDGE_COLS} FROM graph_edges WHERE status=?{origin_sql}{scope_sql} "
            "ORDER BY id",
            tuple(params),
        )
        return [_row_to_edge(r) for r in await cur.fetchall()]

    async def retract_edge(self, edge_id: int) -> bool:
        async with self.lock:
            cur = await self.db.execute(
                "UPDATE graph_edges SET status='retracted' WHERE id=? AND status='live'", (edge_id,)
            )
            await self.db.commit()
        return cur.rowcount > 0

    async def delete_derived_edges(self) -> int:
        """The ONE sanctioned bulk delete: clears the derived cache (origin='derived' ONLY) so the
        builder can re-derive from scratch. Asserted rows can never be touched here."""
        async with self.lock:
            cur = await self.db.execute("DELETE FROM graph_edges WHERE origin='derived'")
            await self.db.commit()
        return cur.rowcount

    # --- suggestions (quarantined) ----------------------------------------
    async def add_suggestion(
        self,
        *,
        kind: str,
        payload: dict,
        trust_class: str,
        project_id: int | None = None,
        evidence: list | None = None,
        sensitivity: str | None = None,
        extractor_model: str | None = None,
        est_cost_usd: float | None = None,
    ) -> int:
        async with self.lock:
            cur = await self.db.execute(
                f"INSERT INTO graph_suggestions ({_SUGG_COLS}) VALUES "
                "(NULL, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, NULL, NULL)",
                (
                    kind, json.dumps(payload), json.dumps(evidence or []), project_id, trust_class,
                    sensitivity, extractor_model, est_cost_usd, _now(),
                ),
            )
            await self.db.commit()
        assert cur.lastrowid is not None
        return cur.lastrowid

    async def get_suggestion(self, sugg_id: int) -> GraphSuggestion | None:
        cur = await self.db.execute(
            f"SELECT {_SUGG_COLS} FROM graph_suggestions WHERE id=?", (sugg_id,)
        )
        row = await cur.fetchone()
        return _row_to_suggestion(row) if row else None

    async def list_suggestions(
        self,
        *,
        project_id: object = ANY_PROJECT,
        include_global: bool = True,
        status: str = "pending",
    ) -> list[GraphSuggestion]:
        scope_sql, params = _scope("project_id", project_id, include_global=include_global)
        cur = await self.db.execute(
            f"SELECT {_SUGG_COLS} FROM graph_suggestions WHERE status=?{scope_sql} ORDER BY id",
            (status, *params),
        )
        return [_row_to_suggestion(r) for r in await cur.fetchall()]

    async def resolve_suggestion(self, sugg_id: int, *, status: str, resolved_by: str) -> bool:
        """The ONLY transition out of 'pending' → 'approved'|'rejected'. Idempotent: a
        second resolve of an already-resolved suggestion is a no-op (returns False)."""
        if status not in ("approved", "rejected"):
            raise ValueError(f"resolve status must be approved|rejected, got {status!r}")
        async with self.lock:
            cur = await self.db.execute(
                "UPDATE graph_suggestions SET status=?, resolved_at=?, resolved_by=? "
                "WHERE id=? AND status='pending'",
                (status, _now(), resolved_by, sugg_id),
            )
            await self.db.commit()
        return cur.rowcount > 0

    # --- merge/split journal ----------------------------------------------
    async def record_merge(
        self,
        *,
        action: str,
        canonical_kind: str,
        canonical_id: str,
        merged_kind: str,
        merged_id: str,
        created_by: str,
        undo: dict | None = None,
    ) -> int:
        async with self.lock:
            cur = await self.db.execute(
                f"INSERT INTO graph_merges ({_MERGE_COLS}) VALUES "
                "(NULL, ?, ?, ?, ?, ?, ?, ?, ?, NULL)",
                (
                    action, canonical_kind, canonical_id, merged_kind, merged_id,
                    json.dumps(undo or {}), created_by, _now(),
                ),
            )
            await self.db.commit()
        assert cur.lastrowid is not None
        return cur.lastrowid

    async def list_merges(self, *, include_undone: bool = False) -> list[GraphMerge]:
        where = "" if include_undone else " WHERE undone_at IS NULL"
        cur = await self.db.execute(
            f"SELECT {_MERGE_COLS} FROM graph_merges{where} ORDER BY id"
        )
        return [_row_to_merge(r) for r in await cur.fetchall()]

    async def mark_merge_undone(self, merge_id: int) -> bool:
        async with self.lock:
            cur = await self.db.execute(
                "UPDATE graph_merges SET undone_at=? WHERE id=? AND undone_at IS NULL",
                (_now(), merge_id),
            )
            await self.db.commit()
        return cur.rowcount > 0

    async def _live_node_row(self, node_id: int) -> tuple | None:
        cur = await self.db.execute(
            f"SELECT {_NODE_COLS} FROM graph_nodes WHERE id=? AND status='live'", (node_id,)
        )
        return await cur.fetchone()

    async def merge_nodes(
        self, *, canonical_id: int, merged_id: int, created_by: str = "user"
    ) -> int:
        """Fold asserted node ``merged_id`` into ``canonical_id`` (same kind): re-point the merged
        node's asserted edges onto the canonical endpoint, alias its title onto the canonical, and
        RETRACT the merged node (never delete). Returns the ``graph_merges`` journal id;
        :meth:`undo_merge` reverses it exactly. Derived edges are untouched (a rebuild re-derives
        them against surviving rows). Raises ValueError on an invalid pair."""
        if canonical_id == merged_id:
            raise ValueError("cannot merge a node into itself")
        async with self.lock:
            canon = await self._live_node_row(canonical_id)
            merged = await self._live_node_row(merged_id)
            if canon is None or merged is None:
                raise ValueError("both nodes must exist and be live")
            if canon[1] != merged[1]:  # kind must match — merging distinct kinds is nonsensical
                raise ValueError(f"kind mismatch: {canon[1]} != {merged[1]}")
            cep, mep = (canon[1], str(canonical_id)), (merged[1], str(merged_id))
            ops: list[dict] = []
            cur = await self.db.execute(
                f"SELECT {_EDGE_COLS} FROM graph_edges WHERE origin='asserted' AND status='live' "
                "AND ((src_kind=? AND src_id=?) OR (dst_kind=? AND dst_id=?)) ORDER BY id",
                (mep[0], mep[1], mep[0], mep[1]),
            )
            for e in await cur.fetchall():
                eid, (sk, si, dk, di), ekind = e[0], (e[1], e[2], e[3], e[4]), e[5]
                nsk, nsi = cep if (sk, si) == mep else (sk, si)
                ndk, ndi = cep if (dk, di) == mep else (dk, di)
                if (nsk, nsi) == (ndk, ndi):  # self-loop after the fold → retract, no re-point
                    await self.db.execute(
                        "UPDATE graph_edges SET status='retracted' WHERE id=?", (eid,))
                    ops.append({"op": "retract", "id": eid})
                    continue
                dup = await self.db.execute(
                    "SELECT id, status FROM graph_edges WHERE src_kind=? AND src_id=? AND "
                    "dst_kind=? AND dst_id=? AND edge_kind=? AND origin='asserted' AND id!=?",
                    (nsk, nsi, ndk, ndi, ekind, eid),
                )
                other = await dup.fetchone()
                if other is not None:  # re-pointing would collide with the identity index → drop
                    await self.db.execute(
                        "UPDATE graph_edges SET status='retracted' WHERE id=?", (eid,))
                    ops.append({"op": "retract", "id": eid})
                    if other[1] == "retracted":  # revive the survivor; undo re-retracts it
                        await self.db.execute(
                            "UPDATE graph_edges SET status='live' WHERE id=?", (other[0],))
                        ops.append({"op": "revive", "id": other[0]})
                else:
                    await self.db.execute(
                        "UPDATE graph_edges SET src_kind=?, src_id=?, dst_kind=?, dst_id=? "
                        "WHERE id=?", (nsk, nsi, ndk, ndi, eid))
                    ops.append({"op": "repoint", "id": eid, "old": [sk, si, dk, di]})
            alias = None  # keep the merged title discoverable as an alias label on the canonical
            labels = _loads(canon[14], [])
            if merged[2] and merged[2] != canon[2] and merged[2] not in labels:
                await self.db.execute(
                    "UPDATE graph_nodes SET labels_json=?, updated_at=? WHERE id=?",
                    (json.dumps([*labels, merged[2]]), _now(), canonical_id))
                alias = merged[2]
            await self.db.execute(
                "UPDATE graph_nodes SET status='retracted', updated_at=? WHERE id=? "
                "AND status='live'", (_now(), merged_id))
            undo = {"merged_id": merged_id, "alias": alias, "edges": ops}
            j = await self.db.execute(
                f"INSERT INTO graph_merges ({_MERGE_COLS}) VALUES "
                "(NULL, 'merge', ?, ?, ?, ?, ?, ?, ?, NULL)",
                (cep[0], cep[1], mep[0], mep[1], json.dumps(undo), created_by, _now()))
            await self.db.commit()
        assert j.lastrowid is not None
        return j.lastrowid

    async def undo_merge(self, merge_id: int) -> bool:
        """Reverse a journaled merge exactly: restore re-pointed/retracted edges, drop the alias,
        bring the merged node back live, mark the journal row undone. Idempotent (a second call is
        a no-op — returns False)."""
        async with self.lock:
            cur = await self.db.execute(
                "SELECT canonical_id, undo_json FROM graph_merges "
                "WHERE id=? AND undone_at IS NULL", (merge_id,))
            row = await cur.fetchone()
            if row is None:
                return False
            canonical_id, undo = int(row[0]), _loads(row[1], {})
            for op in reversed(undo.get("edges", [])):
                if op["op"] == "repoint":
                    sk, si, dk, di = op["old"]
                    await self.db.execute(
                        "UPDATE graph_edges SET src_kind=?, src_id=?, dst_kind=?, dst_id=? "
                        "WHERE id=?", (sk, si, dk, di, op["id"]))
                elif op["op"] == "retract":
                    await self.db.execute(
                        "UPDATE graph_edges SET status='live' WHERE id=?", (op["id"],))
                elif op["op"] == "revive":
                    await self.db.execute(
                        "UPDATE graph_edges SET status='retracted' WHERE id=?", (op["id"],))
            alias = undo.get("alias")
            if alias:
                lc = await self.db.execute(
                    "SELECT labels_json FROM graph_nodes WHERE id=?", (canonical_id,))
                lrow = await lc.fetchone()
                if lrow is not None:
                    kept = [x for x in _loads(lrow[0], []) if x != alias]
                    await self.db.execute(
                        "UPDATE graph_nodes SET labels_json=?, updated_at=? WHERE id=?",
                        (json.dumps(kept), _now(), canonical_id))
            await self.db.execute(
                "UPDATE graph_nodes SET status='live', updated_at=? WHERE id=?",
                (_now(), int(undo["merged_id"])))
            await self.db.execute(
                "UPDATE graph_merges SET undone_at=? WHERE id=?", (_now(), merge_id))
            await self.db.commit()
        return True


def _row_to_node(r: tuple) -> GraphNode:
    return GraphNode(
        id=r[0], kind=r[1], title=r[2], summary=r[3], embedding=_from_blob(r[4]),
        embedding_model=r[5], content_hash=r[6], project_id=r[7], trust_class=r[8],
        sensitivity=r[9], source_kind=r[10], created_by=r[11], model=r[12], status=r[13],
        labels=_loads(r[14], []), created_at=r[15], updated_at=r[16],
    )


def _row_to_edge(r: tuple) -> GraphEdge:
    return GraphEdge(
        id=r[0], src_kind=r[1], src_id=r[2], dst_kind=r[3], dst_id=r[4], edge_kind=r[5],
        origin=r[6], project_id=r[7], trust_class=r[8], sensitivity=r[9], created_by=r[10],
        model=r[11], team=r[12], evidence=_loads(r[13], []), status=r[14], created_at=r[15],
    )


def _row_to_suggestion(r: tuple) -> GraphSuggestion:
    return GraphSuggestion(
        id=r[0], kind=r[1], payload=_loads(r[2], {}), evidence=_loads(r[3], []), project_id=r[4],
        trust_class=r[5], sensitivity=r[6], extractor_model=r[7], est_cost_usd=r[8], status=r[9],
        created_at=r[10], resolved_at=r[11], resolved_by=r[12],
    )


def _row_to_merge(r: tuple) -> GraphMerge:
    return GraphMerge(
        id=r[0], action=r[1], canonical_kind=r[2], canonical_id=r[3], merged_kind=r[4],
        merged_id=r[5], undo=_loads(r[6], {}), created_by=r[7], created_at=r[8], undone_at=r[9],
    )
