# Kairo — Phases 12–16 master execution plan

*(Planned 2026-07-08 by Fable 5. Supersedes nothing: `docs/ROADMAP-post-10B.md` (approved with
R1–R7) remains the product roadmap; THIS document is the execution layer over it — dependency
map, ordering, shared substrate, checkpoints, rituals, and the Opus handoff. Phase 17 is
explicitly OUT of scope here except where a dependency must be noted.*

*Baseline: Phase 11 (Kairo Workstation) COMPLETE on main — 1522 passed / 2 skipped, ruff clean,
migrations at **v9**, core keyless replay gate 19/19 $0, screenshot DoD 0 violations, ADR-0017.
Phase 10C providers complete keyless, flag-off. Phase 10B Checkpoint D live-verified GREEN.
Next ADR number: 0018. Standing rule: NEVER commit `docs/PLAN.md`,
`docs/PLAN-7-voice-consent-checkpoint.md`, `mcp_sample.json`, `config/settings.yaml`,
`config/permissions.yaml`, or `design/`.)*

---

## 1. Dependency map (12 → 16)

```
                 Phase 11 surfaces (DONE): Artifacts Library, FTS5 search, Activity feed,
                 Workspace tabs, Palette, Settings screen, Gate UI, WS v2 events
                        ▲            ▲            ▲            ▲           ▲
                        │            │            │            │           │
  ┌─────────────────────┴──┐   ┌─────┴──────┐  ┌──┴────────┐ ┌─┴────────┐ ┌┴───────────────┐
  │ 12 Action Connectors   │   │ 13 Research│  │ 14 Team   │ │ 15 Memory│ │ 16 Attention & │
  │ (first outward writes) │   │ Services   │  │ Office    │ │ Graph +  │ │ Automation     │
  │                        │   │ Live +     │  │ (render-  │ │ Obsidian │ │ (unattended)   │
  │ builds: WriteIntent,   │   │ Settings   │  │  only)    │ │          │ │                │
  │ journal/outbox,        │   │ maturity   │  │           │ │ builds:  │ │ consumes:      │
  │ approval-queue MVP ────┼──▶│            │  │           │ │ proposal │ │ 12 approvals,  │
  │                        │   │ hardens:   │  │ consumes: │ │ queue ───┼▶│ 15 proposals,  │
  │ HARD DEP: Phase 9      │   │ B1/B2 vs   │  │ WS v2 +   │ │          │ │ 13 asks,       │
  │ OAuth infra (done)     │   │ real       │  │ Activity  │ │ HARD DEP:│ │ UnattendedGate │
  │                        │   │ hostile    │  │ feed      │ │ memory/KB│ │ (done)         │
  │                        │   │ content    │  │ (both     │ │ stores + │ │                │
  │                        │   │            │  │  done)    │ │ 11 index │ │ HARD DEPS:     │
  │                        │   │ HARD DEP:  │  │           │ │ plumbing │ │ 12 (queue has  │
  │                        │   │ 10B catalog│  │ SOFT DEP: │ │ (done)   │ │ producers) +   │
  │                        │   │ (done)     │  │ 13 (life) │ │          │ │ 15 (proposals) │
  └────────────────────────┘   └────────────┘  └───────────┘ └──────────┘ └────────────────┘
        outward writes           external egress   no authority   memory        unattended
        (Checkpoint G)           (Checkpoint H)    (sign-off I)   permanence    automation
                                                                  (Checkpoint J)(Checkpoint K)
```

Hard edges (cannot reorder):
- **16 after 12**: the attention queue's first real producers are connector-write approvals;
  16's routing generalizes 12's approval-queue MVP.
- **16 after 15**: dreaming's only output channel is the proposal queue 15 builds.
- **14 after 11 only** (both hard deps are shipped); 13 is a soft dep (liveliness, not
  correctness).
- **13 after 10B only** (shipped); it does not need 12.

Soft edges (recommended, not required):
- 12 before 13: the preview→approve→journal UX from 12 is the pattern 13's settings/consent
  surfaces echo; 12 also honors the standing 9B promise first.
- 15 before 16: could technically ship 16's attention center without graph proposals, but it
  would launch half-empty.

Phase 17 dependencies to note (NOT planned here): 17 grows 11's `kira backup` MVP into full
restore/migrate; 17's diagnostics screen wants 12's write journal + 13's egress/settings
surfaces + 16's scheduler-liveness signals. Nothing in 12–16 may assume 17 exists.

## 2. Recommended execution order

**12 → 13 → 14 → 15 → 16.** As approved in the post-10B roadmap, and re-confirmed:

1. **12 first** — the 9B promise is standing and pinned; its WriteIntent/journal/approval
   substrate compounds into 13 and 16; it is the highest-utility jump ("manages my day").
2. **13 second** — cheap (ADR-0015 made each service an adapter+tests+flag), fast wins, and
   the first live proof of context_policy/output_trust against real hostile content while the
   12 muscle-memory (checkpoint → live canary → ratchet) is fresh.
3. **14 third** — pure render-only breather between the two safety-heavy arcs; by then runs
   have real variety (scanners + research + image gen) to show.
4. **15 fourth** — the corpus keeps growing while 12–14 ship; extraction lands on a richer
   corpus and feeds 16.
5. **16 last, deliberately** — unattended automation lands on mature surfaces (queue
   producers from 12/13/15 all exist), never creates them.

Swap rule (pre-authorized): if daily-driver utility is starving, 14 may slide after 15 — 14
has no dependents. No other reordering without a new plan review.

**Substrate first.** Before the arc's first *live* run, build the Context Reuse substrate
(§4A, S7). It is keyless and adds no authority, so it slots in as the next implementation unit
after Phase 12 Milestone 1 (or as parallel-prep), and it makes every subsequent orchestration
fan-out, planner/judge/review agent, and long project session cheaper and faster on every
provider — so it must land before the Phase-12 live canary and any Phase-13 live run.

## 3. What can be prepared in parallel (safely)

Parallel here means: while phase N sits at a checkpoint or awaits a live ritual on Habib's
machine, Opus may build **keyless, flag-off, no-authority** pieces of a later phase in a
separate branch/session. Never two live-flag flips in flight at once.

**Safe to prepare in parallel (all keyless, all inert until flagged/approved):**
- 13's `SERVICE_CATALOG` rows + `pricing.yaml` v2 service entries + keyless fake-transport
  adapter tests (adapters exist but flags stay `services.enabled=[]`).
- 14's office canvas, layout editor, and status-node rendering — it consumes only existing
  read models; a UI branch can mature while 12/13 checkpoints wait.
- 15's migration design + graph read models + suggestion-queue state machine with fixture
  data (no extraction job scheduled).
- The **Context Reuse substrate (§4A, S7)** — capability metadata, the stable-first prompt
  assembler + prefix hashes, the `ContextReusePolicy` adapters, and the normalized cache ledger
  are all keyless / no-authority; build them here, before any live run makes tokens expensive.
- Shared test helpers (§13) — write once, early.
- Docs/ADR drafts for the next phase.

**Must be strictly sequential (one at a time, each behind its own checkpoint):**
- **Outward writes** (12): enabling any live Calendar/Drive/Docs write scope or verb.
  Checkpoint G first; nothing else live-flips while 12's canary ritual is open.
- **External egress** (13): flipping `services.enabled` for Firecrawl/Exa/Jina/SearXNG/image
  gen. Checkpoint H first.
- **Memory permanence** (15): the first extraction run over the real corpus, and enabling the
  Obsidian export path. Checkpoint J first — a polluted memory graph or a leaked export is
  not undoable by `git revert`.
- **Unattended automation** (16): scheduling any dreaming job. Checkpoint K first, then the
  week-long observation window with nothing else changing underneath it.

## 4. Shared substrate — build once, reuse four times

These are the pieces that make the arc fast. Each is built in exactly one phase (owner bold)
and consumed by the rest. Do NOT generalize further than stated — the phases stay separate
because 12/13/15/16 each flip a different class of irreversible switch and each earns its own
checkpoint; only the plumbing is shared.

| # | Substrate | Owner | Reused by | Shape |
|---|---|---|---|---|
| S1 | **WriteIntent two-phase state machine** (`src/kira/actions/intents.py`): draft → previewed(diff) → approved → executed / failed → undone; idempotency key; per-intent preview renderer | **12** | 16 routes intent approvals into attention items; 15's export "apply" reuses the preview→approve shape | migration v10 `write_intents` table |
| S2 | **Write journal / outbox** (`connector_writes`): metadata-only row per executed write — remote id, verb, scope, rollback info, egress-log link | **12** | 16's briefings/ROI read it; 17 (noted only) diagnostics read it | same v10 migration |
| S3 | **Pending-decision read model** (approval-queue MVP in the Gate screen): kind, project, source, preview pointer, state, priority | **12** (MVP) | **16 absorbs it** into `attention_items` — evolve the one-attention-surface pin, never duplicate it | GET read model now; table in 16 (v13) |
| S4 | **Proposal/review queue** (`proposals` table: payload, evidence links, state accept/reject/bulk-reject, provenance) | **15** | 16's dreaming writes ONLY proposals; graph suggestions and dreaming proposals are the same row kind with different `source` | migration v12 |
| S5 | **Settings maturity panels** (models/providers, services enable + credential presence-booleans, budgets, connectors, per-project narrowing) | **13** | 12 adds its connector-scope panel into the same screen; 15/16 add export/automation toggles as rows, not new screens | extends Phase 11 `settings.js` |
| S6 | **Untrusted-framing + provenance plumbing** (B1 context_policy, B2 output_trust, taint demotion, egress log) | shipped (9/10B) | ALL — no phase adds a second framing mechanism | reuse, never fork |
| S7 | **ContextReusePolicy** (provider-agnostic prompt/context caching: capability metadata in the ModelRegistry + client-layer adapters + stable-first prompt ordering + normalized cache ledger — see §4A) | **early substrate** (before Phase-12 live) | ALL — every provider / route / orchestration fan-out / long session | migration v11 (normalized cache columns on `model_calls`) |

Design rule for S1–S4: fields that 16 will need (priority, source, project_id, state
timestamps) are in the schema from day one, so 16 is a read-model + routing phase, not a
migration-churn phase.

## 4A. Cross-cutting substrate — Context Reuse (provider-agnostic prompt/context caching)

**What & why.** One small, foundational layer that makes Kairo's prompts *reusable* across every
provider behind a single `ContextReusePolicy` in the model-registry / client layer. It is
explicitly **not** "Anthropic prompt caching with a coat of paint": Anthropic (`cache_control`,
5m/1h TTL, cache read/write usage), OpenAI (automatic prefix caching, `prompt_cache_key`,
`cached_tokens`), Gemini (implicit caching by default + explicit `CachedContent` resources),
DeepSeek (automatic on-disk prefix caching), and Qwen/DashScope (`cache_control` blocks) all differ
— so **capability is data and behavior is derived**. It lands **early** (the next keyless unit,
before Phase-12's live canary and Phase-13's live research) because it cuts cost + latency for
exactly the 12–16 workloads: orchestration fan-outs, planner/judge/review agents, and long project
sessions. It is keyless-testable end to end (fake clients + cassettes assert the emitted controls,
the ordering, the hashes, and the normalized ledger) — no live key to build or verify it.

**Not a phase.** One shared-substrate task (S7), ~6 keyless sub-steps, one additive migration. It
reduces the cost of everything after it and changes **no authority**.

**The five `ContextReusePolicy` modes** (resolved per (provider, model) from capability metadata;
an unknown/unverified provider resolves to `off`):
1. `off` — emit no cache controls (still benefits from stable ordering).
2. `automatic_prefix` — the provider caches long repeated prefixes itself (OpenAI, DeepSeek);
   Kairo only orders the prompt + optionally sets a cache key.
3. `explicit_breakpoint` — Kairo marks cache breakpoints in-request (Anthropic `cache_control`;
   Qwen/DashScope `cache_control`).
4. `explicit_resource` — Kairo creates/reuses a provider-side cached resource (Gemini
   `CachedContent`), for large stable docs/media, **privacy-reviewed** first.
5. `provider_default` — defer entirely to the provider's own default (Gemini implicit caching);
   no Kairo action beyond ordering.

**Capability metadata** (on `ProviderSpec` / model in the ModelRegistry; fail-closed — absent ⇒
conservative `off`): `supports_context_reuse`, `context_reuse_mode` (one of the five),
`supports_cache_key`, `supports_cache_ttl`, `reports_cached_tokens`, `cache_min_tokens`,
`cache_ttl_options`, `cache_private_allowed`. **Z.ai and any unverified provider = `false` until
confirmed against live docs.**

**Prompt assembly — stable first, volatile last** (the ordering IS the portable win; it helps even
`off`/unknown providers, and it is what every explicit/automatic scheme keys off):
- **STABLE prefix (cacheable):** system safety contract → Kairo playbooks/skills → tool schemas →
  team profiles → service-catalog summaries → stable per-project instructions.
- **VOLATILE tail (never a cache anchor):** latest user turn, current time, pending approvals,
  memory recall, search snippets, connector data, web/email/calendar content.

**Stable-prefix hashes** (the identity of what's cached; recorded in the ledger; any change busts
the cache deliberately): `system_contract_hash`, `tool_schema_hash`, `team_profile_hash`,
`service_catalog_hash`, `project_policy_hash`, and their composite `stable_prefix_hash`.

**Per-provider behavior** (derived from capability metadata, never hardcoded per call site):
- **Anthropic / Qwen(DashScope):** `explicit_breakpoint` — set `cache_control` at the
  stable/volatile seam (1h TTL only where the workload is long-lived AND allowed).
- **OpenAI:** `automatic_prefix` — rely on prefix caching; set `prompt_cache_key` per stable-prefix
  identity where it helps route to a warm cache.
- **Gemini:** `provider_default` (implicit) to start; `explicit_resource` (`CachedContent`) ONLY
  for large stable docs/media and ONLY after a privacy review.
- **DeepSeek:** `automatic_prefix` — rely on the stable ordering (disk prefix cache); no per-request
  control.
- **Z.ai / unknown:** `off` — no cache-specific behavior, but still gets the stable ordering.

**Cost ledger — normalized across providers** (additive migration on `model_calls`; a field a
provider does not report is NULL, never a fabricated 0): `input_tokens`, `output_tokens`,
`cached_input_tokens`, `cache_creation_tokens`, `cache_read_tokens`, `provider_cache_mode`,
`provider_cache_hit_tokens`, `estimated_cache_savings_usd`, `stable_prefix_hash` (plus the existing
route / team / role / stage / project / model). Each provider's client maps its own usage fields
(Anthropic cache_creation/cache_read; OpenAI cached_tokens; Gemini cachedContentTokenCount;
DeepSeek prompt_cache_hit/miss) onto these normalized columns. Metadata-only — token counts + a
hash, never prompt text (§7.8 still holds).

**Cost Center surfaces** (extend Phase 11's Cost screen + Phase 13's settings/cost work): cache-hit
tokens, cache write/read tokens, estimated savings, cache-hit-rate by provider / model / project /
team, and "which routes benefit most" (biggest savings first). All presence/aggregate — never
prompt content.

**Safety (non-negotiable — pinned; see §7.13):** caching NEVER weakens `context_policy`,
private-data routing, taint/egress, project scoping, or retention. DEFAULT caches only the
**stable, non-sensitive prefix**. Private/project content is cacheable ONLY when ALL hold: (1) the
provider is allowed for private context (`private_ok`), (2) the model route permits it,
(3) `cache_private_allowed` is true, (4) TTL/storage behavior is documented, (5) the audit ledger
records it. **Cache is NOT memory** — never persistent storage, never a retrieval path. **No
prewarming with private connector data** by default. Replay/cassette evals stay deterministic and
keyless (cache controls are asserted, not exercised live).

**Tasks (S7 — keyless, no new authority):**
1. Capability metadata on `ProviderSpec` / ModelRegistry (the 8 fields) + fail-closed resolver
   (unknown ⇒ `off`) + per-provider defaults verified against live docs; Z.ai/unknown = false. Pins.
2. Prompt assembler: stable-first / volatile-last section ordering + the five stable-prefix hashes
   + composite `stable_prefix_hash`. Pure, golden-tested.
3. `ContextReusePolicy` + per-provider adapters in the client layer (emit the correct control per
   mode; `off` for unknown). Fake-client tests assert the EXACT emitted controls per provider.
4. Additive migration (**v11**): the normalized cache columns on `model_calls`; the LedgeredClient
   maps each provider's usage → the normalized fields + `estimated_cache_savings_usd`.
5. Cost Center read models + surfaces (hit tokens, read/write, savings, hit-rate by dimension,
   top-benefit routes).
6. Safety pins + docs: the private-content-caching 5-condition gate (adversarially pinned), the
   "cache-is-not-memory" pin, the no-private-prewarm pin, an ADR (context reuse), and the
   privacy-review note for Gemini `explicit_resource`.

**Placement / boundary.** Build as the FIRST implementation unit after Phase 12 Milestone 1 (or as
parallel-prep — keyless/no-authority), and it MUST precede the Phase-12 live canary and any
Phase-13 live run. Migration **v11** (this) shifts the later data-phase migrations to **v12**
(graph) / **v13** (attention). Fable owns the per-provider capability doc (verified against live
provider docs) before any provider's non-`off` mode ships; Opus implements S7 in order, per-task
commits.

## 5. Phase-by-phase execution plans

Discipline for every task in every phase (unchanged from 10B/11): keyless green
(`uv run pytest` + `uv run ruff check .` + `uv run kira eval gate --suite core` replay/$0)
before each commit; per-task commits with explicit paths ending
`Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`; adversarial review
before commit; never commit red; forbidden files untouched.

### Phase 12 — Action Connectors (fulfills the 9B pin) — ~10 tasks, migration v10

**Goal:** Calendar create/update/cancel + Meet links + Drive/Docs create/update, every write
previewed → diffed → human-approved → journaled → undoable where the API allows. Gmail stays
drafts-only forever.

**Exact OAuth scopes after 12** (pinned by test: requested == exactly what code implements):
- `calendar.readonly` (existing — calendar list / free-busy reads)
- `calendar.events` (NEW — event create/update/cancel; Meet via
  `conferenceData.createRequest` inside the same verbs, no extra scope)
- `drive.readonly` (existing reads), `drive.file` (NEW — only files Kairo creates/opens; the
  Docs API accepts `drive.file`, so NO `documents` scope and NO full `drive` scope)
- `gmail.readonly`, `gmail.compose` (unchanged; NO send method/tool/route — grep pin stands)
- Re-consent is a live-ritual step (reconnect with the new scopes; friendly reconnect errors
  from the Phase 9 amendments apply).

**Tasks:**
1. **Plan-of-record + migration v10 + WriteIntent core.** Commit this doc; additive plain-SQL
   v10: `write_intents` + `connector_writes` (S1/S2 schemas incl. the 16-ready fields).
   State machine + idempotency keys + unit tests. No connector code yet.
2. **Preview/diff builder.** Renders a resolved preview for every verb BEFORE approval:
   timezone-resolved start/end, attendees, recurrence (expanded next-3 occurrences), Meet
   link yes/no, `sendUpdates` behavior, and for updates a field-level diff vs the live remote
   state (R5). Pure functions, golden-file tests.
3. **Attendee/contact resolution.** Ambiguous person/email ⇒ the turn ASKS the user and NO
   intent row is created until resolved (R5). Pinned: an unresolved attendee can never reach
   the preview stage.
4. **Calendar write adapter** (create/update/cancel + Meet), fake-transport tests per verb
   incl. retry idempotency (same key ⇒ no duplicate event) and partial-failure journaling.
5. **Drive/Docs write adapter** under `drive.file` only (Docs create + `batchUpdate`
   edit-in-place); scope pin test; rollback info = Drive revision id where available.
6. **Gmail draft improvements** — threading, edit-in-place of existing drafts; the no-send
   grep/test pin re-asserted in the same commit.
7. **Tools + prompts + permissions.** Connector write tools: `permission=ASK`,
   **AUTO_NEVER extended** (no auto mode for connector writes, ever), outside PLAN_SAFE,
   refused by UnattendedGate (unattended may propose an intent, never execute). Route pin
   grows by the named intent routes (create/approve/reject/execute/undo).
8. **Approval-queue MVP + journal UI.** Gate screen gains the pending-writes queue (S3);
   Workspace/Daily surface journal rows + undo buttons; outputs register as artifacts
   (`origin_type=connector_write`) so they land in the Phase 11 Library.
9. **Adversarial evals.** Injected email/doc/event body attempts a calendar write ⇒ surfaces
   ONLY as an ASK with a faithful preview (never silent, never auto); forged/edited preview
   inert (execute uses the stored intent, not model text); cross-project intent access 403;
   taint: private read → write attempt in one turn still demoted per existing rules.

   **⛔ CHECKPOINT G — MANDATORY STOP before any live write** (Checkpoint-D pattern). See §6.

10. **Live canary ritual + docs.** §8 ritual; ADR-0018 (WriteIntent contract), README,
    `docs/verification-12.md`; ratchet only in a dedicated commit if the chunked gate says so.

**Deferred:** Gmail SEND (indefinitely); Sheets/Slides; contacts API; non-Google providers;
elevated/full Drive scope (its own future phase).

### Phase 13 — Research Services Live + Settings maturity — ~8 tasks, no migration expected

**Goal:** flip the research half of the catalog live (Firecrawl, Exa, Jina; SearXNG local;
OpenAI image gen for Frontend) and give models/providers/services/budgets/connectors a real
settings surface. First live hostile content into councils — B1/B2 proven against reality.

**Tasks:**
1. Plan-of-record + `pricing.yaml` v2 rows for every service being enabled + catalog row
   updates (context_policy `public_only`, output_trust `untrusted_external_content` /
   `untrusted_model_generated` for image gen). Unpriced ⇒ stays fail-closed.
2. **Firecrawl + Exa + Jina adapters** — keyless fake-transport tests; egress ⇒ taint
   demotion automatic; framing per B2. (Jina ships only if it still clears the "thin value
   over web_fetch" bar at implementation time — decide in the task, don't carry it dead.)
3. **SearXNG adapter** — local install, still classified egress (it proxies out); docker/CLI
   presence check ⇒ availability.
4. **Image generation (OpenAI) for the Frontend team** — metered, `untrusted_model_generated`,
   outputs register as artifacts (kind `design`, `created_by=agent`) into the Library.
5. **Settings maturity (S5).** Panels: models/providers (10C rows: enable per provider,
   presence-booleans for keys, authority tiers displayed read-only — planner/judge/utility
   pinned anthropic, NOT editable); services (enable/disable, credential presence, pricing
   state, deferred reasons); budgets (per-run/day/service caps); connectors (scopes granted
   vs implemented, reconnect). Per-project service narrowing UI (narrow-only). Every write
   here is a new named mutation route — pin grows explicitly.
   **Google Stitch stays a disabled-by-default catalog row** — enabling it requires the MCP
   client layer (a deliberate ~13–15 architecture decision, NOT made implicitly here).
6. **Cost reservation over service ops** — crawl/search/image ops priced into the worst-case
   reservation; per-service caps enforced mid-run; runaway-crawl test.
7. **Adversarial evals** — hostile-page cassettes (instructions inside fetched content are
   inert; framing survives snippet reuse); private/taint canary: a private-provenance bundle
   is REFUSED to a `public_only` service role (B1, engine-level, adversarially pinned);
   settings screen secret sweep (presence-booleans only).

   **⛔ CHECKPOINT H — MANDATORY STOP before the egress flags flip live.** See §6.

8. **Live hostile-content verification + QA execution path + docs.** §8 ritual. Small rider:
   the QA-team playwright_local execution path (documented 10B follow-up) so Kairo can
   visual-diff its own UI as a per-phase ritual from 14 onward. ADR-0019, README,
   `docs/verification-13.md`.

**Deferred:** Figma/GitHub/Docker/Supabase/Neon MCP (no MCP client — deliberate), Google
Stitch enablement, CodeQL, Promptfoo, Browserbase, Life OS adapters (each rides the proven
13 pattern later as adapter+tests+flag).

### Phase 14 — AI Team Office (visual orchestration layer) — ~7 tasks, NO migration

**Goal:** optional office view over the Studio — rooms per team, member avatars with live
status (role, model, stage, cost, tools chips), runs visualized gather → work → meet →
review → verdict, activity-feed replay. **Render-only; never the default UI (R6); the calm
Studio remains.**

**Decisions made now (so Opus doesn't re-litigate):**
- Layout persists in `settings_json["office_layout"]` per project ⇒ **zero migration**; the
  ONLY new mutation route is `POST /api/projects/{id}/office-layout` (merge-safe like
  `set_label`). Route pin grows by exactly one.
- No WS event-schema bump: the office derives entirely from existing WS v2 events + the
  Activity feed + member_runs read models. If a field is genuinely missing, add it additively
  to a read model, never a new event type.
- DOM/CSS canvas (the Phase 11 vanilla-module pattern) — no game engine, no new dependency,
  CSP `default-src 'self'` untouched. All sprites/assets are Kairo's own; my-virtual-office
  (AGPL-3.0) is UX reference ONLY — zero code/asset reuse (R6).

**Tasks:**
1. Plan-of-record + office-state derivation module (pure functions: events + read models →
   room/desk/status model) with fixture-driven tests. No UI yet.
2. Office canvas + rooms/areas per TeamProfile (incl. Custom), department labels/colors,
   meeting area, status zones.
3. Avatars/status nodes + stage mapping (council = meeting table, synthesis = head office,
   execution = writer's desk with turn-lock indicator, review, verdict) + hand-off animations
   (CSS transitions; `prefers-reduced-motion` + the Phase 11 motion knob respected).
4. Run timeline + activity-feed replay panel; per-agent inspect linking to existing
   trace/Gate routes (navigation only).
5. Layout editor + the single layout-save route + serious/playful mode toggle (Studio stays
   default; office is opt-in per project).
6. Pins + polish: escaping tests over every agent-authored string (textContent-only, same as
   ADR-0017); no-new-authority pin (start/cancel/approve buttons hit EXISTING routes only —
   enumerated); performance on a long fixture run; screenshot DoD (office empty / live run /
   layout editor × 3 themes × 3 widths — via the 13 QA path or the capture harness).

   **🖊 SIGN-OFF I — visual + render-only review** (Checkpoint-F pattern, not an
   adapter-class stop): Habib approves the office direction + the render-only evidence.

7. ADR-0020, README, `docs/verification-14.md`.

**Deferred:** pathfinding/ambient wandering, pets/weather/day-night, voice presence.

### Phase 15 — Memory Graph + Obsidian projection — ~9 tasks, migration v12

**Goal:** typed, evidence-linked, project-scoped graph over memories/chats/KB; extraction as
reviewable proposals ONLY; Obsidian-compatible projection/export. **SQLite is canonical;
Obsidian is a projection — never a write-back path.**

**Tasks:**
1. Plan-of-record + migration v12: `graph_nodes` (typed: entity/topic/decision/person/tool/
   repo; project_id NOT NULL), `graph_edges` (typed, **evidence link to source rows
   mandatory — an edge without evidence is unrepresentable or rejected at the store layer**),
   `proposals` (S4 — generic shape with `source` so 16's dreaming reuses it verbatim).
2. Graph store + invariant pins: project-scoped queries only (cross-project graph is
   deliberately never); edge-without-evidence rejected; node/edge type closed sets.
3. **Extraction job** — batched, `utility` route (anthropic-pinned per 10C authority tiers:
   private conversation content never reaches a non-Anthropic provider), budgeted with a hard
   per-run cap, writes **proposals only** — nothing lands in the graph without review.
4. **Suggestion queue UI** — accept / reject / bulk-reject, evidence preview per proposal,
   Gate-like discipline; accept routes = new named mutation routes (pin grows explicitly).
5. Graph read models + Workspace Overview embed + full graph view (render follows the
   Phase 11 no-authority rules; agent/extracted text escaped).
6. Vault wikilink alignment + **Obsidian export**: per-project only, frontmatter + wikilinks,
   sensitivity-class-filtered, private-sourced rows EXCLUDED by default, output confined to a
   floor-checked export dir under `data/`; export runs as a previewed apply (S1 shape:
   manifest preview → approve → write files → journal row).
7. Golden-file export tests + project-boundary pins on graph queries and export + extraction-
   precision eval scenarios with planted entities (keyless cassettes).
8. Adversarial: planted hostile text in a memory cannot smuggle an auto-accepted edge
   (proposals only, pinned); export dir traversal refused; a private-class node never
   appears in an export manifest.

   **⛔ CHECKPOINT J — MANDATORY STOP before (a) the first extraction run over the real
   corpus and (b) enabling the export path.** See §6.

9. Live ritual + docs: §8; ADR-0021, README, `docs/verification-15.md`.

**Deferred:** embedding-similarity edges; semantic search (post-15 decision); graph-driven
retrieval into prompts (later, separately gated); cross-project graph (never).

### Phase 16 — Attention + Automation (Notification Center + Dreaming) — ~10 tasks, migration v13

**Goal:** one attention system (approvals, reviews, proposals, alerts; priority; routing) +
proposal-only automation (morning briefing, nightly review, bottleneck analysis, ROI
summaries, self-improvement proposals) under UnattendedGate with hard budget caps.

**Tasks:**
1. Plan-of-record + migration v13: `attention_items` (kind approval/review/proposal/alert;
   priority; project; source; state; timestamps) — **absorbing** S3's pending-decision model
   and S4's proposal queue as sources, evolving the one-attention-surface pin (the Gate list
   becomes a view over attention_items; never two competing surfaces).
2. Attention store + lifecycle state machine + read models; migration of existing pending
   Gate items into the new model (additive, reversible).
3. **Notification Center screen** — replaces the Gate list surface (same routes underneath;
   approve/reject still hit the EXISTING gated routes; the center adds routing + priority,
   not authority).
4. **Routing rules** — urgent ⇒ Telegram/Kakao push with minimized bodies (titles + counts,
   per the digest-minimization contract); everything else ⇒ digest. Rules are config-shaped
   data + a routing matrix test, not code paths per rule.
5. **Dreaming runner** — jobs under UnattendedGate: nightly review, morning briefing,
   bottleneck detection over ledger/tasks, ROI/time-saved summaries, self-improvement
   proposals. Tool scope = read-only + Kairo-internal proposal/artifact writes ONLY —
   **no connector writes, no shell, no egress, no sends; it can never execute, schedule a
   risky action, or delete**. Hard per-night budget cap; cap-hit halts the run and emits an
   alert attention item. Outputs = artifacts (briefings land in the Library + Daily) and
   proposals (land in the center).
6. Briefing/review content builders (ledger/tasks/journal/graph read models in, artifact +
   attention items out) — golden-file tests on fixtures.
7. Chunking discipline: dreaming jobs respect the ~14-minute background ceiling — each job is
   a chunk; an orchestrating schedule, not one long run.
8. **Adversarial evals** — the dreaming scope pin (no egress/write/send tool REACHABLE — not
   just unused — from a dreaming context; adversarial prompt tries each); injected content in
   reviewed material cannot escalate a proposal into an action (a proposal's accept path is
   always a human on an existing gated route); budget cap halts; routing matrix (urgent
   private content never leaves as more than title+count); cross-project attention isolation.
9. UI/UX pass: priority discipline (defaults bias to digest — notification fatigue is a
   product failure), quiet hours, per-project mute.

   **⛔ CHECKPOINT K — MANDATORY STOP before any dreaming job is scheduled unattended.**
   See §6. After approval: schedule, then a **week-long live observation window** (§8) with
   no other live changes in flight.

10. ADR-0022, README, `docs/verification-16.md`; ratchet in a dedicated commit if green.

**Deferred:** closed-loop self-improvement (auto-applying proposals) — indefinitely, until a
dedicated consent-framed phase; workflow auto-tuning; two-way Telegram/Kakao chat + remote
approval (candidate Phase 18 — voice-consent-class design, NOT folded in here).

## 6. Mandatory checkpoints (all are full stops: report evidence, wait for Habib)

| ⛔ | Phase | When | Evidence Habib must see before "continue" |
|---|---|---|---|
| **G** | 12 | After Task 9, before ANY live connector write | Per-bullet with named tests: (i) every write verb requires an approved intent — no code path from model output to a remote write without a stored, human-approved intent; (ii) preview faithfulness — executed payload == previewed payload (forged/edited preview inert); (iii) AUTO_NEVER covers connector writes; Plan denies; UnattendedGate refuses execution; VoiceApprover path unchanged; (iv) scope pin — OAuth request list == implemented verbs exactly; (v) idempotency — replayed execute cannot double-write; (vi) journal metadata-only sweep; (vii) attendee ambiguity ⇒ ASK before intent creation; (viii) injected-content eval: hostile body ⇒ ASK with faithful preview, never silent; (ix) suite + ruff + core replay gate green. |
| **H** | 13 | After Task 7, before egress flags flip | (i) availability fail-closed matrix per service (flag/key/pricing); (ii) B1 refusal: private-provenance bundle blocked to public_only roles (engine-level test); (iii) B2 framing on every adapter output incl. snippet reuse; (iv) taint: private-read → egress demoted ASK; (v) reservation prices every metered op; per-service cap halts mid-run; (vi) settings secret sweep (presence-booleans only); (vii) hostile-page cassette scenarios inert; (viii) suite green. |
| **I** | 14 | After Task 6 (sign-off, Checkpoint-F pattern) | Screenshot DoD pack (office empty/live/editor × 3 themes × 3 widths, 0 overlap violations); render-only pin evidence (mutation set grew by exactly office-layout-save; every action button enumerated onto existing routes); escaping tests; AGPL-cleanliness statement (no copied code/assets). |
| **J** | 15 | After Task 8, before real-corpus extraction + export enablement | (i) proposals-only pin — no write path into graph tables except the accept route; (ii) edge-without-evidence unrepresentable; (iii) project-boundary pins on queries + export; (iv) export excludes private-classed rows by default + floor-checked dir + traversal refused; (v) extraction budget cap halts; (vi) utility-route authority pin (anthropic-only for private content); (vii) planted-entity precision eval results; (viii) suite green. |
| **K** | 16 | After Task 9, before scheduling any unattended job | (i) dreaming scope pin — adversarial proof no egress/write/send/schedule/delete tool is reachable; (ii) proposal-escalation eval — injected reviewed content cannot turn a proposal into an action; (iii) budget-cap halt + alert; (iv) routing matrix — urgent pushes are titles+counts only; (v) one-attention-surface evidence (Gate list absorbed, not duplicated); (vi) attention lifecycle + cross-project isolation; (vii) the week-long observation plan agreed (what's watched, what aborts it); (viii) suite green. |

Standing rule between checkpoints: no phase starts on an unrun or red chunked eval gate from
the previous phase's ritual.

## 7. Non-negotiable safety invariants (arc-wide, all pinned by tests)

1. **Nothing in 12–16 weakens** PermissionGate, VoiceApprover, UnattendedGate, taint/egress
   demotion, project scoping in SQL, the service catalog fail-closed availability,
   model-authority tiers (planner/judge/utility = anthropic, no escape hatch), or the eval
   replay/cost controls. Modes compose at the documented seams only.
2. **UI adds no new authority** — ADR-0017 extends over every new screen (14's office, 13's
   settings, 16's center): reads/navigation immediate; every write hits a named, pinned
   mutation route; the closed route set grows only by enumerated additions per phase.
3. **Every outward write is two-phase**: stored intent → faithful preview/diff → human
   approval → execute → journal. No preview, no write. Executed payload == previewed payload.
4. **Gmail send does not exist** — no method, tool, route, or UI action; the grep/test pin is
   re-asserted in every phase that touches connectors.
5. **AUTO_NEVER ∪ PLAN-denied ∪ unattended-refused covers**: connector writes (12), egress
   service calls (13), graph accepts (15), and everything dreaming cannot do (16). Auto mode
   never approves an outward write; unattended contexts can propose only.
6. **B1/B2 discipline on all external egress**: private/tainted provenance never reaches a
   `public_only` service or a `private_ok=False` provider; every non-local-scan output is
   framed untrusted before any model sees it — including when re-quoted in snippets,
   proposals, briefings, or office summaries.
7. **Memory permanence is review-first**: nothing enters the graph, the vault (beyond
   existing gated writes), or an export without an explicit human accept; every graph edge
   carries evidence; exports are per-project, class-filtered, floor-checked.
8. **Ledgers and journals are metadata-only** — never bodies, prompts, secrets, or matched
   values; the secret-absence sweep extends over every new GET surface each phase
   (settings, journal, attention, graph, office read models).
9. **Ambiguity stops the machine**: unresolved attendee/contact ⇒ ASK before an intent
   exists; unpriced metered op ⇒ blocked; missing credential ⇒ tool unregistered; unknown
   route ⇒ 403/absent. Fail closed, visibly, with a reason — never a downgrade.
10. **One attention surface** — 16 evolves the Gate list into the center; at no point do two
    competing approval surfaces exist.
11. **Scopes are exactly what code implements** — OAuth request lists pinned; `drive.file`
    never silently becomes `drive`; re-consent is explicit and user-performed.
12. **Forbidden files stay untouched and uncommitted**: `docs/PLAN.md`,
    `docs/PLAN-7-voice-consent-checkpoint.md`, `mcp_sample.json`, `config/settings.yaml`,
    `config/permissions.yaml`, `design/`. `data/screenshots/` stays gitignored.
13. **Context caching never weakens data-flow safety** (§4A): it never bypasses `context_policy`,
    private-data routing, taint/egress, project scoping, or retention. DEFAULT caches only the
    stable, NON-sensitive prefix. Private/project content is cacheable ONLY when all hold —
    provider allowed for private context (`private_ok`), route permits, `cache_private_allowed`
    true, TTL/storage documented, and the audit ledger records it. Cache is NOT memory (no
    persistent store, no retrieval path) and is never prewarmed with private connector data by
    default.

## 8. Live-verification rituals (exact; all capped; all on Habib's machine)

Common tail for every phase:
```bash
uv run kira eval gate --profile live-chunked --live --max-cost-usd <cap>   # chunked; ~14-min rule
# green + intended ⇒ baseline ratchet in a DEDICATED commit; otherwise no ratchet
```

- **12 (after Checkpoint G):** reconnect OAuth with the new scopes (consent screen shows
  exactly calendar.events + drive.file added). On a real account: canary event create (with 2
  attendees incl. one deliberately ambiguous name earlier in the turn ⇒ verify the ASK) →
  update (verify field-level diff + timezone + `sendUpdates` in the preview) → Meet link
  attach → cancel → **undo** the cancel; Doc create + batchUpdate edit; verify journal rows +
  egress-log rows + artifacts registered; verify a draft still cannot be sent from anywhere.
- **13 (after Checkpoint H):** flip flags one service at a time. Keyed research workflow run
  per service; Costs screen attribution == SQL sums; **hostile-page live test** (a controlled
  page with planted instructions ⇒ inert, framed); **canary proof** that a private-provenance
  bundle is refused to a public_only service; per-service cap halt demo; image-gen output
  lands as a Library artifact.
- **14 (after Sign-off I):** watch ≥2 real orchestrations in the office (one multi-team);
  layout editor roundtrip; long-run performance; confirm every button lands on an existing
  route (network tab).
- **15 (after Checkpoint J):** budgeted extraction over the real corpus; review a real batch
  (accept some, bulk-reject some); open the export **in real Obsidian**; verify no
  private-classed content in the export tree; nightly extraction cost check.
- **16 (after Checkpoint K):** schedule morning briefing + nightly review; then a
  **full week** of observation: routing correctness (urgent vs digest), briefing quality,
  cost per night vs cap, zero unexpected writes in journal/egress logs, no notification
  fatigue. Any silent action ⇒ abort, unschedule, report. Only after the week: closeout +
  ratchet.

## 9. Fable ↔ Opus boundaries

**Fable (planning, checkpoints, sign-offs — no implementation):**
- This roadmap; then ONE lean plan-of-record per phase at kickoff IF the phase needs
  decisions beyond §5 (12 and 16 likely yes — safety-heavy; 13 and 14 can run directly off
  §5; 15 borderline, decide at kickoff). Do not re-plan what §5 already decides.
- Reviewing Checkpoint G/H/J/K evidence and Sign-off I with Habib; adjudicating any
  mid-phase deviation that touches an invariant in §7.
- The MCP-client architecture decision (deferred, ~13–15 window) — a Fable decision doc
  before any MCP-kind service is enabled.

**Opus 4.8 (implementation):**
- Every task in §5, in order, per-task commits, adversarial self/subagent review before each
  commit, checkpoint stops honored as full stops.
- Writing the per-phase ADRs/verification docs (Fable reviews at the checkpoint).
- Recording new eval cassettes once per new scenario (keyless replay thereafter).

**Habib (human-only):** OAuth consent/reconnect; flag flips after checkpoints; live rituals
(§8); baseline-ratchet approval; the week-long 16 observation.

## 10. Cost discipline (default $0)

- Every task verifies with `uv run pytest` + `ruff` + `uv run kira eval gate --suite core`
  (replay, keyless, $0). NEVER bare `gate` in CI paths (defaults to `--suite all` ⇒
  intentional `CassetteMissError` on adversarial).
- New eval scenarios: record cassettes ONCE (`--record`, capped), replay forever. Fake
  transports for every connector/service adapter — live HTTP only in §8 rituals.
- Live runs are always explicit (`--live`) and capped (`--max-cost-usd`); chunked per-suite
  (~14-min background ceiling), aggregate once, judge new scenarios separately.
- Extraction (15) and dreaming (16) carry hard per-run/per-night caps in config from day one;
  the cap-halt path is itself tested keyless.
- Quality-first stands: never economize on models/judges/retries — economize on NOT re-paying
  for what replay already proves.

## 11. Architecture map — everything lands on Phase 11 surfaces

```
12 connector writes ──▶ journal rows ──▶ Activity feed + Workspace; outputs ──▶ Artifacts Library
                   └──▶ pending intents ──▶ Gate queue (12) ──▶ Notification Center (16)
13 research/service outputs ──▶ artifacts (report/design) ──▶ Library + Daily; costs ──▶ Cost Center
13 settings panels ──▶ existing Settings screen (S5)
14 office ──▶ pure view over WS v2 + Activity feed + member_runs (renders 12/13 liveliness)
15 extraction ──▶ proposals ──▶ suggestion queue ──▶ accepted graph ──▶ Workspace Overview embed
             └──▶ Obsidian export (previewed apply) ──▶ journal row ──▶ Activity feed
16 dreaming ──▶ briefings/reviews as ARTIFACTS (Library + Daily card) + PROPOSALS (center)
16 attention_items ◀── sources: 12 intents, 13 asks, 15 proposals, alerts ──▶ routed: push (minimized) / digest
Search (FTS5) indexes it all as it lands (journal/attention/graph metadata ride existing index plumbing).
```

## 12. Shared modules / tables / APIs (likely full list)

- `src/kira/actions/` — `intents.py` (S1), `journal.py` (S2), preview builders (12).
- `src/kira/graph/` — store, extraction job, proposal store (S4) (15).
- `src/kira/attention/` — store, routing, dreaming runner (16).
- Context Reuse (S7, §4A): capability metadata on `ProviderSpec` / ModelRegistry; a client-layer
  `ContextReusePolicy` + per-provider adapters; the stable-first prompt assembler + prefix hashes;
  normalized cache columns on `model_calls`. Cross-cutting — touches the client / registry layer,
  not the actions layer.
- Migrations: **v10** (write_intents, connector_writes) → **v11** (Context-Reuse normalized cache
  columns on `model_calls`) → **v12** (graph_nodes, graph_edges, proposals) → **v13**
  (attention_items). 13 and 14: none. (Final numbers follow build order; this assumes S7 lands
  before Phase 15, as recommended.)
- Routes (each phase enumerates exactly): 12 intent lifecycle + journal GETs; 13 settings
  writes; 14 office-layout save (one); 15 proposal accept/reject + graph/export GETs;
  16 attention lifecycle + routing-rule writes.
- Reused as-is (never forked): PermissionGate/UnattendedGate, taint/egress, ServiceRegistry +
  catalog, LedgeredClient + budgets, ArtifactStore registration, Activity feed, WS v2,
  digest/notification minimization, the Phase 11 UI module pattern (`el`/esc, themes, pins).

## 13. Tests to write once, reuse across all five phases

1. **Route-pin growth helper** — asserts the closed mutation set == previous set ∪ the
   phase's enumerated additions (already the pattern; extract the helper in 12 Task 1).
2. **Secret-absence sweep auto-extension** — new GET read models registered into the existing
   sweep (journal, settings, attention, graph, office) rather than per-phase copies.
3. **Two-phase write harness** — parametrized: draft→preview→approve→execute→journal happy
   path + forged-preview inert + idempotent replay; instantiated for every 12 verb and 15's
   export apply.
4. **Fake Google transport** — one recording/replay fake for calendar/drive/docs verbs with
   scripted failures (12; reused by any future connector).
5. **Availability fail-closed matrix** — parametrized over (flag, key, pricing) per service
   (13; exists in spirit from 10B — extend, don't duplicate).
6. **Untrusted-framing assertion helper** — "this string reached a prompt ⇒ it was framed";
   applied to 13 adapters, 15 proposals, 16 briefings, 14 summaries.
7. **Provenance canary fixture** — planted private-class canary + assertion it never appears
   in: search results cross-project (11, exists), public_only bundles (13), exports (15),
   pushes (16), office read models (14).
8. **Proposal state-machine suite** (S4) — written in 15, parametrized by source so 16's
   dreaming proposals run the same suite.
9. **Budget-cap halt test** — parametrized runner-with-cap (13 services, 15 extraction,
   16 dreaming).
10. **Screenshot DoD harness** — exists (`tests/ui/capture.py`); each UI-bearing phase adds
    screens to the same pack.
11. **Stable-prefix-hash determinism** (S7) — identical stable inputs ⇒ identical
    `system_contract_hash` / `tool_schema_hash` / `team_profile_hash` / `service_catalog_hash` /
    `project_policy_hash`; any change to that content busts the composite `stable_prefix_hash`.
    Pure, golden.
12. **Cache-ledger normalization** (S7) — each provider's usage fields map onto the same
    normalized columns; a provider that does not report a field ⇒ NULL, never a fabricated 0;
    `estimated_cache_savings_usd` is derived, and the row still carries NO prompt text.

## 14. Do NOT build yet (standing list; each needs its own decision/phase)

- **MCP client layer** — deliberate architecture decision (~13–15), Fable doc first; gates
  Stitch/Figma/GitHub/Docker/Supabase/Neon.
- **Gmail send** — indefinitely; not even behind a flag.
- **Closed-loop self-improvement** (auto-applying proposals) — indefinitely.
- **Two-way Telegram/Kakao chat + remote approval** — candidate Phase 18,
  voice-consent-class design.
- **Cross-project memory graph** — never.
- **Semantic/embedding search** — decide after 15.
- **Elevated/full Drive scope; Sheets/Slides; contacts** — own future phase.
- **Generic browser / Playwright interaction verbs** — separately planned + gated.
- **Life OS adapters** (Paperless-ngx, Actual, Mealie, Linkwarden, n8n) — post-13 deferred
  track, one adapter+flag at a time.
- **Phase 17 items** (daemon/tray, full restore/migrate, onboarding, retention) — noted
  dependencies only; nothing in 12–16 pre-builds them beyond keeping v10–v13 additive.

## 15. Commit / checkpoint structure (anti-huge-commit rules)

- One commit per §5 task, explicit paths, message pattern
  `Phase <N> Task <k>: <what> — <key invariant/pin>`, ending with the Opus co-author line.
- Migrations land ALONE with their store + tests (v10/v11/v12 commits contain no UI).
- Adapters land one-per-commit with their fakes; flags stay off in the same commit.
- UI screens land after their read models (separate commits).
- Checkpoint stops are commits-then-stop: everything keyless is committed BEFORE the stop;
  the flag-flip/live work after approval is its own small commit.
- Baseline ratchets: always a dedicated commit with the gate output quoted.
- Docs (ADR + verification + README) close each phase as the final task's commit.
- Nothing is committed red; adversarial review precedes every commit; forbidden files never
  staged (re-check `git status` before every commit — the T11 race rule: stage by explicit
  path, never `git add -A`).

## 16. Opus 4.8 handoff prompt — Phase 12, Milestone 1

> You are implementing **Phase 12 (Action Connectors), Milestone 1 = Tasks 1–6** of
> `docs/ROADMAP-12-16-execution.md` §5 — the keyless substrate: migration v10 + WriteIntent
> state machine (Task 1, which also commits the roadmap doc as the plan of record),
> preview/diff builder (Task 2), attendee-ambiguity resolution (Task 3), calendar write
> adapter with fake transport (Task 4), Drive/Docs write adapter under `drive.file` (Task 5),
> Gmail draft improvements with the no-send pin re-asserted (Task 6). Milestone 2 (Tasks 7–9
> + Checkpoint G) follows in the same phase; do NOT enable any live write, flip any flag, or
> request any OAuth scope change in Milestone 1 — everything is keyless, fake-transport, and
> inert.
>
> Baseline: Phase 11 complete (1522 passed / 2 skipped, ruff clean, migrations v9, route pin
> current). Read `docs/ROADMAP-12-16-execution.md` fully — §5 Phase 12, §6 Checkpoint G, §7
> invariants, §13 shared tests, §15 commit rules are binding. Also read
> `docs/phase-11-implementation-playbook.md` (working discipline),
> `src/kira/permissions/` (Gate/UnattendedGate seams), the Phase 9 connector adapters
> (transport + scope patterns), and ADR-0015/0017.
>
> Binding for M1: WriteIntent fields include the Phase-16-ready columns (priority, source,
> project_id, state timestamps) per §4 S1/S3; idempotency keys from day one; preview shows
> timezone/attendees/recurrence/Meet/`sendUpdates` (R5) and field-level diffs for updates;
> an ambiguous attendee can never reach the preview stage; connector write tools are ASK +
> AUTO_NEVER + outside PLAN_SAFE + refused by UnattendedGate (wired in M2 Task 7, but nothing
> in M1 may assume otherwise); journal is metadata-only; Gmail send stays nonexistent
> (re-assert the grep/test pin in Task 6's commit).
>
> Discipline: per-task commits with explicit paths (never `git add -A`) ending
> `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`; every commit green
> (`uv run pytest`, `uv run ruff check .`, `uv run kira eval gate --suite core` — replay,
> $0; never bare `gate`); adversarial review before each commit; extract the §13 shared test
> helpers (route-pin growth, two-phase harness, fake Google transport) as you first need
> them; never touch or commit `docs/PLAN.md`, `docs/PLAN-7-voice-consent-checkpoint.md`,
> `mcp_sample.json`, `config/settings.yaml`, `config/permissions.yaml`, `design/`. Stop after
> Task 6 and report; Milestone 2 ends at ⛔ Checkpoint G, which is a FULL STOP before any
> live write.
