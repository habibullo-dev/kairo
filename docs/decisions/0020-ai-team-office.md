# ADR-0020: AI Team Office (Phase 14)

*Status: accepted (Phase 14, 2026-07-09). An OPTIONAL, premium visual view over the existing
orchestration system — a "team office / operations floor" rendering of a project's teams, members,
workflow stages, head reviewer, costs, and live activity. It is a render-only skin; it adds no
engine, no new authority, and no new action path. Checkpoint I (render-only sign-off) approved.*

## Context

Phase 10B/13 built the orchestration engine + the calm `#studio` timeline (roster cards, stage
timeline, head-reviewer badge, live panel). Phase 14 answers "can we SEE the team working?" with a
spatial, calm operations-floor view — teams as rooms, members as status nodes, the workflow as a
stage flow ending at Fable's "chair" — without becoming a new engine, a new action path, a game, or
a toy dashboard. `my-virtual-office` (AGPL) was UX inspiration ONLY; no code, CSS, layout, or asset
was copied — Kairo builds its own visual language on its existing token system.

## Decision

- **A project workspace TAB, not a new top-level screen.** `#workspace/{id}/office`, added to the
  fixed `workspace.js` `TABS` allowlist (loaded behind the existing validated dynamic import + error
  boundary). The calm `#studio` stays the app default; the Office is opt-in.
- **One new read model, a PURE ASSEMBLER — no storage, NO migration.** `office_overview(config,
  services, project_id)` shapes existing read models (`teams_catalog`, `model_routes_status`,
  `services_status`, orchestration list + `member_runs`, `activity_feed`) into office state
  (rooms → member nodes + head + stage map + live summary + recent runs + feed). Metadata / short
  summaries only. Served read-only at `GET /api/workspace/{id}/office`.
- **Two layouts, one DOM + one data source:** Compact (default — dense, information-first) and Office
  (roomier operations floor), switched by a root class (`.office-compact` / `.office-full`) — a pure
  CSS relayout, never a re-render or refetch.
- **Live via the existing WS bus + surgical patch.** A module-singleton `busOn("orchestration")`
  listener, guarded on the office root's DOM presence (inert after a tab switch), coalesces events
  into ONE `requestAnimationFrame` repaint and touches only the affected stage pip / room / member
  node / feed row — never a full re-render, never `refreshIfActive`.
- **Actions reuse existing routes ONLY.** "Launch" deep-links to `#studio`; "Cancel" POSTs the
  existing `/api/orchestration/{id}/cancel`; approvals surface through the app's global amber overlay
  (the Office mints no nonce, resolves no approval); per-node inspect only navigates (GET) to Trace /
  Artifacts / Costs.
- **Layout persistence is localStorage-ONLY** (the `ui/theme.js` pattern): mode + collapsed rooms in
  `kairo:office:{projectId}`, clamped on read. The plan's optional `POST /api/projects/{id}/
  office-layout` was **not** shipped — so the mutation-route closed set is unchanged (**pin stays
  35**).

## The walls (all pinned by tests)

- **No new authority** — render + navigate + clicks to already-enumerated routes; mutation-route pin
  exactly **35** (`test_ui_readmodels`, `test_office_layout`).
- **No new action path** — start/cancel/approve/turn all go through existing routes; the office panel
  posts only to `/api/orchestration/` (cancel), never `/api/turn` (`test_workspace_ui`).
- **Agent/service text is inert** — every model/service/member/feed string is built via `el()` text
  children (textContent); no `innerHTML`/`insertAdjacentHTML`/`html:`; dynamic selectors use
  `CSS.escape`. A planted `<script>`/`SYSTEM:`/`javascript:` in a title survives as visible text
  (`test_office_text_safety`).
- **No private body / no key value** — the assembler is metadata + short summaries only; member
  prompts/reports never appear; the GET is secret-swept over provider + prompt/report canaries
  (`test_office_readmodel`, `test_office_routes`).
- **No external assets / AGPL-clean** — the office JS + the fenced `/* Phase 14 Office */` CSS block
  carry no `http`/`//cdn`/`@import`/`url()`; token-driven, Kairo's own (`test_office_no_external_assets`).
- **Works under replay/demo** — a pure assembler over stored data; the whole screenshot DoD ran on
  seeded JSON with no live API.
- **Bounded performance** — rAF-coalesced repaints (single-flight), feed capped in DOM **and** buffer,
  pending patches bounded (Set of rooms / Map of members) (`test_office_perf_bounds`).
- **Studio stays default; Office is optional** — a tab you open, a mode you choose.

## Consequences

Kairo gains a calm, premium way to watch a project's teams work — teams as rooms, members as status
nodes with live rings, the stage flow to Fable's chair, a bounded live feed — entirely as a
render-only skin over surfaces that already exist. Nothing here can start work, approve a risk, or
leak a body/key that the underlying routes don't already govern; the whole view degrades to idle
rooms when a service is absent, and works fully under replay. Reversible by not opening the tab.
ADR numbering: 0020 (Phase 14). Next reserved: 0021 (Phase 15 — memory graph).
