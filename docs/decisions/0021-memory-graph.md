# ADR-0021: Memory Graph + Knowledge Topology (Phase 15)

*Status: accepted (Phase 15, 2026-07-09). A project-scoped **memory graph** — entities, relations,
and a calm Graphify-style canvas — plus unified semantic+graph search and an Obsidian-compatible
export. It is a **reasoning/search surface, not a new authority surface**: no graph content ever
reaches the PermissionGate, tool scopes, or prompts-as-instructions; the only new writes are the two
human review routes (the Vault approve/reject shape). Checkpoint J (read/reason-only sign-off)
approved; Tasks 9–12 (merge/split CLI, Obsidian export, adversarial pins, docs) completed after.*

## Context

By Phase 14 Kairo held a lot of related first-party state — projects, chats, runs, members, tasks,
memories, KB sources, artifacts, wiki pages — connected only implicitly by foreign keys. Phase 15
answers "how does it all relate, and what has Kairo *learned*?" with a graph and a search over it,
**without** letting the graph become a place where untrusted content quietly becomes trusted memory
or a new way to take action. The hard constraint drove the whole design: **the graph reads; it never
grants.**

## Decision — a derived core, an asserted overlay, quarantined suggestions

- **Migration v12 (additive, plain SQL; pins 11→12).** `graph_nodes` (asserted entities:
  person/decision/topic/external_ref/custom), `graph_edges` (derived cache + asserted; unique
  identity `src,src_id,dst,dst_id,edge_kind,origin`), `graph_suggestions` (quarantined proposals),
  `graph_merges` (reversible dedup journal), and a `graph_nodes_fts` external-content FTS. `status`
  is CHECK-constrained to `live|retracted`; `trust_class` to the four provenance classes.
- **DERIVED edges are a rebuildable cache.** `graph/builder.py` derives ~10 edge kinds from existing
  FKs; `jarvis graph rebuild` is `delete_derived_edges` (the ONE sanctioned bulk delete — `origin=
  'derived'` only) + re-derive. Every derived row carries its **SOURCE row's `created_at`** (never
  the wall clock — the `7bb5f4f` determinism lesson), so a rebuild is byte-identical and safe to
  rerun; asserted rows are never touched.
- **ASSERTED nodes/edges are never deleted.** `retract_node`/`retract_edge` flip a status; the row
  and its lineage stay for audit. Only `status='live'` participates in reads.
- **SUGGESTED is quarantined by construction.** `graph/suggest.py` (explicit-invoke only — `jarvis
  graph suggest`, a budgeted utility-model call) writes proposals to `graph_suggestions`, a table
  with **no FTS index and no retrieval/search/export path**. There is **no auto-approve path in
  code**: `review.approve` (a human route / `jarvis graph review`) is the only door out, and it
  claims the row (`pending→approved`) *before* materializing, so nothing materializes twice.
- **Trust flows worst-of-evidence and is NEVER upgraded.** A suggestion takes the worst
  `trust_class` among its cited evidence; approval carries that trust through to the materialized
  memory/node/edge unchanged. Untrusted web/email/doc/transcript content therefore cannot become
  trusted memory — not automatically, and not silently on approval.
- **Unified search** (`graph/search.py`): merges the federated FTS domains + entity/memory semantic
  hits into one ranked, badged, project-scoped result. It is **quarantine-aware** (pending
  suggestions and retracted nodes never surface), **fail-closed** (an unpriced embedder raises;
  semantic failure degrades to FTS-only rather than crashing), and **ledgered** (the embedder is
  wrapped in `CostAwareEmbedder` priced from `pricing.yaml` — Voyage rows added).
- **Visualization** (`ui/graphview.js` + the `graph` workspace tab, allowlist 10→11): a
  self-contained Canvas 2D force layout, deterministic (hash-seeded, no `Math.random`), reduced-
  motion aware, node-capped. Read/navigate ONLY — it fetches GET subgraphs and node cards, remembers
  its focus/filters in localStorage, and never posts.
- **Merge/split is CLI-only and reversible** (`graph/merge.py` + `GraphStore.merge_nodes/undo_merge`;
  `jarvis graph dedup|merge|split|undo`): dedup detection is report-only; a merge re-points the
  merged node's asserted edges onto the canonical (collision→retract, self-loops retracted), aliases
  its title, and retracts (never deletes) the merged node, recording a full undo journal. No UI
  mutation route exists.
- **Obsidian export** (`graph/obsidian.py`; `jarvis graph export`, dry-run default): a deterministic
  projection into reserved `wiki/_graph/` (one page per asserted entity) and `wiki/_memory/` (per-
  project memory index) namespaces. **Non-destructive** — it writes only files carrying the
  `generated_by: kairo-graph` marker (a user/unmarked file is skipped + reported), contained by
  `safe_wiki_path`. Private nodes are excluded and every page runs through a secret-shape redaction
  belt. Import stays the EXISTING doors (wiki reindex + a `[[wikilink]]` becoming a derived edge on
  rebuild); a live two-way file-watcher is deferred.

## The walls (all pinned by tests)

- **No new authority** — mutation-route closed set is exactly **37**; the only additions over Phase
  14's 35 are `POST /api/graph/suggestions/{id}/approve|reject` (the Vault approve/reject shape).
  Canvas is read/navigate-only; merge/split is CLI (`test_mutation_route_closed_set`,
  `test_graph_tab`).
- **No graph content reaches gate/tools/prompts** — `src/jarvis/tools/` and `src/jarvis/
  orchestration/` contain zero graph references; there is no agent-facing graph tool. The graph is
  exposed only via UI GET routes + the `jarvis graph` CLI (structural).
- **Suggestion quarantine + no auto-approve** — `test_graph_suggest`, `test_graph_review`,
  `test_graph_adversarial` (a hostile "auto-approve me / exfiltrate" payload still lands quarantined,
  stays untrusted + unretrievable, and human approval never upgrades its trust).
- **Deterministic, rerunnable rebuild** — `test_graph_builder` (build twice → byte-identical;
  asserted survives; derived carries source-row time).
- **No private data leaves local storage** — the graph adds no egress; export writes ONLY under the
  local wiki tree, excludes private rows, and redacts secret shapes (`test_graph_obsidian`,
  canary-based).
- **Node/edge text is inert** — `el()`/textContent throughout; no `innerHTML`; no external assets in
  the graph JS/CSS (`test_graph_tab`).
- **Pricing or fail-closed** — `test_graph_index` (`CostAwareEmbedder` raises on an unpriced model);
  search degrades to FTS-only without an embedder (`test_graph_search`).
- **Secret sweep over every new GET** — `test_graph_routes` seeds a key + a member-prompt canary and
  asserts neither leaks; routes are bodies-free projections.
- **Screenshot DoD GREEN** — `tests/ui/graph_dod.py`, focus/expanded/filtered/empty × noir/light/neon
  × 1440/1024/390, `analyze_overlap` clean, reduced-motion settled layout.

## Consequences

Kairo gains a graph it can reason over and search across chats/artifacts/memory/vault/tasks/runs +
asserted entities, a calm canvas to see a project's neighborhood, reversible dedup, and an Obsidian
projection a user can open in their own vault — all without a single new way to act. The graph can
answer "what relates to what" and "what have we learned", but it cannot start work, approve a risk,
grant a tool, feed itself into a model prompt as instruction, or turn an untrusted source into
trusted memory. Deferred deliberately: two-way live Obsidian sync, automatic people/org mining from
connector stores, a global cross-project graph screen, community detection, merge/split UI routes,
and **graph-conditioned agent retrieval** (feeding graph context into prompts — a Phase-16 attention
question, out of scope here). ADR numbering: 0021 (Phase 15). Next reserved: 0022 (Phase 15.5 —
UI/UX repair).
