"""Long-term memory (Phase 2).

Three tiers around the agent loop:

* **Working memory** — the message list, compacted by ``core/context.py`` when it
  nears the token budget.
* **Long-term memory** — this package: an embeddings-indexed store
  (:mod:`~kira.memory.store`) with a semantics layer
  (:mod:`~kira.memory.service`) exposing remember / recall / auto-recall.
* **Episodic memory** — transcripts persist in ``persistence``; an end-of-session
  reflection step (:mod:`~kira.memory.reflection`) distills durable facts here.

Public symbols are re-exported as each task lands.
"""

from __future__ import annotations

from kira.memory.embeddings import Embedder, FakeEmbedder, VoyageEmbedder
from kira.memory.reflection import reflect
from kira.memory.service import MemoryService, RememberResult
from kira.memory.store import Memory, MemoryStore, Provenance, ScoredMemory

__all__ = [
    "Embedder",
    "FakeEmbedder",
    "Memory",
    "MemoryService",
    "MemoryStore",
    "Provenance",
    "RememberResult",
    "ScoredMemory",
    "VoyageEmbedder",
    "reflect",
]
