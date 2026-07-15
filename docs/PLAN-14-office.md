# Jarvis Phase 14 — AI Team Office (visual orchestration layer)

*(Planned by Fable 2026-07-09; Opus 4.8 implements and commits this doc in Task 1. Baseline:
Phase 13 COMPLETE + live-verified (research services + S7 context reuse; ADR-0018/0019,
`docs/verification-13.md`, commit `3d2807e`). Suite green, core replay gate 19/19 $0, ruff clean,
migrations at v11, mutation-route pin at **35**. NEVER touch: docs/PLAN.md,
docs/PLAN-7-voice-consent-checkpoint.md, mcp_sample.json, config/settings.yaml,
config/permissions.yaml, design/.)*

## 0. Context — what this phase is (and is NOT)

Phase 14 adds an **optional, premium visual view over the existing orchestration system** — the
"AI Team Office": a calm operations-floor rendering of a project's teams, members, workflow stages,
head reviewer, costs, and live activity. It is a **render-only skin** over surfaces that already
exist. It is NOT a new engine, a new action path, a new authority, a game, or a toy dashboard.

**The calm `#studio` timeline stays the default.** The Office is an opt-in alternate view, reached
per project. `my-virtual-office` (AGPL) is **UX inspiration only** — no code, assets, or layouts are
copied; Kairo builds its own visual language on its existing token system.

This is a big *visual* phase but a small *surface* phase: one new read model (an assembler over
existing read models — no new storage, **no migration**), one new project-scoped screen tab, one
optional merge-safe layout-save route, and the CSS/JS for the view. Nothing here weakens a boundary;
every "action" in the Office is a click that calls an already-enumerated route.

## 1. What already exists (grounding — inspected, requirement 1)

- **Studio (`screens/studio.js`)** composes `/api/studio` (catalog: `teams`, `workflows`,
  `services`, `model_routes`, `active_project_id`, `busy`) + `/api/orchestration` (run summaries) +
  `/api/orchestration/{id}` (detail: `run` + `members[{role,stage,status,iterations,denied_count,
  cost_usd}]` + `roi` + `cost_breakdown{by_stage,services}` + `synthesis_summary` + `context_manifest`).
  It already renders a **stage timeline** (council→synthesis→execution→review→verdict→done), **member
  cards** (title, capability, `route_role → model·provider`, tool/service chips), a **head badge**
  (Fable on the planner route = synthesis + final verdict, an engine stage), and a **live panel**
  driven by `onEvent`.
- **Live events (WS, `EVENT_SCHEMA_VERSION = 2`)** flow through `app.js`: every message whose `kind`
  starts with `orchestration_` is `busEmit("orchestration", msg)`'d AND fed to `studioOnEvent`.
  Fields: `orchestration_started{run_id,team,workflow,title,estimated_cost_usd}`,
  `orchestration_stage{run_id,stage}`, `orchestration_agent{run_id,team,role,member,stage,ok}`,
  `orchestration_round{run_id,round,verdict}`, `orchestration_completed{run_id,status,verdict}`.
- **Workspace shell (`screens/workspace.js`)** is a per-project tabbed home at `#workspace/{id}/{tab}`
  with a FIXED tab allowlist (`overview·chats·artifacts·memory·tasks·vault·studio·costs·activity`),
  lazy-loading `screens/workspace/{tab}.js` behind a per-tab error boundary. It already has a
  `studio` tab (recent runs + "Launch in Studio") and an `activity` tab
  (`/api/workspace/{id}/activity` — a coarse, metadata-only run/artifact/chat feed).
- **Teams (`orchestration/teams.py`)**: 8 `TeamProfile` constants —
  `research·frontend·backend·security·qa·pm·ops·custom` — each with members
  `{id,title,route_role,capability,tools,services}` + `default_workflows`. Data-driven, so the
  Office renders a room per team the catalog returns (custom teams included).
- **Design system (`kairo.css`, 658 lines)**: a CSS-custom-property token set on `:root`, overridden
  per theme via `:root[data-theme="light"|"neon"]` (noir is the default), plus `data-density`,
  `data-layout`, and a `.reduce-motion` class + `prefers-reduced-motion`. Tokens: `--canvas --ink
  --muted --subtle --line(-strong) --panel(-strong/-soft) --shadow(-soft) --accent(-rgb) --accent-2/3
  --good --attention --danger --cost --veil-a/b/c --gap --radius --motion --nav`. The premium/calm
  look is **CSS gradients only (an atmospheric veil) — zero image assets**; icons are emoji/monograms.
  Responsive breakpoints at `900px` and `720px` (rail collapses to 58px).
- **Screenshot DoD (`tests/ui/capture.py` + `kira.ui.screenshots`)**: a headless-browser harness
  over `THEMES × VIEWPORTS` that screenshots each `hash:screen:state` and runs `analyze_overlap`
  (no element overlap, no horizontal overflow); the pure machinery is unit-tested keyless
  (`test_screenshot_harness.py`). Viewports are the pinned 1440/1024/390 (verification-11).
- **Safety patterns to reuse**: `ui/dom.js` `el()`/`esc()` (textContent-only building); the
  closed **mutation-route pin** (`test_ui_readmodels.test_mutation_route_closed_set`, currently 35);
  the **secret-absence sweep** over every GET; merge-safe `settings_json` writes
  (`ProjectStore.set_label`/`set_services`); the appearance layer (`ui/theme.js`, localStorage only).

## 2. Do we need an Activity-Feed substrate first? (requirement 2 — DECISION)

**Yes — a small read-model substrate task first, but it is a pure ASSEMBLER, not a new event log
or table.** Reasoning:

- Phase 11's `/api/workspace/{id}/activity` is **too coarse** for the Office (run/artifact/chat rows
  only — no per-member, per-stage, per-cost granularity).
- BUT the granular data **already exists**: live per-agent/stage transitions arrive on the WS
  `orchestration_*` stream (ephemeral, fine for live), and the persisted per-member detail
  (role/stage/status/iterations/cost) is in `orchestration_run_detail`, with per-stage/service cost
  in `cost_breakdown`. The static roster (model/provider/tools/services) is in `teams_catalog` +
  `model_routes` + `services`.
- So the Office needs **no new persisted feed, no new table, no migration**. It needs one tailored
  projection that shapes the existing read models into "office state" (rooms→nodes), plus the live
  WS stream it already receives via the bus.

**Task 1 builds that substrate** (`office_overview` read model + `GET /api/workspace/{id}/office`)
BEFORE any visuals, so the view is a thin render over a tested, secret-swept, office-shaped payload.

## 3. Office data model / read model (requirement 3)

`readmodels.office_overview(config, store, project_id, *, budgets=None) -> dict` — a **pure
assembler** over `teams_catalog()`, `model_routes_status()`, `services_status()`,
`orchestration_runs_view(project_id)`, and (for the latest/selected run) `orchestration_run_detail`
+ `cost_breakdown`. Presence/metadata/summaries only — never a prompt, report body, or key value.

```
{
  "project_id": int,
  "head": { "label": "Fable", "route": {model, provider} },     # planner route; synthesis + verdict
  "stages": ["council","synthesis","execution","review","verdict"],  # canonical order for the map
  "rooms": [                                                     # one per team in the catalog
    { "team": "research", "name": "Research", "icon": "…", "accent": "<token-or-hex>",
      "nodes": [ { "member_id","title","role","capability",     # static roster (from catalog+routes)
                   "model","provider","tools":[…],"services":[{name,state}],
                   # overlaid from the active/last run when present:
                   "stage": str|None, "status": "idle|running|ok|denied|error",
                   "cost_usd": float|None, "iterations": int|None } ] } ],
  "live": { "run_id","team","workflow","title","stage","status","verdict",
            "estimated_cost_usd","actual_cost_usd" } | None,     # the in-flight/last run summary
  "recent_runs": [ {id,team,workflow,status,verdict,actual_cost_usd,estimated_cost_usd,ts} ],
  "feed": [ {ts,type,title,status} ]                             # short metadata rows (existing activity feed)
}
```

Route: `GET /api/workspace/{project_id}/office` (read-only; mirrors `/api/workspace/{id}` + `/activity`
plumbing). The client also composes the **live** layer from the WS `orchestration_*` bus (§4). No
new event storage. `feed` reuses the Phase-11 activity read model (metadata + short summaries).

## 4. UI architecture + modules (requirement 4)

**Placement:** a new **project workspace tab `office`** (project-scoped ⇒ matches "per project"),
added to the `workspace.js` `TABS` allowlist. Route `#workspace/{id}/office`. The top-level `#studio`
screen is untouched and remains the default calm view. New/edited files:

- `src/kira/ui/static/screens/workspace/office.js` — the Office panel (NEW). Renders from
  `/api/workspace/{id}/office`; subscribes to `busOn("orchestration", …)` for live patches.
- `src/kira/ui/static/screens/workspace.js` — add `["office","Office"]` to `TABS` (allowlist).
- `src/kira/ui/static/ui/office.js` — pure view helpers (room/zone layout, node rendering, stage
  map, feed formatting), unit-friendly + reused by both modes (NEW, optional split).
- `src/kira/ui/static/kairo.css` — an `/* --- Phase 14 Office --- */` block using existing tokens
  only (no new theme, no assets).
- `src/kira/ui/readmodels.py` — `office_overview`; `src/kira/ui/server.py` — the GET route
  (+ optional layout-save route, §10 M3).

**Two modes, one data source (requirement 5):**
- **Compact (default):** a serious, dense layout — the stage map as a slim horizontal rail, rooms as
  compact cards with a small node grid, a text live feed. Calm, professional, information-first.
- **Office (opt-in):** the richer "operations floor" — team **rooms/zones** as larger translucent
  panels laid out on a responsive grid, members as **status nodes** with a status ring + monogram,
  the stage map as a calm flow with the head "chair", a fuller live feed. Same data; more space +
  spatial arrangement. The mode is a toggle in the Office header; it never becomes the app default.

**Live-update strategy (performance-critical, §9):** the Office subscribes to the orchestration bus
`app.js` already emits. On each event it patches its in-memory `live` model and **surgically repaints
only the affected node/stage/feed row** (never a full re-render, never a `refreshIfActive("workspace")`
which would re-fetch + re-import). A module-singleton listener guards on the office root's DOM
presence (repaints only when mounted; inert after a tab switch — the established guard pattern).

**Actions reuse existing routes only (no new action path):** a "Launch" affordance deep-links to
`#studio` (or POSTs the SAME `/api/orchestration/run` the Studio uses); "Cancel" POSTs the existing
`/api/orchestration/{run_id}/cancel`; approvals surface through the SAME global amber overlay
(`app.js` `onApproval`) — the Office never mints a nonce or resolves an approval itself. Per-node
**inspect panel** links out (GET/navigate only) to Trace (`#trace`), Artifacts
(`#workspace/{id}/artifacts`), and Costs (`#workspace/{id}/costs`).

## 5. Visual language (requirement 5 — Kairo's own, token-driven, no assets)

- **Rooms/zones:** translucent `--panel`/`--panel-soft` surfaces over the existing veil gradient,
  each team tinted by a per-team **accent** (a small fixed palette mapped to team id, or the
  project's color for its own room), a thin `--line` border, `--radius` corners, `--shadow-soft`
  lift. Room header = emoji/monogram + team name + a compact "N members · stage · $cost" line.
  Laid out on `grid` (auto-fit, `minmax`) so rooms reflow at 900/720px to 1-col.
- **Status nodes (NOT cartoons):** a circular monogram (member initials) or the role emoji inside a
  **status ring** whose color is a token — `--subtle` idle, `--accent` running (a gentle pulse,
  motion-gated), `--good` ok, `--attention` review/denied, `--danger` error. Under it: title,
  `role · model·provider` (mono, dim), tool/service chips (existing `.chip` classes with the
  service-state color). Professional, calm — no faces, no animations beyond a subtle running pulse.
- **Stage map:** the canonical `council → synthesis → execution → review → verdict` as a calm
  horizontal flow (reusing/extending the existing `.timeline`/`.stage` classes): past = filled dim,
  active = accent, future = outline. The **head reviewer** (Fable/Opus) is a distinct terminal
  "chair"/verdict node with the existing head badge, clearly labeled as the synthesis + final verdict
  engine stage (never a team member).
- **Activity feed:** a right-hand/bottom column of short rows — `{icon} {short title} · {stage/status}
  · {relTime}` — built with `esc()`/textContent. **Metadata + short summaries only**; never a report
  body, prompt, or key. `aria-live="polite"`, capped length (§9).
- **Calm by default:** noir/light/neon all derive from tokens; motion respects `.reduce-motion` +
  `prefers-reduced-motion`; the Office reads `data-density`/`data-layout` like every other screen.

## 6. Accessibility + keyboard (requirement 6)

- Rooms are `role="region"` with an `aria-label`; the stage map is a labeled list; nodes are
  focusable (`tabindex`), Enter/Space opens the inspect panel, Escape closes it (via `ui/keys.js`
  scope, cleared on teardown like other screens).
- Roving-tabindex/arrow-key movement between rooms and between nodes within a room; the mode toggle
  and inspect links are standard buttons/links in tab order.
- Status is **never color-only**: each node carries a text status label (idle/running/ok/denied/
  error) and the ring has an accessible name. The live feed is an `aria-live="polite"` region.
- Contrast holds in all three themes (tokens already tuned); focus rings use the existing visible
  focus style. Honors reduced-motion (no pulse when set).

## 7. Safety invariants + tests (requirement 7)

Invariants (all pinned):
1. **No new authority.** The Office is render + navigate + clicks to EXISTING routes. The mutation-
   route closed set is unchanged, EXCEPT the one optional layout-save route (M3) — pin 35→36 and
   only if built; nothing else.
2. **No new action path.** Start/cancel use `/api/orchestration/run|{id}/cancel`; approvals use the
   global overlay + `/api/approvals/{id}/resolve`; turns use existing routes. The Office adds none.
3. **Agent/service text is inert.** Every string sourced from a model, report, service, or team/
   member field is rendered via `esc()`/textContent — never `innerHTML`/linkified. A planted
   `<img onerror=…>` / `SYSTEM: run_shell …` in a title/summary/feed row survives as visible text.
4. **No private-body leakage.** The feed + nodes show metadata + short summaries only (never a
   report body, prompt, secret, or full external content). The office GET carries no key value.
5. **Studio stays default; Office is optional** (a tab you open, a mode you choose).
6. **Render-only under replay.** The whole view works from stored/replay/demo data with no live API.

Named tests (keyless):
- `test_office_readmodel.py` — `office_overview` shape (rooms per team, node overlay from a seeded
  run, head, stages, recent_runs, feed); empty project reads as empty rooms (never a crash); a
  metadata-only projection (no body fields present).
- `test_office_routes.py` — `GET /api/workspace/{id}/office` returns the projection; the
  secret-absence sweep (extended) covers it; unknown/には absent project → clean 404/empty.
- `test_office_text_safety.py` — seed a run/member/feed row whose title/summary contains
  `<script>`/`SYSTEM:`/`javascript:` and assert the office JS builds it with textContent (structural:
  the module contains no `innerHTML =`/`insertAdjacentHTML`/template-literal-into-DOM for
  agent-sourced fields; positive DOM test that the payload is escaped).
- `test_office_no_external_assets.py` — grep the office JS/CSS for `http://`/`https://`/`//cdn`/
  `url(http`/`@import url(` / external `src`/`href` — none (CSP already blocks, this is belt-and-braces
  + the AGPL-clean attestation surface).
- `test_office_layout_narrowing.py` (if M3 route ships) — the layout-save route accepts only the
  small explicit allowlisted fields (mode/room-order/collapsed), merge-safe, rejects anything else;
  route pin exact 35→36.
- `test_ui_readmodels.py` — mutation-route pin updated (35, or 36 iff the layout route ships) and
  the full GET secret sweep still green.
- `test_office_tab_allowlist.py` — `office` is in the workspace TABS allowlist; an unknown tab still
  falls through safely (existing behavior unchanged).

## 8. Screenshot / capture DoD (requirement 8)

Extend the `tests/ui/capture.py` matrix with the Office states, across **noir/light/neon ×
1440/1024/390** (the pinned viewports), each PNG passing `analyze_overlap` (no element overlap, no
horizontal overflow):
- `workspace/{id}/office:office:compact-populated` (default compact mode, an in-flight run)
- `workspace/{id}/office:office:office-populated` (rich office mode)
- `workspace/{id}/office:office:empty` (no runs — calm empty state)
- `workspace/{id}/office:office:large` (the synthetic large run, §9)

Seed the states from replay/demo data (no live API). The DoD is GREEN = zero layout violations
across the full theme×viewport×state grid; captured in `docs/verification-14.md`. Reuse
`kira.ui.screenshots` (keyless-unit-tested machinery) unchanged.

## 9. Performance for long/large runs (requirement 9)

- **Incremental patch, never full re-render:** bus events mutate `live` and repaint only the changed
  node/stage/feed row. No `refreshIfActive("workspace")` on events (that re-fetches + re-imports).
- **Bounded live feed:** cap to the last N rows (e.g. 50), oldest dropped; long-run history stays in
  `recent_runs`/the persisted activity tab, not the live DOM.
- **Coalesced repaints:** batch bursts of events into one `requestAnimationFrame` repaint; the ring
  pulse is CSS (GPU), motion-gated.
- **Bounded roster/rooms:** rooms = number of teams (≤ ~8 + custom); nodes = members per team (small).
  A "large run" test seeds many rounds/agent events + a big `recent_runs` list to confirm the view
  stays responsive (paint budget) and memory is bounded (feed cap, no unbounded event array).
- **Cheap first paint:** the office GET is a single assembler round-trip; the live layer attaches
  after. No polling loop beyond the existing status poll.
- A `test_office_perf_bounds.py` (keyless) asserts the feed cap + that N synthetic events produce a
  bounded in-memory structure (no unbounded growth) via the pure view helpers.

## 10. Milestones + checkpoints (requirement 10)

Discipline unchanged: every task green (`ruff` + full suite + `eval gate --suite core` replay $0),
per-task commit with explicit paths, adversarial review before each commit. **Studio stays default
throughout; the Office ships behind the workspace tab, never as the app landing.**

- **M0 — Office substrate (Task 1).** Commit this doc. `office_overview` read model (assembler, no
  storage, no migration) + `GET /api/workspace/{id}/office` + `test_office_readmodel.py` +
  `test_office_routes.py` (incl. the extended secret sweep). No UI yet.
- **M1 — Office tab + compact mode (Tasks 2–3).** Add `office` to the workspace TABS allowlist;
  `screens/workspace/office.js` rendering the **compact (default)** mode from the GET; live bus
  subscription with surgical patching; per-node inspect panel (GET/navigate links); actions wired to
  EXISTING routes (launch→#studio / `/api/orchestration/run`; cancel→`/api/orchestration/{id}/cancel`).
  `test_office_tab_allowlist.py` + `test_office_text_safety.py`.
- **M2 — Visual office mode + stage map (Tasks 4–5).** The rich rooms/zones + status nodes + stage
  map + head chair + live feed; the Compact↔Office toggle; the `/* Phase 14 Office */` CSS (tokens
  only, no assets). `test_office_no_external_assets.py`.
- **M3 — Layout persistence (Task 6).** Project-specific layout in `settings_json["office_layout"]`
  (small explicit blob: mode default, room order, collapsed rooms). Prefer **localStorage-only**
  (like themes) if that satisfies the product; add the ONE merge-safe route
  `POST /api/projects/{id}/office-layout` (subset-validated, `set_label` pattern) **only if
  server-side persistence is required** — route pin **35→36**. `test_office_layout_narrowing.py` +
  the pin update.
- **M4 — A11y + performance + adversarial pins (Task 7).** Keyboard/ARIA/reduced-motion; the
  incremental-patch + feed-cap + coalesced repaint; `test_office_perf_bounds.py`; finalize the
  text-safety + no-authority pins.

  **⛔ CHECKPOINT I — MANDATORY render-only sign-off (Checkpoint-F pattern).** Full stop, report,
  wait. Evidence, per bullet with named tests: (i) no new authority — mutation pin exact (35, or 36
  iff the layout route shipped) and nothing reaches a tool/executor directly; (ii) every action
  routes through an existing Studio/Gate/turn route (no new action path); (iii) agent/service text
  is textContent/escaped (planted-injection stays inert); (iv) feed/nodes are metadata + short
  summaries, no private body, no key value (secret sweep over the new GET); (v) no external
  assets/CDN (pinned); (vi) no copied AGPL code/assets — Kairo's own token-driven visual language
  (attested); (vii) works fully under replay/demo (no live API); (viii) screenshot DoD GREEN across
  noir/light/neon × 1440/1024/390 for compact + office + empty + large, zero overlap/overflow;
  (ix) performance bounds under the large synthetic run (incremental patch, feed cap); (x) Studio
  remains the default, Office is optional; (xi) full suite + ruff green. Report, then continue only
  on Habib's approval.

- **Task 8 — docs + live/demo verification (after Checkpoint I).** ADR-0020 (the AI Team Office —
  render-only visual layer, no new authority), `docs/verification-14.md` (the DoD grid + the demo
  walkthrough), README Status entry. No behavior change; no baseline ratchet.

## 11. Live / demo verification (requirement 11 — Task 8, after approval)

1. `kira --ui`; open a project → **Workspace → Office**. Confirm: Studio (`#studio`) is still the
   default; the Office is a tab, opt-in.
2. With **replay/demo data** (no live API): rooms render for each team; the stage map + head chair
   are clear; nodes show role/model/provider/tools/services/cost/status.
3. Launch a run from the Office (deep-link/existing route) → watch the live stage map + node rings +
   feed update in real time via the WS bus; confirm surgical repaint (no full-screen flicker) and the
   feed stays capped on a long run.
4. Trigger a risky action mid-run → the SAME global amber overlay handles the approval (the Office
   minted nothing). Cancel → existing cancel route.
5. Toggle Compact↔Office; toggle noir/light/neon and shrink to 390px — no overlap/overflow; a saved
   layout (if M3 shipped) persists per project.
6. Text-safety spot check: a run/member/feed field containing markup renders as visible text.
7. Capture the screenshot DoD grid; record results in `docs/verification-14.md`.

## 12. Opus 4.8 handoff (requirement 12)

Execute Tasks 1–8 in order; **MANDATORY stop at ⛔ Checkpoint I** (after Task 7) with the 11-bullet
evidence, before Task 8 and before any live/demo. Per-task commits with explicit paths (never
`git add -A`) ending `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`;
adversarial review before each commit; suite + ruff + `eval gate --suite core` replay green every
task. Reuse, never fork: `ui/dom.js` `el()`/`esc()`, the workspace tab shell + allowlist + error
boundary, the orchestration bus (`busOn`), `ui/theme.js`/`ui/keys.js`, the token system (no new
theme, **no image assets/CDN**), `kira.ui.screenshots` (DoD machinery), the `set_label`/
`set_services` merge-safe settings-write pattern, the secret-sweep + mutation-route-pin discipline.
**No new authority, no new action path, no new storage/migration** (the office read model is an
assembler; layout is a small explicit blob). The Office must never become the app default; the calm
Studio stays home. Keep the plan's scope — do NOT drift into the memory graph, dreaming/automation,
or packaging (Phases 15–17). ADR-0020 reserved for this phase.
