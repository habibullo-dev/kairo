# Kairo Phase 11 — Workstation UI/UX + Product Surface

*(Planned 2026-07-08 by Fable from seven read-only analysis passes (product IA, UX reference,
frontend, backend/search, safety, QA, cost). To be committed as `docs/PLAN-11-workstation.md`
in Task 1. Implements the roadmap's Phase 11 with approved amendments R1–R4 and constraints
C1–C11 (docs/ROADMAP-post-10B.md), reshaped around the Workstation UI/UX priority.
NEVER commit or modify: `docs/PLAN.md`, `docs/PLAN-7-voice-consent-checkpoint.md`,
`mcp_sample.json`, `config/settings.yaml`, `config/permissions.yaml`, `design/` —
design/ is READ-ONLY visual reference.)*

## 0. Context + repo baseline

Baseline (rev `ca8a9a8`): suite **1385 passed / 1 skipped**, ruff clean; migrations **v8**;
mutation-route closed set **25** (pinned in
`test_ui_readmodels.py::test_mutation_route_closed_set`); `EVENT_SCHEMA_VERSION = 2`; eval
replay fully **keyless/$0** (97 committed core cassettes; proven with invalid keys 19/19);
Phase 10B live-verified; Phase 10C providers shipped (flag-gated off); Google Stitch cataloged
(`google_stitch`, deferred, `GOOGLE_STITCH_API_KEY` presence-only). FTS5 confirmed in the
SQLite build (3.53.1).

Kairo's backend is strong; the product surface is the weakness: Projects is a bare switcher,
chats have full server support but **no screen**, there is no artifacts store, no global
search, one hardcoded theme, and Daily is good but not yet a command center. Phase 11 makes
the workstation feel premium, project-first, and searchable — **adding zero new authority**.

## 1. Subagent findings (condensed; load-bearing facts)

- **IA (Product Architect):** current nav = Daily/Projects/Studio/Gate/Vault/Tasks/Memory/
  Meetings/Hub/Costs/Trace/Lab. `/api/sessions` already supports list/search/pin/resume —
  a Chats surface is low-effort. Tasks/Memory/Costs/Studio read models already accept
  `project_id` ⇒ embeddable as workspace tabs. Gate's amber modal (nonce + heartbeat) is the
  app-wide approval surface and must stay globally reachable; Trace stays reachable
  (auditability).
- **UX (V2 reference):** the prototype ships a full token system — 3 themes
  (`body[data-theme=light|noir|neon]`) redefining `--canvas/--ink/--muted/--panel/--accent/
  --attention/--cost/--veil-*`, plus knobs `--nav:292px`, `--rail`, `--gap:16px`,
  `--radius:8px`, `--bg-intensity:.28`, `--motion:180ms`, `data-density=compact`,
  `data-layout=expanded`, `reduce-motion`. Components: surface/panel, project/stat/agent
  cards, chips/status-pills, segmented control, list rows (+`.attention`), stage timeline,
  command palette with saved-view cards, gate modal with risk banner, empty-state block.
  Cautions: NO Inter font (system stack only — no external resources), backgrounds via CSS
  gradient veils (the reference PNGs are 1.5–1.9 MB and design/assets is not committable),
  `backdrop-filter` sparingly (rail/modal only), `color-mix` needs rgba fallback.
- **Frontend:** UI is small (~1,760 lines). Screen contract: `render(container, api)` +
  optional `onEvent`; fetch-on-enter + WS `refreshIfActive`. Risks: app.js god-file tendency;
  `esc()` duplicated 8× and **not quote-safe** (attribute-injection smell — real instances in
  projects.js/studio.js); hardcoded hex in Studio CSS; no central keyboard handling. Plan: a
  leaf `static/ui/` layer (dom.js with `esc`/`escAttr`, components.js, theme.js, format.js,
  bus.js, keys.js), thin screens, one `workspace.js` orchestrating tab panels, strict import
  DAG (ui/ ← screens/ ← app.js), progressive migration screen-by-screen.
- **Backend/Search:** **chat content lives in SQLite** (`messages.content` JSON; saved via
  delete+reinsert each turn) — FTS5 works. Wiki + meeting-note text is FTS-able via
  `kb_chunks.text` (files on disk are mirrored into chunks). Digests are stored (summary +
  minimized sections). Six FTS domains: messages, memories, kb_chunks, tasks,
  orchestration_runs (title+synthesis_summary), digests; artifacts becomes the seventh once
  its table exists. `projects` has NO pinned/label columns — add `pinned` (ADD COLUMN),
  labels ride `settings_json`. Artifact producers + hook sites identified (digest store,
  orchestration writer, eval report, `knowledge/service.py::write_page`, meeting capture).
- **Safety:** the 25 mutation routes enumerated; GET secret sweep **auto-discovers** new
  non-parameterized GETs (canaries: all keys + OAuth tokens + launch token + session id) —
  parameterized detail routes are NOT swept (must be covered manually). Auth: per-launch
  token → httponly cookie, loopback-only bind refused at config load, strict CSP
  `default-src 'self'`, Origin check on mutations. Theme/density MUST be localStorage-only
  (a server route would be new authority). `test_no_external_resources_in_any_asset` exists.
  Eval chip is copy-command only (ADR-0005) — keep.
- **QA:** Playwright real driver is unwired (`set_driver` is test-only). Wiring spec: new
  `browser` optional extra (`playwright>=1.4`), a `PlaywrightInspectDriver` implementing the
  one-method `inspect(verb,url,selector)` protocol (guards untouched), `set_driver` called in
  `run_ui` behind a degrading try/import. Screenshot harness must exchange the launch
  `?token=` for the session cookie. Shots → gitignored `data/screenshots/`,
  `{screen}__{state}__{theme}-{width}w.png`. Cheap no-overlap check:
  `scrollWidth <= innerWidth` + bounding-rect scan per viewport (1440/1024/390). Eval
  scenarios: **none change**; replay gate stays green keyless; live judged gate = closeout.
- **Cost:** ledger rows carry project_id/team/stage/provider/purpose (both tables) — "by
  project/provider/stage" are grouping-allowlist additions (pure read models).
  **"by mode" is NOT derivable** (no `mode` column; `purpose` is the honest proxy —
  scoped OUT, below). Daily "cost today" reuses `state.runner.today_spend_usd` (already
  polled) — zero new queries. Budget-warning status + ROI aggregate = small pure read
  models. Keep fetch-on-enter + WS invalidation; NO new polling intervals; LIMIT everything;
  debounce search ~250 ms; always render `unpriced` separately (never $0).

## 2. Product principles

1. **Project-first.** Projects are the organizing surface; the per-project Workspace is where
   work lives. Global surfaces (Daily, Studio, Costs) aggregate across projects.
2. **Calm, premium, one attention surface.** Amber = decisions only (approvals/review);
   cost = teal monitoring, visible but not stressful; quiet states are deliberate and
   informative. One primary attention block per screen, ordered by priority.
3. **Nothing empty.** Every screen has a designed empty state that teaches the next action.
4. **No new authority.** The UI reads and navigates; every write/generate/mutation goes
   through the existing Gate/turn routes. The palette navigates — it never posts.
5. **Tokens, not styles.** All appearance (theme/density/accent/bg-intensity/motion/layout)
   is CSS-custom-property + `data-*`/localStorage driven; no per-screen hex.
6. **Honest data.** Unpriced spend shown as unpriced; eval freshness as copy-command;
   provider presence as booleans; nothing fabricated.

## 3. Information architecture (target)

- **Primary rail:** Daily · Projects · Studio · Costs · Settings — plus the global search
  palette (Ctrl/Cmd-K) and the persistent Gate badge.
- **Project Workspace** (route `#workspace/{id}`, from a Projects card): tabs
  Overview · Chats · Artifacts · Memory · Tasks · Vault · Studio · Costs · Activity.
- **Utility area (below divider):** Gate · Trace · Hub · Lab · Meetings. Gate/Trace stay
  globally reachable (load-bearing approval + auditability); Hub becomes
  connections-status; Lab stays view-only eval history.
- **Deliberate deviation from the V2 prototype:** no third right-rail column in Phase 11 —
  appearance knobs live in Settings plus a compact theme toggle in the status bar (less
  layout risk; the rail can return with Phase 14's office view). The prototype's Office
  screen is explicitly **out** (Phase 14).

## 4. Screen-by-screen scope

**Daily — command center.** Priority order (top→down): pending approvals (amber, links to
Gate) → Now (active turn/run) → briefing/digest → today's tasks → recent artifacts (new) →
latest orchestration run (status+cost, links to Studio) → cost today (reuse runner poll) →
notices → connector health (presence booleans from hub read model). Conversation + composer
stay. Calm defaults; each card has an empty state.

**Projects — grid.** Cards: icon/color, name, label chip (one of Coding, Creativity,
Business, Personal, Learning, Finance — user-editable set stored per project), status,
pinned star, health chips (open tasks · sessions this week · last run verdict · month
spend), archived section collapsed. Saved views / smart collections row (R1): built-ins
"Recent artifacts", "Needs review", "Generated this week", "By team/model", "Pinned project
work" + user-defined saved views. Pin/label/archive are the only writes (existing +
minimal new routes, §5).

**Project Workspace.** `screens/workspace.js` + `screens/workspace/*.js` panels sharing one
fetched project context: Overview (description, health, pinned artifacts, recent activity,
quick actions that NAVIGATE); Chats (sessions list for the project, search, pin/resume via
existing routes; transcript view links to runs via session_id joins); Artifacts (scoped
library); Memory, Tasks (existing read models with `project_id`); Vault (scoped overview —
new read-model filter); Studio (runs for the project + launch via existing Studio flow);
Costs (`costs_overview(project_id)`); Activity (derived, metadata-only feed — see §5;
designed as the replayable substrate for Phase 14's office view).

**Chats.** Lives as the Workspace tab + palette/global search results + a "recent chats"
Daily card. Message editing/regenerate/branching = **out** (11.5).

**Artifacts Library.** Global screen + workspace tab. Left: filterable list (kind, project,
label, pinned, date, team/model); right: preview panel (markdown/text render via
textContent-based renderer, image preview, metadata block: project, team/role/model, date,
cost origin, sensitivity, provenance, content_hash, origin link). Pin/label writes only.
File content served ONLY through a confined route (§5 — artifacts root confinement +
sensitive-path refusal).

**Global Search (palette).** Ctrl/Cmd-K overlay: type-ahead federated FTS5 across projects,
chats, artifacts, memory, tasks, vault/KB, runs, digests; filters (project/type/date/
team/model/sensitivity); saved searches = saved views; navigation actions ("Go to Studio",
"Open project X"). **Permission-aware, hard rule:** the palette performs GETs and
navigation ONLY — zero direct POSTs; "write" entries navigate to the surface that owns the
write (pinned by test — stronger than roadmap C11).

**Studio polish.** Shared components for roster cards (member: role, model+provider chip,
tools/services chips, status pill, cost); the run timeline (council → synthesis → execution
→ review → verdict) as the standard timeline component; **the head reviewer (Fable/Opus)
visibly badged** on synthesis/verdict; run history/detail kept; providers panel kept.
Office-style view: later phase, not default, not built now.

**Cost Center.** Periods (today/week/month) × dimensions (project, team, model, provider,
service, stage, purpose); budget-warning banner (ok/soft/hard vs `project_monthly_usd`,
`confirm_above_usd` surfaced); ROI/time-saved aggregate + per-run list; unpriced always
distinct. Secondary breakdowns lazy-load on tab expand. "By mode" **excluded** (no ledger
column; `purpose` is the documented proxy). Provider balance/usage APIs **deferred**
(future optional); per-provider spend from the ledger is shown instead.

**Settings.** Appearance: theme (light/noir/neon), density, accent, background intensity,
motion (incl. reduce-motion), layout (focused/expanded) — ALL localStorage/client-side.
Status sections (read-only, presence-booleans + copy-commands): providers/models routes,
services catalog states, budgets, connectors, privacy/safety summary (mode, unattended
posture, egress notes). Debug/trace toggle lives here, default OFF, presentation-only.

**Google Stitch (future service, not a blocker).** Already cataloged (deferred, egress,
`project_non_private`, `untrusted_model_generated`, `GOOGLE_STITCH_API_KEY` presence-only).
Phase 11 touchpoints only: artifacts support `kind="design"` and `origin_type="google_stitch"`
so future Stitch outputs land as artifacts/design references (never executed/committed code);
the services panel keeps showing it deferred. The adapter waits for the MCP-client layer.

## 5. Data / API / read models

**Migration v9 (one migration, plain additive SQL):**
1. `artifacts` table (R2): `id, project_id→projects (nullable), kind, title, local_path,
   external_uri, CHECK((local_path IS NULL) <> (external_uri IS NULL)), content_hash
   UNIQUE, origin_type, origin_id, created_by CHECK IN ('user','agent','system'), team,
   role, model, sensitivity, provenance_class, labels_json, pinned INTEGER DEFAULT 0,
   created_at` + indexes (project_id), (pinned). Dedupe: re-register with the same hash
   returns the existing row.
2. `ALTER TABLE projects ADD COLUMN pinned INTEGER NOT NULL DEFAULT 0` (labels ride
   `settings_json["label"]`; archive uses existing status/archived_at).
3. `saved_views(id, name, scope, query_json, project_id, created_by, created_at)`.
4. FTS5 **external-content** tables + insert/update/delete triggers + in-migration backfill
   for: messages (join sessions for project scope), memories, kb_chunks (join kb_sources for
   scope; wiki chunks global), tasks (title+payload), orchestration_runs
   (title+synthesis_summary), digests (summary), artifacts (title+labels). A
   `rebuild`-style maintenance command re-syncs indexes idempotently.
   (messages caveat: save_messages bulk delete+reinserts — triggers stay correct, just busy.)

**Search service (`src/kira/search/`):** federated query over the FTS tables returning
`{domain, id, project_id, title, snippet(), provenance/sensitivity class, ts}`; **scoping in
SQL** — project filter applied via each domain's project_id (or join); snippets only, never
full bodies; results for model-facing reuse (none in Phase 11) would be framed untrusted.
Adversarial canary tests: planted content in project A never returned for project B.

**Artifact producers (hooks):** digest completion, orchestration run completion (summary
artifact), eval gate report, `write_page` (wiki), meeting capture — each registers via
`ArtifactStore.register(...)` with provenance; `local_path` must resolve under
`data/artifacts/` **or** an existing managed root (wiki/eval paths recorded as the managed
file's path with kind-specific confinement), never a sensitive path (`is_sensitive_path`
check at registration). No retro-backfill of historical outputs (documented).

**New GET routes** (auto-covered by the secret sweep; parameterized ones get manual sweep
tests): `/api/search`, `/api/artifacts`, `/api/artifacts/{id}`,
`/api/artifacts/{id}/content` (STRICT: only registered ids; resolve + confine to the
artifact's managed root; refuse sensitive paths; text/image only, size-capped),
`/api/workspace/{project_id}` (aggregate: overview+health+activity),
`/api/views` (saved views list). Extended: `costs_overview` (periods × new dimensions +
warnings + ROI aggregate — allowlist gains `project_id`, `provider`, `stage`), `/api/daily`
(recent artifacts, latest run), vault overview project filter.

**New mutation routes — closed set grows 25 → 30, each project/session-metadata-class
authority (mirrors `/api/sessions/{id}/pin`):** `POST /api/projects/{id}/pin`,
`POST /api/artifacts/{id}/pin`, `POST /api/artifacts/{id}/label`, `POST /api/views/save`,
`POST /api/views/{id}/delete`. Project `label` folds into the existing
`/api/projects/{id}/update` (writes `settings_json["label"]`). NO theme/appearance routes
(localStorage only). NO eval-run route (pinned). NO new WS event kinds;
`EVENT_SCHEMA_VERSION` stays 2 (artifact/search refresh via fetch-on-enter + existing
events).

## 6. Frontend architecture

New leaf layer `src/kira/ui/static/ui/` (ES modules, no build step, import DAG:
`ui/* ← screens/* ← app.js`):
- `dom.js` — canonical `esc()` + **`escAttr()`** (quote-safe) + `el()` builder; delete the 8
  duplicated `esc` copies; fix the attribute-interpolation sites (projects.js color,
  studio.js title/option).
- `components.js` — card, statCard, chip, statusPill, tabBar, table, listRow, emptyState,
  timeline, previewPanel.
- `theme.js` — applies `data-theme/data-density/data-layout` + `--accent/--bg-intensity/
  reduce-motion` from localStorage; exports the knob API for Settings + status-bar toggle.
- `format.js` — money/time/bytes (dedupe inline closures).
- `bus.js` — WS event registry (`bus.on(kind, fn)`); app.js emits; replaces hardcoded
  `dailyOnEvent`/`studioOnEvent` incrementally.
- `keys.js` — single document keydown dispatcher: palette hotkey, Escape-closes-overlay
  (incl. approval modal), per-screen scopes cleared on navigate.
- `palette.js` — the search/command overlay (GET + navigate only).

`kairo.css` → token refactor: `:root` base + `[data-theme=light|noir|neon]` blocks using the
prototype's token names/values; density/layout/motion knobs; background = **CSS gradient
veils only** (no image assets — design/assets stays uncommitted; `--bg-intensity` scales the
veil); glass (`backdrop-filter`) on modal + palette only, solid `--panel` fallback; Studio's
hardcoded hex → tokens. System font stack (NO Inter, no external resources — the existing
asset test pins this).

Screens: rewrite `projects.js` (grid); new `workspace.js` (+ `workspace/*.js` panels),
`artifacts.js`, `settings.js`; extend `daily.js`, `studio.js`, `costs.js`; router gains one
capability: hash args (`#workspace/{id}`). Approval modal, nonce flow, `setSurface`
tracking, and `renderRunnerState` are load-bearing — untouched semantics.

## 7. Safety invariants (pinned by tests)

1. Mutation closed set: exactly 30 routes, enumerated (the 5 new ones named above); every
   new mutation is metadata-class; no generic KV route.
2. Palette performs GETs/navigation only — an enumerated-action pin asserts zero POST
   capability from palette code paths.
3. No eval-run route exists; the eval chip stays copy-command (ADR-0005).
4. Appearance is client-side only — assert no settings/theme mutation route.
5. Gate: amber reserved for attention; approval nonce + live-heartbeat flow unchanged; Gate
   badge reachable from every screen.
6. Debug/trace default-hidden; presentation-only (no route/capability keys off it).
7. `esc`/`escAttr` used for ALL dynamic interpolation; untrusted strings (transcripts,
   digests, memory, search snippets, artifact titles/content) render via
   textContent/escaped paths; injection tests with `<img onerror>`/`<script>` payloads.
8. No external resources (`test_no_external_resources_in_any_asset` extended over new files).
9. Secret sweep: auto-covers new GETs; ADD manual sweep tests for parameterized routes
   (`/api/artifacts/{id}`, `/{id}/content`, `/api/workspace/{id}`).
10. `/api/artifacts/{id}/content`: root confinement + `is_sensitive_path` refusal +
    registered-id-only + size cap (adversarially tested with escape/sensitive canaries).
11. Search scoping in SQL: cross-project canary pins (project A content never surfaces for
    project B filter); snippets only.
12. All Phase ≤10C contracts intact (Gate/taint/modes/floors/providers/eval ritual);
    `EVENT_SCHEMA_VERSION` unchanged.

## 8. Tests + screenshot definition of done

- **Keyless unit tests** per store/read model/route (TestClient + temp SQLite +
  FakeEmbedder pattern): FTS trigger↔base parity (insert/update/delete + the
  save_messages bulk path), idempotent rebuild, artifact dedupe/XOR/CHECKs, saved views,
  scoping canaries, cost-center groupings/warnings/ROI, workspace aggregate, content-route
  confinement, palette pin, mutation pin 30, sweeps, escaping.
- **Replay/keyless evals by default:** the replay gate (97 cassettes) must stay green after
  every task — `uv run kira eval gate --suite core` is $0/keyless. NO eval scenario
  changes expected; NO live judged gate until phase closeout (terminal ritual, Habib's
  machine, chunked commands).
- **Screenshot DoD (R4)** — captured via Kairo's own `playwright_local` (driver wired in
  T2): the six required shots — empty state, populated state, narrow/mobile (390w), project
  page, search results, artifact preview — in **noir** at 1440w, PLUS Daily + Workspace in
  all three themes, PLUS the no-overlap/clipping assertion (`scrollWidth <= innerWidth` +
  bounding-rect scan) at 1440/1024/390 on every primary screen. Shots land in gitignored
  `data/screenshots/` (`{screen}__{state}__{theme}-{width}w.png`); the capture harness
  exchanges the launch `?token=` for the session cookie.
- **A11y:** `a11y_check` snapshot on primary screens; keyboard: palette, tab order, Escape.

## 9. Staged task list (per-task commits; suite + ruff green each)

- **T1 — Plan + migration v9 + stores.** Commit this doc. Migration v9 (§5) + `ArtifactStore`
  (register/dedupe/pin/label/list) + `SavedViewStore` + projects.pinned plumbing + FTS
  triggers/backfill + rebuild command. Keyless tests (parity, dedupe, XOR, canary scoping at
  the store layer). *(L)*
- **T2 — Playwright driver + capture harness.** `browser` extra (`playwright>=1.4`),
  `PlaywrightInspectDriver` (implements `inspect`; guards untouched), `set_driver` in
  `run_ui` behind degrading import, capture harness (`tests/ui/capture.py`, not
  pytest-collected) with token exchange + viewport matrix + no-overlap check. Keyless tests
  via the injectable seam. Enables the screenshot DoD for every later task. *(M)*
- **T3 — Search service + artifact producers.** `search/` federated query + SQL scoping +
  snippets; producer hooks (digest/orchestration/eval/wiki/meeting) register artifacts with
  provenance + sensitive-path refusal. Adversarial canary tests. *(L)*
- **T4 — Read models + routes.** New GETs + the 5 mutations (pin 25→30) + cost-center
  read-model extensions + daily/vault extensions; manual sweeps for parameterized routes;
  content-route confinement tests. *(L)*
- **⛔ CHECKPOINT E — search/artifacts safety review (STOP for Habib).** Evidence, each with
  a named test: cross-project canaries; snippets-only; content-route escape/sensitive
  refusals; mutation pin = 30 exact; sweeps green over every new GET incl. parameterized;
  FTS parity + rebuild; replay gate green keyless; suite + ruff green. Report and WAIT.
- **T5 — Design system.** kairo.css token refactor + 3 themes + knobs; `ui/` layer (dom/
  components/theme/format); esc→import migration + escAttr fixes; status-bar theme toggle.
  First screenshot baseline (3 themes). *(L)*
- **T6 — Shell + IA + keyboard.** Nav rework (primary/utility split), router hash args,
  `bus.js` + `keys.js`, Gate badge persistent. *(M)*
- **T7 — Command palette.** Overlay + federated search + navigation actions + saved
  searches; GET/navigate-only pin. *(M)*
- **T8 — Daily command center.** Priority-ordered cards (§4) + empty states; cost card via
  runner state. *(M)*
- **T9 — Projects grid.** Cards + labels + pins + health + saved views row (R1 built-ins +
  user-defined). *(M)*
- **T10 — Project Workspace.** workspace.js + 9 tab panels (reusing scoped read models);
  Activity derived feed (metadata-only, Phase 14-replayable). *(L)*
- **⛔ CHECKPOINT F — visual direction review (STOP for Habib).** Screenshot pack: Daily,
  Projects, Workspace (Overview/Chats/Artifacts tabs) × 3 themes × 3 widths + no-overlap
  results. Habib signs off the visual direction before the remaining screens. Report and
  WAIT.
- **T11 — Chats + Artifacts screens.** Workspace Chats tab polish (search/links/empty
  states) + Artifacts Library (global + scoped, preview panel, pin/label). *(L)*
- **T12 — Studio polish.** Components adoption, timeline, head-reviewer badge, per-agent
  status/model/tools/cost chips. *(M)*
- **T13 — Cost Center.** Periods × dimensions, warnings, ROI aggregate, lazy breakdowns.
  *(M)*
- **T14 — Settings.** Appearance knobs (localStorage) + status sections + debug toggle.
  *(M)*
- **T15 — QA + docs + closeout prep.** Full screenshot DoD set + a11y + responsive matrix;
  ADR-0017 (workstation IA, token system, no-new-authority UI); README Phase 11; learning
  notes; replay gate green keyless; hand Habib the closeout checklist (live judged chunked
  gate = terminal ritual on his machine; no ratchet expected). *(M)*

## 10. Non-goals (explicit)

Semantic/embedding search; AI Team Office view (Phase 14 — Activity feed is its substrate);
connector writes (Phase 12); memory graph/Obsidian (Phase 15); notification center/dreaming
(Phase 16); chat message editing/regenerate/branching (11.5 fast-follow); "by mode" cost
grouping (no ledger column — `purpose` is the proxy; revisit only with a deliberate
migration); provider balance/usage APIs (future optional); Google Stitch adapter/MCP client
(Phase 13+; catalog + artifacts-provenance readiness only); mobile app; new WS event kinds;
background image assets (CSS veils only); a third rail column; eval-run affordance (never).

## 11. Opus implementation handoff

Execute T1–T15 in order with **mandatory stops at Checkpoint E (after T4) and Checkpoint F
(after T10)** — report evidence and WAIT for Habib's approval at each. Per-task commits with
explicit paths ending `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`.
Suite + ruff green at every commit; the keyless replay gate (`kira eval gate --suite
core`, $0) green at every task; if any cassette misses after a legitimate scenario-adjacent
change, re-record with `--record --max-cost-usd 5`, sweep (`sweep_cassettes` discipline),
and commit cassettes in a dedicated commit — do not change eval scenarios in this phase.
Amendments R1–R4 are binding (saved-view names; artifact schema fields; palette
no-authority; screenshot DoD). Never commit/modify the forbidden files (header). Never
weaken Gate/taint/modes/floors/providers/eval contracts. UI renders untrusted text via
textContent/escaped paths only; no external resources; no new WS kinds; no theme routes.
Reuse pins & patterns: closed-set mutation pin, auto-discovering secret sweep (+ manual
parameterized sweeps), FK-enforced project ids in fixtures, autouse `_close()` db fixtures,
hardened path confinement (`resolve_path` + `is_sensitive_path`) for the artifact content
route. When in doubt between beauty and calm: calm.
