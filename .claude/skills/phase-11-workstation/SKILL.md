---
name: phase-11-workstation
description: >-
  Kairo Phase 11 (Workstation UI/UX + product surface) implementation rules. Load and follow
  when implementing, reviewing, planning, or spawning subagents for ANY Phase 11 task — the
  search/artifacts backend, migration v9, UI screens, the design/token system, routes, the
  command palette, Studio/Cost/Settings surfaces. The canonical, fuller version is
  docs/phase-11-implementation-playbook.md; this skill is its always-loaded distillation.
---

# Phase 11 — Workstation implementation rules

You are an implementation/worker agent on **Kairo Phase 11** (plan: `docs/PLAN-11-workstation.md`;
full playbook: `docs/phase-11-implementation-playbook.md`). Follow these rules; if a task conflicts
with them, STOP and raise it rather than guessing. Working rhythm every task:
**understand → implement in strict order → adversarially review with subagents → verify (suite +
ruff + keyless replay gate) → commit with explicit paths.** Never commit red.

## Non-negotiable safety invariants (pinned by tests)
- **The UI adds NO new authority.** It reads and navigates. Every write / generation / action goes
  through the EXISTING Gate/turn/mutation routes — a screen never reaches a tool/executor/file
  write directly.
- **Mutation-route closed set is a pin (currently 30)** — `test_ui_readmodels.py::
  test_mutation_route_closed_set`. New mutations must be metadata-class, mirror an existing one
  (e.g. `sessions/{id}/pin`), and be added to that set in the same commit. No generic/"run
  anything" route; no eval-run route.
- **Secret sweep stays intact** — auto-covers non-parameterized GETs; you MUST add a manual sweep
  test for each new parameterized GET (it skips `{param}`). No secret/token/session-id on the wire.
- **The command palette + search GET/navigate ONLY — never POST.** A "write" entry navigates to
  the surface that owns the write.
- **Appearance is client-side only** (localStorage) — no server theme/settings route (new authority).
- **Amber = attention/decisions only.** Gate approval (nonce + live heartbeat) unchanged + reachable
  from every screen. Debug/trace default-hidden, presentation-only.
- **No external resources** in any asset (only the `http://www.w3.org/` SVG namespace). Untrusted
  strings render via `textContent`/escaped paths only; attribute interpolation uses quote-safe
  `escAttr` from `ui/dom.js`.

## Search / artifacts invariants (from Checkpoint E — do not regress)
- **Project scoping in SQL/JOIN, never in `MATCH`.** A project-B query must never return a
  project-A row (adversarially pinned).
- **Snippets only** — never a full body; chat JSON projected to plain prose, capped.
- **Quarantined content is never searchable or servable** (unreviewed meeting transcripts, ADR-0004).
- **Content route never leaks paths/secrets** — `serialize_artifact` omits `local_path` (ships
  `has_content`); `/api/artifacts/{id}/content` serves a registered id only, via
  `ArtifactStore.content_path` (re-confines + refuses sensitive), fixed text/image media allowlist,
  size-capped. Producer hooks are guarded + fail-soft.

## Discipline
- **Phase/scope:** stop at checkpoints (report + WAIT); build the task in front of you; do NOT turn
  UI work into backend rewrites (the v9 search/artifacts backend is done behind Checkpoint E — ride
  it, don't reopen it).
- **UI/UX:** `design/` is READ-ONLY reference (never modify/commit). Premium but calm; one primary
  attention surface per screen; every screen has a designed empty state; tokens not per-screen hex;
  system fonts + CSS gradient veils (no Inter, no heavy PNGs).
- **Vanilla-modular frontend:** ES modules + plain CSS, no framework/build. Split into the
  `static/ui/` leaf layer + thin screens; don't grow `app.js`. One quote-safe escaper (`ui/dom.js`).
- **Eval/cost:** keyless replay gate is the default and must stay green each task; `--live`/`--record`
  cost real money — explicit, capped, rare. Do NOT launch `--record` casually.
- **Subagents:** use them for scoped review/discovery; avoid overlapping edits to the same file
  (implementation is sequential + committed per task); adversarially verify findings before trusting.

## Forbidden files — never modify or commit
`docs/PLAN.md` · `docs/PLAN-7-voice-consent-checkpoint.md` · `mcp_sample.json` ·
`config/settings.yaml` · `config/permissions.yaml` · `design/`. Stage only explicit paths;
never `git add -A`.
