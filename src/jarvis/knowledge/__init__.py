"""Research + Markdown knowledge base (Phase 4): the "LLM Wiki".

Jarvis ingests sources (files, webpages, notes) into immutable raw artifacts +
deterministic Markdown, indexes them for retrieval, and maintains an
Obsidian-compatible wiki of durable pages with provenance and citations.

Layout (built up over Milestone 4):

* ``store.py``          — KnowledgeStore: SQLite persistence for sources, the chunk
                          index, and the wiki-link index (schema v4)
* ``chunking.py``       — pure, heading-aware Markdown chunking
* ``converters.py``     — the ONLY third-party-converter import site (markitdown,
                          optional docling, trafilatura for web, passthrough) +
                          sanitization, byte caps, and URL/path safety
* ``convert_worker.py`` — subprocess entry point: a killable conversion sandbox
* ``links.py``          — pure Markdown/``[[wikilink]]`` extraction + resolution
* ``service.py``        — KnowledgeService: ingest / query / lint / rebuild / review

Two safety properties dominate the design and are enforced structurally, not by
prompt framing (see docs/PLAN-4-knowledge.md and docs/decisions/0004-*):

1. Conversion is gated I/O — a converter opening an attacker-supplied file is a
   read the gate must see, run under a killable subprocess with input and
   decompressed-size caps.
2. The knowledge base is a contained injection sink — provenance is derived from
   the database, never from converted content; unattended ingests are quarantined
   until a human reviews them.
"""
