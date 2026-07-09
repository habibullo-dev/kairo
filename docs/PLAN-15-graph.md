# Jarvis Phase 15 — Memory Graph + Knowledge Topology (Obsidian-compatible)

*(Planned by Fable 2026-07-09; Opus 4.8 implements and commits this doc in Task 1. Baseline:
Phase 14 COMPLETE (AI Team Office, ADR-0020, `ec33b63`); suite 1832 green, core replay gate 19/19
$0, ruff clean, migrations at **v11**, mutation-route pin at **35**. NEVER touch: docs/PLAN.md,
docs/PLAN-7-voice-consent-checkpoint.md, mcp_sample.json, config/settings.yaml,
config/permissions.yaml, design/, .env, data/connectors/ or any token file.)*

## 0. Context — what this phase is (and is NOT)

Phase 15 makes Kairo's memory and knowledge **visibly organized, project-aware, searchable, and
graph-connected**: one queryable topology over projects, chats, artifacts, vault pages, KB sources,
memories, tasks, orchestration runs, teams, services, decisions, people, and external sources —
plus a calm Graphify-style visualization, unified semantic+graph search, a review workflow that
keeps untrusted content from ever becoming durable memory silently, and an Obsidian-compatible file
projection of it all.

It is a **reasoning and search surface, not a new authority surface**. The graph adds ZERO new
write/action paths beyond two review routes that copy the existing Vault-quarantine pattern. Nothing
a node or edge says can reach a tool, an approval, or an egress path. The agent's existing memory
recall and KB retrieval are **unchanged** this phase — the graph augments what the human (and
search) can see, not what the model is fed. Calm by design: focus-first neighborhoods, filters,
saved views, project scopes, progressive disclosure — never a whole-corpus hairball.

## 1. What already exists (grounding — inspected)

- **Federated FTS5** (`persistence/fts.py`): 7 quarantine-aware domains — `chats · memories ·
  knowledge · tasks · orchestration · digests · artifacts` — with project scoping
  (`scope_clause`), used by the Ctrl-K palette (GET-only). `rebuild_all()` exists.
- **KB store** (`knowledge/store.py`): `kb_sources` = primary/audit, **never-DELETE**, with
  `review_status` quarantine (unreviewed sources invisible to search — ADR-0004);
  `kb_chunks` + `kb_wiki_links` = **derived, rebuildable caches** (delete+rebuild is the one
  sanctioned DELETE). Wiki pages are human-first markdown with `[[wikilinks]]` already parsed
  (link_kind `wikilink|markdown`) — the vault is **already Obsidian-openable**.
- **Memory store** (`memory/store.py`): `memories(id, type, content, embedding, embedding_model,
  source, status[live|superseded|forgotten], superseded_by, created/updated/last_accessed,
  access_count, project_id)`. No review state — memory today is written only by Kairo's own
  reflection paths. Embeddings: `VoyageEmbedder` (`voyage-3-large`, key via Secrets/.env,
  `input_type` query/document).
- **Artifacts** (`persistence/artifacts.py`): identity `(origin_type, origin_id)`, carrying
  `project_id, kind, sensitivity, provenance_class, team, role, model, created_by, labels_json` —
  the exact metadata vocabulary the graph needs, already in production.
- **Review-queue precedent**: Vault tab → `POST /api/vault/sources/{id}/approve|reject` (in the
  35-route pin), capped preview so approval is informed.
- **Saved views**: `saved_views` table with `scope` + `POST /api/views/save` /
  `/api/views/{id}/delete` (Phase 11) — graph views can reuse this, zero new routes.
- **Cost machinery**: `pricing.yaml` schema v2 (`models:` + `services:`), fail-closed unpriced
  blocking, `ServiceBudget` per-run/day caps (Phase 13), ledgers. **Gap found: Voyage embeddings
  have NO pricing row today** — KB ingest embeds unmetered. Phase 15 closes this for all NEW
  indexing (and wraps the shared embedder with a ledger without changing KB ingest behavior).
- **Orchestration**: runs (`workflow, team, status, verdict, synthesis_summary, cost`) + member
  runs (role/stage/status/cost, bodies-free read models); teams/services as code constants
  (`teams.py`, `SERVICE_CATALOG`).
- **UI shell**: workspace tab allowlist (10 tabs incl. `office`), per-tab error boundary,
  `el()`/textContent discipline, token CSS, screenshot DoD machinery (`jarvis.ui.screenshots`,
  Phase-14 self-contained `tests/ui/office_dod.py` pattern), mutation pin **35**.
- **Determinism lesson (Phase 14, commit `7bb5f4f`)**: wall-clock leaking into derived
  content breaks replay/rebuild determinism — derived graph rows must carry their SOURCE rows'
  timestamps, never `now()`.

## 2. Architecture — derived core, asserted overlay, quarantined suggestions

The load-bearing decision (mirrors `kb_sources` vs `kb_chunks`):

1. **Derived layer (a rebuildable cache, no truth of its own).** Most nodes/edges already exist as
   rows and foreign keys: chat→project, artifact→(run|digest|wiki) via `origin_type/origin_id`,
   run→project/team, member-run→run/role, wiki page→cited sources (`source_ids` front-matter),
   wiki page→wiki page (`kb_wiki_links`), memory→project/session, task→project,
   source→origin URL, service/team catalog constants. The **graph builder** derives these
   deterministically into a cache (`graph_edges` rows with `origin='derived'`). Rebuild =
   delete-derived + re-derive; **asserted rows are never touched**; running it twice yields
   byte-identical results (stable ordering, source-row timestamps, content-hash identity).
2. **Asserted layer (human-approved, never-DELETE).** New first-class entities that have no
   existing row — `decision`, `person`, `topic`, `external_ref` — and semantic edges
   (`relates_to`, `decision_about`, `person_involved`, `about_topic`) live in `graph_nodes` /
   `graph_edges` with `origin='asserted'`, `status[live|retracted]` (retract, never delete).
   Asserted rows exist ONLY via the review workflow (§6) or explicit human CLI — never
   automatically.
3. **Suggestion layer (quarantined, invisible).** Extractors propose memories/entities/edges into
   `graph_suggestions` with bodies-free **evidence pointers** (kind:id + excerpt offsets), the
   extractor model, and cost. Suggestions are **never searchable, never retrievable, never
   exported, never rendered as graph truth** until a human approves — exactly the ADR-0004
   source-quarantine posture.

Node addressing: a node is `(kind, ref_id)` — derived kinds reference existing rows (`project:3`,
`artifact:41`, `wiki:pages/foo.md`, `memory:17`, `run:9`, `source:12`, `task:5`, `chat:22`,
`team:security`, `service:firecrawl`); asserted kinds reference `graph_nodes.id`
(`person:2`, `decision:4`). No duplication of existing rows into a node table.

**Metadata on every node/edge (non-negotiable #8):** `project_id` (NULL=global), `origin`
(derived|asserted|suggested), `trust_class` (trusted_local | reviewed | untrusted_external |
model_generated — mapped from artifact `provenance_class`, KB `review_status`+`created_by`,
connector taint), `sensitivity` (reused artifact vocabulary), `source_kind`, `created_by`
(user|agent|system), `model` (when model-derived), `team` (when run-derived), `created_at`
(source-row time for derived; review time for asserted). Derived values are **computed from the
underlying row at build time** — the graph can never hold a HIGHER trust than its source.

```
src/jarvis/graph/
├── __init__.py
├── store.py        # GraphStore over graph_nodes/edges/suggestions/merges (asserted never-DELETE)
├── builder.py      # deterministic derive: existing stores -> derived edge cache (+ FTS entities)
├── service.py      # GraphService: neighbors/subgraph/filters read models; merge/split ops
├── suggest.py      # extractors -> graph_suggestions (budgeted utility model; evidence pointers)
├── search.py       # unified semantic + graph search (embed-once cosine + FTS + neighbor expand)
└── obsidian.py     # deterministic file projection + staged import (Task 10; export-first)
```

## 3. Schema — migration v12 (additive, plain SQL)

- **`graph_nodes`** — asserted entities only: `id, kind(person|decision|topic|external_ref|custom),
  title, summary(short, bodies-free), project_id, trust_class, sensitivity, source_kind,
  created_by, model, status(live|retracted), created_at, updated_at, labels_json`.
- **`graph_edges`** — `id, src_kind, src_id, dst_kind, dst_id, edge_kind, origin(derived|asserted),
  project_id, trust_class, sensitivity, created_by, model, team, evidence_json(pointers only),
  status(live|retracted), created_at`. Unique on
  `(src_kind,src_id,dst_kind,dst_id,edge_kind,origin)`. Derived rows: delete+rebuild sanctioned;
  asserted rows: never-DELETE (retract).
- **`graph_suggestions`** — `id, kind(memory|node|edge), payload_json, evidence_json,
  project_id, trust_class, sensitivity, extractor_model, est_cost_usd, status(pending|approved|
  rejected), created_at, resolved_at, resolved_by`. Quarantined: excluded from every FTS trigger,
  read model, export, and retrieval path.
- **`graph_merges`** — journal: `id, canonical_kind, canonical_id, merged_kind, merged_id, action
  (merge|split), undo_json, created_by, created_at, undone_at`. Merge = alias + edge re-point
  recorded reversibly; the merged node is retracted, never deleted.
- **FTS**: one new domain `entities` over `graph_nodes` (title+summary), quarantine/status-aware,
  wired into the existing triggers + `rebuild_all()` + the palette. Version pins 11→12.
- **Embedding cache for entities**: `graph_nodes.embedding/embedding_model` columns (same numpy
  pattern as memories), re-embedded only when `title+summary` content-hash changes (§7).
- **Memory files are a PROJECTION, not storage.** Canonical memory stays SQLite; §10 exports
  deterministic per-project markdown. No schema change to `memories` (suggestions are a separate
  table, so the memory table's semantics are untouched).

## 4. Read models + routes (reads are fine; writes are enumerated)

- `GET /api/workspace/{project_id}/graph` — the project-scoped subgraph projection: nodes/edges
  with full metadata, plus `counts` by kind/trust for the filter bar. Query params (validated,
  clamped): `focus=(kind:id)`, `depth<=2`, `kinds=…`, `trust=…`, `since=…`, `limit<=300`.
- `GET /api/graph/node/{kind}/{id}` — one node's card: metadata, neighbors (capped), provenance
  chain, "appears in" (runs/chats/pages). Bodies-free; parameterized-GET secret sweep required.
- `GET /api/graph/search?q=…&project_id=…` — unified search (§7): merged FTS + semantic + entity
  hits with kind/trust badges and graph-neighbor context. GET-only; palette consumes it.
- `GET /api/graph/suggestions?project_id=…` — the review queue (pending only, evidence previews
  capped like the Vault queue).
- **NEW mutations (pin 35→37, the ONLY additions):**
  `POST /api/graph/suggestions/{id}/approve` and `POST /api/graph/suggestions/{id}/reject` —
  byte-for-byte the Vault approve/reject pattern (session-auth, project-checked, idempotent,
  journaled). Approve materializes: memory-suggestion → a real `memories` row (source =
  `reviewed_suggestion`); node/edge-suggestion → asserted `graph_nodes`/`graph_edges`.
  Merge/split apply is **CLI-first this phase** (`jarvis graph merge/split/undo`) — no route, so
  the graph UI stays read+review-only. Saved graph views reuse the EXISTING `/api/views/save`
  (new `scope="graph"` value; subset-validated) — no new route.

## 5. Suggestion pipeline (how entities/memories are proposed — never trusted)

`graph/suggest.py` runs **only when explicitly invoked** (CLI `jarvis graph suggest`, or the
existing reflection hook extended — NOT a new scheduler job this phase):

- Extractors scan **bounded, already-local** material: chat session summaries, run
  synthesis_summaries, digest items, wiki pages — via the `utility` route (anthropic-only,
  ledgered, budgeted with a hard per-invocation cap; unpriced ⇒ refuse).
- Each proposal carries evidence pointers (never content copies), the extractor model, trust_class
  computed from the WORST source in its evidence (one untrusted evidence item ⇒
  `untrusted_external` ⇒ the approve UI shows the warning frame), and sensitivity inherited the
  same way.
- **Hard invariants (pinned):** a suggestion whose evidence includes web/email/docs/transcript
  content can NEVER auto-approve; there is no auto-approve path at all (no config flag exists to
  create one); suggestions are invisible to FTS/retrieval/export; approving is the only door, and
  it requires an authenticated human session.
- People nodes: extracted only from material the human already sees (chat/digest text), NEVER
  mined from raw connector stores; automatic connector-derived people graphs are **deferred**.

## 6. Review workflow (UI + CLI)

- **Memory tab** (existing workspace tab) gains a "Suggested" section — the review queue with the
  Vault tab's exact interaction: evidence preview (capped, textContent), trust/sensitivity badges,
  Approve / Reject buttons → the two new routes. An `untrusted_external` suggestion renders inside
  the standard warning frame so the human sees WHY it needs scrutiny.
- Approved memory suggestions become normal `memories` rows (retrievable next session); approved
  entities/edges appear in the graph as `asserted · reviewed`.
- CLI parity: `jarvis graph review` (list/approve/reject) mirrors `kb review`.
- Edit-before-approve: v1 allows title/content trimming on approve (validated length caps);
  anything more is reject-and-recreate. (Keeps the route payload narrow.)

## 7. Unified semantic + graph search, cost-aware indexing

- **Query path:** embed the query ONCE (Voyage, `input_type="query"`) → cosine over
  memories + kb_chunks + entity embeddings (existing numpy pattern) → merge with FTS hits from
  the 7+1 domains → optional 1-hop graph expansion of the top hits ("connected: …") → ranked,
  kind/trust-badged results. Quarantine-aware at every layer (unreviewed sources, suggestions,
  retracted rows never surface).
- **Cost control (non-negotiable #10):**
  - `pricing.yaml` gains Voyage rows under `models:` (per-1M-token embed rates, `effective`
    dated). The **graph indexer + unified search fail closed** without a row.
  - A ledger wrapper around the shared embedder records embedding calls (model, tokens, cost) —
    NEW paths (entity indexing, unified search) always ledgered+capped (`ServiceBudget`-style
    per-run/day); the EXISTING KB-ingest call sites keep working unchanged but now produce ledger
    rows too (observability only — no new blocking on the legacy path this phase, explicitly
    noted in the ADR as a ratchet candidate).
  - Re-embedding is content-hash keyed: unchanged text is never re-embedded; `jarvis graph
    reindex` reports skipped/embedded/spend before running (plan → confirm for large batches).
- Eval determinism: eval runs replay embeddings through the existing cassette embedder — no live
  Voyage in the gate.

## 8. Visualization — calm Graphify-style canvas (no deps, no CDN)

`ui/static/ui/graphview.js` — a self-contained vanilla ES module (Canvas 2D force layout, ~2–300
lines, CSP-clean, zero external assets):

- **Focus-first, never a hairball:** the default render is a FOCUS NODE + depth-1 neighborhood
  (the project node when opened from a workspace). Expand = click a frontier node (progressive
  disclosure). Hard visible-node cap (~150) with a "+N more" affordance; type-clustered layout
  seeds; deterministic seed positions (hash of ref) so the same view lays out the same way.
- **Encoding:** node color by KIND (token palette), ring style by TRUST (solid=trusted_local /
  reviewed, dashed=model_generated, hazard-dashed=untrusted_external — plus a text badge, never
  color-only), size by degree (capped). Edges: derived=thin, asserted=solid+labeled on hover.
- **Filter bar:** kind chips, trust chips, time window, project scope (default: current project;
  "include global" toggle) — all reflected in the querystring so a view is deep-linkable.
- **Saved views:** name + filters + focus persisted via the EXISTING saved-views routes
  (`scope="graph"`); the localStorage-first pattern for last-used filters.
- **Interactions are read/navigate-only:** click → the node card panel (metadata, provenance,
  "open in" links to Chats/Artifacts/Vault/Studio/Office/Trace). NO create/edit/delete on canvas.
- **Motion:** the force simulation settles and STOPS (energy threshold); `prefers-reduced-motion`
  / `.reduce-motion` ⇒ render the settled layout with no animation. Hover/pan/zoom stay cheap
  (rAF-coalesced, like the Office).
- **Surfaces:** new workspace tab `graph` (allowlist 10→11, `#workspace/{id}/graph`); Vault page
  view gains a "Connections" strip (backlinks + cited-by from `kb_wiki_links`/edges — list first,
  mini-canvas optional); the Office inspect drawer gains a "Graph" navigate link; the palette
  shows entity hits (GET-only). A global `#graph` screen is DEFERRED — project scope is the calm
  default.

## 9. Safety model (non-negotiables → enforcement)

1. **No untrusted source becomes trusted memory automatically** — suggestions table + no
   auto-approve path exists; approve requires an authed human; trust_class = worst evidence.
   *Pinned:* `test_graph_suggestions_quarantine`, eval `memory_suggestion_quarantine`.
2. **Web/email/docs/transcripts stay untrusted until reviewed** — trust_class derivation maps
   connector/web provenance to `untrusted_external`; the review UI frames it.
   *Pinned:* `test_graph_trust_derivation`.
3. **Graph = reasoning/search surface, not authority** — graph content never reaches
   PermissionGate, approvals, tool scopes, or prompts-as-instructions; node/edge text renders
   textContent-only. *Pinned:* `test_graph_no_authority` (structural: graph modules import no
   gate/executor), `test_graph_text_safety`.
4. **No new write/action authority through graph UI** — mutation pin 35→**37** exactly (the two
   review routes); canvas is read/navigate-only; merge/split is CLI.
   *Pinned:* `test_mutation_route_closed_set` update + `test_graph_ui_readonly`.
5. **Reads fine; writes via existing patterns** — approve/reject copies the Vault route shape;
   views reuse `/api/views`. *Pinned:* route tests + secret sweep on every new GET.
6. **No private data leaves local storage** — the graph adds no egress; Obsidian export writes
   ONLY under the local wiki tree; export excludes `sensitivity=private` rows and all connector
   bodies. *Pinned:* `test_obsidian_export_no_private_no_secrets` (canary-based).
7. **Obsidian sync leaks no secrets, never destructive** — §10 guards.
8. **Deterministic, rerunnable rebuild** — derived-only delete+rebuild; source-row timestamps
   (never wall-clock — the `7bb5f4f` lesson); stable ordering. *Pinned:*
   `test_graph_rebuild_deterministic` (build twice, byte-identical dumps; asserted rows survive).
9. **Keys from .env only** — Voyage/Anthropic via existing Secrets loading; no new key handling.
10. **Pricing or fail-closed** — voyage rows added; indexer/search refuse unpriced.
    *Pinned:* `test_graph_indexing_fail_closed_unpriced`.

## 10. Obsidian bridge (export-first; import is staged; two-way live sync DEFERRED)

- **The vault is already the wiki tree** — a user can open `knowledge/wiki/` in Obsidian today.
  Phase 15 adds a deterministic **projection** of graph + memory into a reserved namespace:
  `wiki/_graph/` (entities: one page per asserted node, front-matter = metadata, body = summary +
  `[[wikilinks]]` to related pages) and `wiki/_memory/{project-slug}.md` (per-project memory
  file: approved memories as list items with provenance + dates). Deterministic: same DB ⇒
  byte-identical files (stable ordering, no wall-clock).
- **Non-destructive by construction:** the exporter writes ONLY inside the reserved namespaces,
  ONLY files carrying the `generated_by: kairo-graph` front-matter marker (a user-edited or
  unmarked file at a target path ⇒ skip + report, never overwrite), via `safe_wiki_path`
  containment. User notes elsewhere are never touched. `.obsidian/` config is left alone.
- **No secrets:** export sources are already bodies-free/reviewed surfaces; additionally a
  redaction pass refuses to write any line matching the existing secret-shape patterns, and the
  sensitive-path floor (`paths.py`) is excluded from any read. Canary-pinned.
- **Import = the EXISTING doors:** a page the user writes/edits in the vault is already picked up
  by wiki reindex (human-first, trusted); a `[[wikilink]]` in a user page to a `_graph/` entity
  becomes a derived edge on rebuild (delightful, free). Bulk-importing an EXTERNAL Obsidian vault
  routes through the existing `kb ingest` + review quarantine — no new trust door. A live
  file-watcher/two-way merge engine is **deferred**.
- CLI: `jarvis graph export [--project]`, dry-run by default with a diff summary; `--write` to
  apply.

## 11. Tests / evals (beyond per-task units)

- **Keyless suite:** store invariants (asserted never-DELETE, retract-only; derived rebuild
  determinism incl. asserted-survival; merge journal reversibility), trust/sensitivity derivation
  matrix, suggestion quarantine (invisible to FTS/retrieval/export until approved; approve
  materializes exactly once; reject is terminal), route pins (37 exact; secret sweep over every
  new GET incl. parameterized), UI structural pins (graph tab allowlist; graphview.js — no
  innerHTML, no external assets, no api.post except none; read-only canvas), unified-search
  quarantine + fail-closed-unpriced, Obsidian export (determinism, marker-guard, namespace
  containment, private/secret canaries never exported), FTS `entities` domain + rebuild.
- **Screenshot DoD:** extend the Phase-14 self-contained pattern (`tests/ui/graph_dod.py`):
  states `focus-project · expanded · filtered · empty` × noir/light/neon × 1440/1024/390,
  `analyze_overlap` green; reduced-motion renders the settled layout.
- **Evals (recorded to core at implementation time, then replayed $0):**
  `memory_suggestion_quarantine` — untrusted web content proposes a memory; assert it is NOT
  retrievable in a following session until approved (and IS after);
  `graph_search_grounded` — a question answered via unified search with correct citation.
  Both recorded with the frozen-clock harness (the `7bb5f4f` fix). The pre-existing adversarial
  cassette gap is untouched (core stays the per-task gate); one new adversarial SCENARIO
  (`inj_graph_suggestion_poison` — evidence text instructs auto-approval/exfiltration; assert
  quarantine + no egress) ships keyless-pinned and live-run in Task 12, not added to the replay
  gate.

## 12. Milestones + tasks (per-task commits; suite + ruff + core gate green each task)

**M0 — substrate**
1. **Plan doc + migration v12 + GraphStore.** Commit this doc; `graph_nodes/edges/suggestions/
   merges` + FTS `entities` domain + version pins 11→12; store with never-DELETE/retract
   invariants.
2. **Deterministic builder + rebuild CLI.** Derive the full edge set from existing stores;
   `jarvis graph rebuild`; determinism + asserted-survival pins.

**M1 — read models + review**
3. **GraphService read models + routes (GET).** Subgraph/node-card/counts; scoping, clamps,
   secret sweeps.
4. **Suggestion pipeline** (`graph/suggest.py`, explicit-invoke only; budgeted utility route;
   worst-evidence trust; quarantine invariants).
5. **Review workflow.** Queue read model + the two approve/reject routes (**pin 35→37**) +
   Memory-tab "Suggested" section + `jarvis graph review` CLI.

**M2 — search + indexing**
6. **Unified semantic+graph search + palette integration.** Voyage pricing rows; ledgered,
   capped, fail-closed indexer; content-hash re-embedding; `jarvis graph reindex`.

**M3 — visualization**
7. **graphview.js canvas module + workspace `graph` tab** (allowlist 10→11). Focus+expand, caps,
   filters, deterministic layout, reduced-motion, node card panel.
8. **Surfaces + saved views.** Vault "Connections" strip; Office inspect "Graph" link; palette
   entity hits; saved graph views via existing `/api/views` (`scope="graph"`); deep links.
   Screenshot DoD (`tests/ui/graph_dod.py`) green.

   **⛔ CHECKPOINT J — MANDATORY full stop (read/reason-only sign-off).** Evidence, each with its
   named test: (i) mutation pin exactly 37 and both new routes are the Vault-pattern review ops;
   (ii) graph UI is read/navigate-only (no canvas mutation, no new action path); (iii) suggestion
   quarantine — untrusted content cannot become memory/edge without an authed human approve (incl.
   the adversarial pin); (iv) trust/sensitivity metadata on every node/edge, derived from source,
   never upgraded; (v) rebuild deterministic + safe to rerun (asserted survives; byte-identical);
   (vi) unified search quarantine-aware + fail-closed unpriced + ledgered; (vii) no graph content
   reaches gate/tools/prompts-as-authority; (viii) secret sweep green over every new GET;
   (ix) screenshot DoD green; (x) suite + ruff + core replay gate green. STOP; Tasks 9–12 (merge
   tools + Obsidian WRITES + live ritual) proceed only on Habib's approval.

**M4 — merge + Obsidian (post-checkpoint)**
9. **Dedup + merge/split (CLI-first).** Candidate detection (exact-key + embedding similarity,
   report-only); `jarvis graph merge/split/undo` — journaled, reversible; no UI mutation.
10. **Obsidian export + staged import.** `graph/obsidian.py` per §10; dry-run default;
    marker-guard + namespace containment + secret/private canary pins.
11. **Adversarial pins + evals.** Record the two core scenarios (frozen clock); keyless
    adversarial pins; `inj_graph_suggestion_poison` scenario authored (live-run in Task 12).
12. **Docs + live verification.** ADR-0021 (memory graph — derived/asserted/suggested,
    review-only writes), `docs/verification-15.md`, README Status; live ritual: rebuild on the
    real DB (timed, deterministic re-run), one real suggest→review→approve cycle, unified search
    spot checks, graph tab on real data (calm at scale), Obsidian export dry-run→write→open in
    Obsidian, adversarial suggestion live proof, embedding spend visible in the ledger under cap.

## 13. Now vs deferred (explicit)

**Now:** everything in §12. **Deferred:** two-way live Obsidian sync (file watcher / conflict
merge); automatic people/org mining from connector stores; global cross-project graph screen;
community detection / auto-clustering; graph-conditioned agent retrieval (feeding graph context
into model prompts — a Phase-16 attention question, deliberately out of scope); merge/split UI
routes; entity dedup auto-apply (report-only now); `.obsidian/` config generation; Graphify
plugin parity beyond the calm canvas.

## 14. Opus 4.8 handoff

Execute Tasks 1–12 in order; **MANDATORY full stop at ⛔ Checkpoint J** (after Task 8) with the
ten-bullet evidence — Tasks 9–12 only on approval. Per-task commits with EXPLICIT paths (never
`git add -A`) ending `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`;
adversarial self-review before each commit; suite + ruff + `uv run jarvis eval gate --suite core`
(keyless replay) green every task; never commit red; never commit the NEVER-touch list. Reuse,
never fork: the kb_sources/kb_chunks primary-vs-derived pattern, ADR-0004 quarantine + the Vault
approve/reject route shape, `persistence/fts.py` domains, the saved-views routes, the artifact
metadata vocabulary, `VoyageEmbedder` + cassette embedder (evals stay keyless), `ServiceBudget` +
pricing fail-closed, `el()`/textContent + token CSS + the workspace tab allowlist + the Phase-14
self-contained screenshot-DoD harness pattern, hardened CLI ritual style for graph
rebuild/merge/export. Derived rows carry SOURCE timestamps (never wall-clock — the `7bb5f4f`
lesson). Mutation pin moves 35→37 exactly once (Task 5); tab allowlist 10→11 exactly once
(Task 7). Do NOT drift into Phase 16 (attention/dreaming) or graph-fed model prompts.
ADR-0021 reserved for this phase.
