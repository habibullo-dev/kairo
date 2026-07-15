# Kairo roadmap — Phases 11–17 (post-10B)

*(Proposed 2026-07-08; **APPROVED 2026-07-08 with amendments R1–R7**, incorporated below.
Baseline: Phase 10B complete through Task 19 —
1260 passed / 1 skipped, ruff clean, migrations v8; 10B live verification + eval ratchet
still pending on the user's machine. Product direction: the backend is strong; the next
arc makes Kairo useful and pleasant — findability, artifacts, project UX, then actions,
then presence/automation. Local-first and safety-first are unchanged; no phase weakens
PermissionGate, taint/egress, project boundaries, voice approval, cost ledger, service
catalog, or eval gates.)*

## 0. Gate before anything

Run the pending 10B live checklist (`docs/verification-10B.md`) + the chunked eval gate,
and ratchet the two new adversarial baselines on a green run. Standing discipline: no new
phase starts on an unrun or red gate. **(R7: explicitly required before Phase 11 Task 1.)**

## Approval amendments (2026-07-08, binding)

- **R1 — Saved Views / Smart Collections in Phase 11**: built-ins ("Recent artifacts",
  "Needs review", "Generated this week", "By team/model", "Pinned project work") plus
  user-defined filters saved per project.
- **R2 — Artifact record schema**: every artifact carries `content_hash`, `origin_type` +
  `origin_id`, `created_by` (user | agent | system), producing team/role/model,
  sensitivity/provenance class, and `external_uri` vs `local_path` as clearly separated
  fields; registration dedupes by hash.
- **R3 — Permission-aware command palette**: read/jump actions run immediately;
  write/generate actions route through the existing Gate/turn flow; Ctrl+K grants **no new
  authority** (pinned).
- **R4 — UX definition of done includes screenshots**: empty state, populated state,
  mobile/narrow, project page, search results, artifact preview — captured via
  playwright_local where possible (dogfooding; available once R7's live enablement is done).
- **R5 — Phase 12 attendee/contact ambiguity handling**: an ambiguous person/email must be
  resolved by asking the user BEFORE a calendar invite or draft is created; calendar
  previews must show timezone, attendees, recurrence, Meet link, and the
  notification/`sendUpdates` behavior.
- **R6 — AI Team Office stays an optional visual layer**, never the default UI; the calm
  Studio remains the default. my-virtual-office (AGPL-3.0) is UX reference only — no code
  or assets copied.
- **R7 — 10B live/local verification + eval ratchet completes before Phase 11 Task 1.**

## 1. Recommended ordering

| # | Phase | Effort | Fable boundary | One-line rationale |
|---|---|---|---|---|
| 11 | **Workstation Foundation** — Search, Artifacts & Project UX | Large | Yes + checkpoint (scoping design) | The shelves before more stuff; every later phase renders into these surfaces; fully keyless. |
| 12 | **Action Connectors** (fulfills the 9B pin) — gated Calendar/Drive/Meet writes | Large | Yes + mandatory checkpoint (first outward writes) | Long-promised; builds the preview/diff/approve UX that 13 and 16 reuse. |
| 13 | **Research Services Live** + Settings maturity | Medium | Yes + checkpoint (first external egress enabled) | Cheap (catalog machinery exists); makes teams genuinely powerful; proves B1/B2 against real hostile content. |
| 14 | **AI Team Office** — visual orchestration layer | Medium-Large | Yes (UX design + render-only pins; no adapter checkpoint) | The flagship "alive, premium" moment; by now runs have real variety to show. |
| 15 | **Memory Graph & Obsidian projection** | Medium-Large | Yes | Corpus is big enough to structure; feeds Overview pages and gives 16 substrate. SQLite canonical, Obsidian is projection. |
| 16 | **Attention & Automation** — Notification Center + Dreaming | Large | Yes + mandatory checkpoint (unattended LLM work) | Merged deliberately: proposals need the queue; the queue needs producers. Proposal-first, never silent writes. |
| 17 | **Packaging, Daemon & Multi-Device** — MacBook migration, backup/restore, health | Medium-Large | Yes (data-loss-class risk) | Hardens everything; lands when the MacBook move is real. |

**Deferred track (not phases):** Life OS adapters (Paperless-ngx, Actual, Mealie,
Linkwarden/Karakeep, n8n; SearXNG can ride 13) — each a SERVICE_CATALOG row + small safe
adapter once the 13 pattern is proven. MCP client layer (GitHub/Docker/Supabase/Figma) is a
deliberate architecture decision to make around 13–15, never a default. Semantic/embedding
search after 15. CodeQL/Promptfoo when Security/QA need depth. Gmail send: out indefinitely.

**Ordering tradeoffs:** 12 ↔ 13 are swappable — 13 is smaller and gives faster wins; 12 first
is recommended because the 9B promise is standing and its approval-preview infrastructure
compounds into everything later. 14/15/16 are permutable — if daily-driver utility beats
visual delight, pull 16 ahead of 14; the default order rides 13's newly-live services into
the office view while data phases (15) and autonomy (16) mature behind it. Autonomy (16)
deliberately comes late: it should land on mature surfaces, not create them.

---

## Phase 11 — Workstation Foundation: Search, Artifacts & Project UX

- **Goal:** everything Kairo has ever produced or ingested is findable in seconds and
  organized by project; the workstation feels premium and never empty.
- **Why now:** the backend outpaces the surface — chats, digests, runs, eval reports, and
  meeting notes exist but are invisible. Adoption risk is the top product risk. Also every
  later phase (12's write journal, 13's research outputs, 14's activity, 15's exports, 16's
  briefings) lands into these surfaces. Fully keyless → cheap to build with full test rigor.
- **Dependencies:** none new. Migration v9. FTS5 confirmed present (SQLite 3.53.1).
- **Major features:**
  - `search/` service: SQLite FTS5 external-content tables + triggers over chats/messages,
    project chats, memories, KB/vault docs, tasks, orchestration run summaries, digests,
    meeting notes, artifact metadata. unicode61 tokenizer, `snippet()` highlights, rebuild
    command. Same single connection + write lock (triggers ride existing writes).
  - Scoping model: every indexed row carries project_id + a provenance/sensitivity class;
    scoping enforced in SQL at query time; global search = union of visible projects;
    private-source-derived rows filtered by class. Search API is a GET read model returning
    snippets + pointers, never full bodies.
  - Filters: project, label, date, type, model/team, status; project-scoped search on every
    project page; global search + command palette (Ctrl+K) with keyboard-first navigation.
  - **Artifacts Library:** `artifacts` table (project_id, kind: report / patch / wiki /
    screenshot / design / eval_report / orchestration_summary / export; labels; pinned;
    and per R2: `content_hash`, `origin_type` + `origin_id`, `created_by`
    (user | agent | system), producing team/role/model, sensitivity/provenance class,
    `external_uri` vs `local_path` as separate fields, dedupe-by-hash on registration).
    ArtifactStore registration API adopted by digest/orchestration/eval/wiki writers.
    Floor: registration refuses sensitive paths and any `local_path` outside
    `data/artifacts/<project>/`.
  - **Saved Views / Smart Collections (R1):** built-ins — "Recent artifacts",
    "Needs review", "Generated this week", "By team/model", "Pinned project work" — plus
    user-defined filters saved per project (stored per project, GET read models).
  - Project organization: labels/categories (Coding, Creativity, Business, Personal,
    Learning, Finance — user-editable), pinned/favorites, archived, project groups.
  - Richer project pages: tabs Overview / Chats / Artifacts / Memory / Tasks / Vault /
    Studio / Costs / **Activity** (unified event-feed read model — also the substrate the
    Phase 14 office replays).
  - Command palette is permission-aware (R3): read/jump immediate; write/generate actions
    route through the existing Gate/turn flow; no new authority from Ctrl+K.
  - UX maturity pass A: empty states that teach, hierarchy/typography, calm density.
    Definition of done includes screenshots (R4): empty state, populated state,
    mobile/narrow, project page, search results, artifact preview — via playwright_local
    where possible.
  - Small task: `kira backup` MVP (SQLite backup API + vault/artifacts + manifest) —
    pulled forward from 17 because the data becomes valuable faster than packaging matures.
  - Core chat ergonomics (edit/regenerate, stop, copy/quote, inline artifact previews) —
    scoped in or explicitly split to an 11.5, decided in the detailed plan.
- **Safety risks:** cross-project leakage via global search (the #1 risk — SQL-enforced
  scoping, adversarially pinned with planted canaries); snippet leakage of private connector
  content (index only what's already stored/minimized; digest storage minimization stands);
  secrets in artifacts (floor check at registration + sweep over new GETs); FTS triggers
  must not break single-writer discipline.
- **Tests/evals:** trigger↔index parity incl. delete/update; idempotent rebuild; canary
  cross-project pins; mutation-route pin grows by the exact new routes; secret sweep over
  `/api/search` + `/api/artifacts`; injection-in-indexed-doc scenario (results are data;
  any model-facing reuse of snippets is framed untrusted); palette no-new-authority pin
  (R3: every palette write action maps onto an existing gated route, enumerated); artifact
  dedupe/hash tests (same content_hash → no duplicate row) + schema completeness pin (R2).
- **Live verification:** index build over the real DB; query latency target on real data;
  UX walkthrough checklist; backup+restore roundtrip on a copy; chunked eval gate.
- **Deferred:** semantic/embedding search; artifact diff viewer v2; cross-device index.
- **Effort:** Large. **Fable boundary:** yes — new data model + leakage-sensitive surface;
  checkpoint on the scoping design before the search API ships.

## Phase 12 — Action Connectors (Phase 9B fulfilled)

- **Goal:** Kairo acts on Calendar/Drive — create/update/cancel events, Meet links,
  Docs create/update — every write previewed, diffed, human-approved, journaled.
- **Why now:** the pinned second half of Phase 9; the utility jump from "knows my day" to
  "manages my day"; builds the approval-preview UX 13/16 reuse.
- **Dependencies:** Phase 9 OAuth/connector infra + egress log; Gate; 11's surfaces (soft).
- **Major features:** WriteIntent two-phase pattern (draft/preview with resolved diff →
  human approval → execute → journal row with remote id + rollback info); Calendar
  create/update/cancel + Meet link creation; Drive/Docs create/update under **drive.file
  scope only** (full-Drive scope stays out; elevated scope would be its own future phase);
  Gmail draft workflow improvements (threading, edit-in-place, **still no send**); write
  journal (outbox) table + UI; undo where the API allows; approval-queue MVP in Gate.
  Per R5: attendee/contact ambiguity handling — an ambiguous person/email is resolved by
  asking the user BEFORE any invite or draft is created; calendar previews always show
  timezone, attendees, recurrence, Meet link, and notification/`sendUpdates` behavior.
- **Safety risks:** first outward writes to real accounts. Injected content (email/doc/event
  bodies) steering writes → framed untrusted + every connector write human-approved,
  AUTO_NEVER extended (no auto mode for connector writes, ever); scope creep → scopes pinned
  to exactly what code implements; duplicate/partial writes → idempotency keys + journal;
  timezone/recurrence corruption → dry-run diff shows resolved times.
- **Tests/evals:** fake-transport tests per verb incl. retry idempotency; adversarial evals
  (injected content attempts a calendar write → surfaces as ASK with faithful preview, never
  silent); OAuth scope pin (requested == exact list); journal metadata-only sweep;
  mutation-route pin update.
- **Live verification:** real account canary event create/update/cancel (+ Meet link), Doc
  create/update, journal + undo, egress-log rows; chunked eval gate.
- **Deferred:** Gmail SEND (indefinitely, until an explicit consent-framed phase);
  Sheets/Slides; contacts; non-Google providers.
- **Effort:** Large. **Fable boundary:** yes + **mandatory mid-phase checkpoint** before
  live writes are enabled (Checkpoint-D pattern).

## Phase 13 — Research Services Live + Settings maturity

- **Goal:** enable the external research half of the catalog (Firecrawl, Exa, Jina;
  SearXNG local; image gen for Frontend) and give models/services/budgets/connectors a real
  settings screen.
- **Why now:** cheap — ADR-0015 made each service adapter+tests+flag, not a redesign;
  Studio teams become genuinely useful; first live proof of context_policy/output_trust
  against real hostile content.
- **Dependencies:** 10B catalog/ServiceTool; pricing.yaml v2 entries; live keys.
- **Major features:** Firecrawl/Exa/Jina adapters (public_only context, untrusted framing,
  metered pricing, egress + taint demotion); SearXNG (local install, still classified
  egress — it proxies out); OpenAI image gen for Frontend (untrusted_model_generated,
  metered); settings screen (models/providers, service enable/credential presence — never
  values, budgets, connectors); per-project service narrowing UI. Small task: QA-team
  execution path so QA can hold playwright_local (documented 10B follow-up) — Kairo can then
  visual-diff its own UI as a per-phase regression ritual.
- **Safety risks:** first live external content in councils — real injection pressure (B2
  framing is the defense; add live adversarial scenarios with hostile pages); metered crawl
  cost runaway (reservation must price crawl ops; per-run/service caps); credentials in the
  settings UI (presence-booleans pin).
- **Tests/evals:** per-adapter keyless fakes; catalog invariants; hostile-page scenarios
  (instructions inert); unpriced-metered blocks; taint private-read→egress demoted.
- **Live verification:** keyed research workflow runs; Costs attribution matches SQL; canary
  proof that private content cannot reach a public_only service; chunked gate + ratchet.
- **Deferred:** Figma / GitHub / Docker / Supabase/Neon / **Google Stitch** MCP (no MCP client
  yet — deliberate), CodeQL, Promptfoo, Browserbase. **Google Stitch** is a cataloged
  Frontend/Product design-**generation** service (`google_stitch`, egress, `project_non_private`,
  `untrusted_model_generated`, key `GOOGLE_STITCH_API_KEY`, disabled-by-default): it needs the MCP
  client layer + a review of the official Stitch MCP package before enablement. Its output
  imports as artifacts (`produced_by=google_stitch`) — never executed/committed; Claude/Opus
  adapts the design into Kairo's frontend. The MCP client layer is the gating decision (~13–15).
- **Effort:** Medium. **Fable boundary:** yes — checkpoint before the egress flags flip;
  the plan itself can be lean (ADR-0015 did the heavy lifting).

## Phase 14 — AI Team Office (visual orchestration layer)

- **Goal:** an optional, per-project-customizable "office" view over the Studio — teams as
  rooms, members as avatars with live status, runs visualized as gather → work → meet →
  review → verdict, live activity feed, per-agent inspect. Serious compact mode and playful
  visual mode. **Render-only**: a visualization/control surface over existing routes.
  Per R6: it is an optional alternate view, NEVER the default — the calm Studio stays.
- **Why now (not 11):** Phase 11 must stay focused on findability; by 14 the office has
  something real to show (scanners, research, browsers from 13) and rides 11's Activity
  feed + existing WS v2 events + member_runs read models.
- **Dependencies:** Studio WS v2 (exists), 11's activity feed, 13's services (soft — for
  liveliness).
- **Major features:** 2D office canvas (DOM/canvas, dependency-light — no game engine);
  per-project layout (rooms/areas per TeamProfile incl. Custom, department labels,
  colors/icons, meeting area, status zones) persisted per project; avatars/personas per
  member showing role, model/provider, current stage, status (idle/working/meeting/
  reviewing), cost so far, tools/services chips, last output summary — all from existing
  read models; stage mapping (council = meeting table; synthesis = head's office; execution
  = writer's desk with a turn-lock indicator; review; verdict); hand-off animations between
  stages; per-agent panel linking to trace/Gate; layout editor; mode toggle (serious =
  existing Studio timeline aesthetic; playful = the office).
- **Safety risks:** low by construction, pinned anyway — the only new mutation route is
  layout-save; every start/cancel/approve goes through the existing OrchestrationController
  and Gate (no new authority); agent-authored text is escaped, never rendered as HTML;
  the feed replays metadata + short summaries only (never pruned/private bodies).
- **Tests/evals:** mutation pin grows by exactly layout-save; office-state derivation from
  events (pure read-model tests); escaping/XSS tests; no-new-authority pin.
- **Live verification:** watch real orchestrations in the office; layout editor roundtrip;
  performance on long runs.
- **Deferred:** pathfinding/wandering ambiance, pets/weather/day-night (playful backlog);
  voice presence. **License note:** my-virtual-office is AGPL-3.0-or-later — UX reference
  ONLY; no code or asset reuse; all sprites/CSS are Kairo's own or permissively licensed.
- **Effort:** Medium-Large (frontend-heavy). **Fable boundary:** yes for UX/interaction
  design + render-only pins; no adapter-class safety checkpoint needed.

## Phase 15 — Memory Graph & Obsidian projection

- **Goal:** entities/topics/decisions/people/tools/repos extracted from memories/chats/KB
  into a reviewable, project-scoped graph; Obsidian-compatible wikilink/frontmatter export.
  SQLite stays canonical; Obsidian is a projection.
- **Why now:** post-11 search shows *what* exists; the graph shows *how it connects*; feeds
  richer Overview pages and gives 16's dreaming real substrate.
- **Dependencies:** memory/KB stores; 11's extraction/index plumbing; model registry
  (extraction is ledgered LLM work).
- **Major features:** graph tables (typed nodes/edges, every edge evidence-linked to source
  rows); extraction as background **proposals** — nothing lands without review (suggestion
  queue with accept/reject/bulk-reject, Gate-like); per-project graph UI (Overview embed +
  full view); vault wikilinks aligned; Obsidian export (frontmatter + wikilinks) per
  project, respecting sensitive floors and excluding private-sourced rows by default.
- **Safety risks:** hallucinated edges polluting memory (review-first + mandatory
  provenance); private content in exports (per-project only, class-filtered, floor-checked
  export dir); extraction cost (budgeted, batched, utility route).
- **Tests/evals:** suggestion state machine; edge-without-evidence rejected; export golden
  files; project-boundary pins on graph queries + export; extraction-precision eval
  scenarios with planted entities.
- **Live verification:** extract over the real corpus, review a batch, open the export in
  real Obsidian; nightly extraction cost check; chunked gate.
- **Deferred:** embedding-similarity edges; cross-project graph (deliberately never);
  graph-driven retrieval into prompts (later, separately gated).
- **Effort:** Medium-Large. **Fable boundary:** yes — new derived-data model + review
  contract.

## Phase 16 — Attention & Automation (Notification Center + Dreaming)

- **Goal:** one attention system — approval queue, priorities, urgent-vs-digest separation,
  review queues, Telegram/Kakao routing — plus proposal-only automation: nightly review,
  morning briefing, bottleneck detection, ROI/time-saved summaries, self-improvement
  proposals.
- **Why now:** by 16 Kairo generates many attention objects (12's writes, 13's asks, 15's
  suggestions); automation without a mature attention surface is noise. Merged because
  dreaming's only output channel IS the proposal queue.
- **Dependencies:** 12 (write approvals), 15 (suggestions feed in), scheduler/digest/
  notifications (exist), UnattendedGate (exists).
- **Major features:** unified attention_items model (approval / review / proposal / alert;
  priority; project; source; state); Notification Center screen that **absorbs** the Gate
  list — evolving the one-attention-surface pin, not duplicating it; routing rules (urgent →
  Telegram/Kakao push with minimized bodies; rest → digest); dreaming jobs under
  UnattendedGate with hard budget caps: day review, morning briefing, bottleneck detection
  over ledger/tasks, ROI summaries, self-improvement proposals ("this workflow keeps
  getting rejected at review — proposal: adjust the template"). Dreaming can draft
  artifacts and attention items only — its tool scope is read-only + Kairo-internal
  proposal writes; it can never execute connector writes, shell, or egress.
- **Safety risks:** autonomy creep (scope pin adversarially enforced; AUTO_NEVER and
  unattended contracts untouched); notification fatigue (priority discipline, digest
  default); private content in pushes (titles + counts, per digest minimization).
- **Tests/evals:** dreaming scope pin (no egress/write tool reachable — adversarial);
  attention lifecycle; routing matrix; budget cap halts a dreaming run; injected content in
  reviewed material cannot escalate a proposal into an action.
- **Live verification:** run nightly/morning for a real week; verify routing, briefing
  quality, cost per night; chunked gate.
- **Deferred:** closed-loop self-improvement (auto-applying proposals) — indefinitely,
  until a dedicated consent-framed phase; workflow auto-tuning.
- **Effort:** Large. **Fable boundary:** yes + **mandatory checkpoint** (unattended
  background LLM work touching everything).

## Phase 17 — Packaging, Daemon & Multi-Device

- **Goal:** Kairo as a durable installed product: daemon/tray autostart, health checks,
  full backup/restore + MacBook migration, sync-friendly data layout, settings polish,
  first-run onboarding.
- **Why now:** hardens everything that exists; lands when the MacBook move is real.
- **Dependencies:** all prior; grows 11's backup MVP into full restore/migrate.
- **Major features:** daemon + tray (Windows) / menubar (macOS) with autostart;
  single-instance + port management; health endpoint + self-diagnostics screen (db
  integrity, index freshness, connector token expiry, scheduler liveness, egress-log
  summary); versioned backup/restore with a **tested restore path** (secrets excluded /
  handled separately; encrypt-at-rest option); `kira migrate` for the MacBook move (path
  translation; credentials re-entered, never silently copied); Syncthing guidance (vault/
  artifacts sync; the DB and token stores NEVER sync — instance-lock guard); onboarding
  wizard + demo-mode content.
- **Safety risks:** backup as exfiltration surface (no raw tokens; encryption option);
  restore clobbering (explicit, previewed, never automatic); two devices on one DB via sync
  = corruption (hard rule + lockfile/instance-id guard).
- **Tests/evals:** backup/restore roundtrip incl. migration-version mismatch; instance
  lock; diagnostics read models; secret sweep over diagnostics.
- **Live verification:** the actual MacBook migration + a restore drill on a fresh machine.
- **Deferred:** mobile app; multi-user; cloud relay; two-way remote access (see below).
- **Effort:** Medium-Large. **Fable boundary:** yes (data-loss-class risk).

---

## Merge / split decisions

- **Merged:** UI/UX maturity (candidate 9) is distributed, not a phase — 11 (project/
  hierarchy/empty states), 13 (settings), 14 (Studio/office), 17 (onboarding). Polish tied
  to features has a forcing function; a "polish phase" doesn't.
- **Merged:** Notification Center (8) splits — approval-queue MVP into 12 (writes need it),
  full center + Dreaming (5) into 16 (proposals need the queue; the queue needs producers).
- **Split out:** Life OS (6) → deferred adapter track post-13; SearXNG can ride 13.
- **Split out:** semantic search out of 11 (FTS5 first — local, free, proven; embeddings
  after 15).
- **Pulled forward:** backup MVP from 17 into 11.
- **Kept separate:** 12 and 13 both flip real-world switches; each earns its own checkpoint
  and live verification.

## Missing areas (not in the candidate list)

1. **Two-way remote access** — Telegram/Kakao as a remote *chat + approval* channel, not
   just notifications. Huge daily-driver value; voice-consent-class safety design
   (approving a write from a phone must be as deliberate as VoiceApprover). Candidate
   Phase 18, or approval-only actions folded into 16's routing.
2. **Backup/restore urgency** — addressed by pulling the MVP into 11.
3. **Core chat ergonomics** — edit/regenerate/branch, stop, copy/quote, inline artifact
   previews. Scoped into 11 or an explicit 11.5.
4. **Data lifecycle & scale** — retention, archive compaction, VACUUM/FTS maintenance,
   pagination/virtualized lists. Sliced into 11 (index maintenance) and 17 (retention).
5. **Security posture surface** — the Phase 9 egress log has no UI; audit view + connector
   token-expiry warnings belong in 11's Activity or 17's diagnostics.
6. **Dogfooding QA** — the QA-team playwright execution path (documented 10B follow-up)
   lets Kairo visual-diff its own UI each phase; small task in 13.
7. **Accessibility + keyboard completeness** of the workstation itself (11's palette
   starts it; a11y pass rides each UI phase).

## Handoff prompt for the Phase 11 detailed plan

> Plan Phase 11 — "Workstation Foundation: Search, Artifacts & Project UX" — as
> `docs/PLAN-11-workstation.md`, in the established format (PLAN-10B-teams.md precedent):
> context, architecture, per-task order with separate commits, safety non-negotiables
> pinned by tests, tests/evals, live verification, and handoff instructions for Opus 4.8.
>
> Baseline: Phase 10B complete (Tasks 10–19; 1260 passed / 1 skipped; ruff clean;
> migrations v8). Pre-condition: the 10B live checklist (`docs/verification-10B.md`) and
> chunked eval gate have been run GREEN and the two new adversarial baselines ratcheted —
> do not start Task 1 otherwise. FTS5 is confirmed available (SQLite 3.53.1).
>
> Scope IN: (a) FTS5 search service over chats/project chats/memories/KB-vault/tasks/
> orchestration summaries/digests/meeting notes/artifact metadata, with triggers + rebuild;
> (b) project- and provenance-scoped query model, global + per-project search, filters
> (project/label/date/type/model-team/status); (c) Artifacts Library — table + registration
> API adopted by digest/orchestration/eval/wiki writers, files under
> `data/artifacts/<project>/`; each record carries `content_hash`, `origin_type` +
> `origin_id`, `created_by` (user|agent|system), producing team/role/model,
> sensitivity/provenance class, and `external_uri` vs `local_path` as clearly separated
> fields; registration dedupes by hash (R2); (d) project labels/categories,
> pinned/favorites, archive, groups; (e) richer project pages (Overview/Chats/Artifacts/
> Memory/Tasks/Vault/Studio/Costs/Activity) with a unified Activity feed read model;
> (f) command palette + keyboard-first navigation; (g) UX maturity pass (empty states,
> hierarchy, calm premium density); (h) `kira backup` MVP; (i) decide in-plan whether
> core chat ergonomics (edit/regenerate/stop/quote/inline artifact previews) fits or
> splits to 11.5; (j) Saved Views / Smart Collections (R1): built-ins "Recent artifacts",
> "Needs review", "Generated this week", "By team/model", "Pinned project work", plus
> user-defined filters saved per project.
> Scope OUT: semantic/embedding search, the AI Team Office view (Phase 14 — but the
> Activity feed must be designed as its replayable substrate), connector writes, memory
> graph.
>
> Binding constraints:
> C1 — search scoping enforced in SQL (project_id + provenance class on every indexed
> row); global search never returns another project's private-derived rows; adversarial
> canary pins mandatory.
> C2 — search returns snippets + pointers only, never full bodies; secret sweep extends
> over `/api/search` and `/api/artifacts`; any model-facing reuse of snippets is framed
> untrusted.
> C3 — artifact registration is floor-checked: path must resolve under the artifacts root
> and never on a sensitive path; artifacts are metadata rows + files, never blobs in SQLite.
> C4 — index only content Kairo already stores (digest storage minimization stands; no new
> retention of connector bodies for search's sake).
> C5 — FTS triggers ride the single connection under the shared write lock; migration v9
> is additive plain SQL; index rebuild is idempotent and offline-safe.
> C6 — the closed mutation-route pin grows only by named routes (artifact register/label/
> pin, project label/pin/archive/group, layout of pages is GET-only).
> C7 — the Activity feed is metadata + short summaries only and is designed to be replayed
> by the future office view (Phase 14) without change.
> C8 — the phase is keyless end-to-end; no new model/API dependencies; eval-gate re-run at
> the end is the only spend.
> C9 — UX bar is part of the definition of done: empty states that teach, keyboard-first,
> and a screenshot set at the checkpoint covering empty state, populated state,
> mobile/narrow, project page, search results, and artifact preview — captured via
> playwright_local where possible (R4).
> C10 — MANDATORY Checkpoint E after the search core + scoping model (before the search
> API/UI is exposed), with canary-leak evidence per bullet, then continue.
> C11 — the command palette is permission-aware (R3): read/jump actions run immediately;
> write/generate actions route through the existing Gate/turn flow; Ctrl+K introduces no
> new authority — every palette write action maps onto an existing gated route, and a pin
> enumerates them.
>
> Standing rules: never commit `docs/PLAN.md` or `docs/PLAN-7-voice-consent-checkpoint.md`;
> per-task commits with explicit paths ending
> `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`; never weaken
> PermissionGate/taint/modes/project boundaries; chunked eval rule stands (~14-min cap);
> baseline changes get a dedicated commit.
