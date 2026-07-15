"""Phase 15 — Memory Graph + Knowledge Topology.

A queryable topology over projects, chats, artifacts, vault pages, KB sources, memories, tasks,
orchestration runs, teams, services, decisions, people, and external sources. Three layers:

* **derived** — a rebuildable edge CACHE over existing rows/FKs (``builder.py``); no truth of its
  own, delete+re-derive is safe and deterministic.
* **asserted** — human-approved nodes/edges (``graph_nodes`` / ``origin='asserted'``); never-DELETE
  (retract by status), full provenance.
* **suggested** — quarantined proposals (``graph_suggestions``); invisible to search/retrieval/
  export until a human approves. No auto-approve path exists.

It is a reasoning/search surface, NOT an authority surface: nothing here reaches the PermissionGate,
an approval, a tool scope, or a model prompt-as-instruction.
"""

from __future__ import annotations

from kira.graph.store import (
    GraphEdge,
    GraphMerge,
    GraphNode,
    GraphStore,
    GraphSuggestion,
)

__all__ = ["GraphEdge", "GraphMerge", "GraphNode", "GraphStore", "GraphSuggestion"]
