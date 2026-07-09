# Phase 14 — verification (AI Team Office, render-only DoD)

*Run 2026-07-09. Phase 14 is a RENDER-ONLY visual layer, so verification is the screenshot
definition-of-done + the keyless structural/safety suite — no live API, no interactive UI session,
no baseline ratchet. The screenshot grid was produced by the self-contained harness
[`tests/ui/office_dod.py`](../tests/ui/office_dod.py) (it seeds `office_overview` JSON in-process,
serves a COPY of the static dir, and runs the REAL `office.js` + `kairo.css` in headless chromium —
no running app / auth / DB). PNGs land under the gitignored `data/screenshots/office`; the real
`data/` tree, config, and `.env` were untouched.*

## Screenshot DoD — GREEN (36/36)

`analyze_overlap` (no element past the viewport, no horizontal scroll; `jarvis.ui.screenshots`,
chromium-1228) across **4 states × 3 themes × 3 viewports**:

| State | noir | light | neon |
|---|---|---|---|
| `compact-populated` (dense default, live run) | ✅ 1440/1024/390 | ✅ 1440/1024/390 | ✅ 1440/1024/390 |
| `office-populated` (roomier floor, live run) | ✅ 1440/1024/390 | ✅ 1440/1024/390 | ✅ 1440/1024/390 |
| `empty` (no runs — calm idle rooms) | ✅ 1440/1024/390 | ✅ 1440/1024/390 | ✅ 1440/1024/390 |
| `large` (24-run synthetic — feed/recent stress) | ✅ 1440/1024/390 | ✅ 1440/1024/390 | ✅ 1440/1024/390 |

Zero layout violations across all 36. Spot-checked visually: the Office-mode/noir/1440 operations
floor (8 team rooms, accent tops, the stage flow → Fable's chair, the live "Security · review" strip
with Cancel, member status nodes with monogram rings + tool/service chips + status pills, side feed +
recent runs), Compact/light/1440 (denser 4-column grid, correct light tokens), and large/neon/390
(clean single-column mobile reflow, wrapped stage rail, populated feed, no overflow).

## Structural + safety verification (keyless suite)

Seven `test_office_*.py` files + `test_workspace_ui` (extended) pin the Checkpoint-I walls:

| Wall | Test |
|---|---|
| No new authority — mutation pin **35**, layout has no route | `test_ui_readmodels`, `test_office_layout` |
| No new action path — office posts ⊆ `/api/orchestration/`, never `/api/turn` | `test_workspace_ui`, `test_office_tab_allowlist` |
| Agent/service text inert — no `innerHTML`/`html:`, `CSS.escape`, hostile-title passthrough | `test_office_text_safety` |
| No private body / no key value — bodies-free overlay, secret sweep on the GET | `test_office_readmodel`, `test_office_routes` |
| No external assets / AGPL-clean — no `http`/`//cdn`/`@import`/`url()` | `test_office_no_external_assets` |
| Bounded performance — rAF coalescing, feed cap (DOM + buffer), bounded Set/Map, no full re-render | `test_office_perf_bounds` |
| Studio default; Office opt-in tab; unknown-tab gate intact | `test_office_tab_allowlist` |

Full suite **1832 passed**, ruff clean, `jarvis eval gate --suite core` **19/19 PASS** (two
fresh-process replays), `office.js` `node --check` OK.

## Render-only "walkthrough" (what the DoD actually exercised)

Because this is a render-only phase, the plan's live walkthrough (§11) is covered by the DoD + the
keyless pins rather than an interactive session: the Office renders per team as rooms with the stage
map + head chair (all four states); a seeded in-flight run overlays its team's nodes (live status +
cost) and lights the live strip; the Compact↔Office toggle relayouts via the root class; noir/light/
neon + 1440/1024/390 show no overlap/overflow; and a hostile title renders as visible text. The live
WS-bus patching, per-node inspect (navigate-only), and launch/cancel (existing routes) are pinned
keyless (`test_office_perf_bounds`, `test_office_text_safety`, `test_workspace_ui`); a live UI session
would exercise them against a running orchestration but was not required for the render-only gate.

## Orthogonal: adversarial replay is red because cassettes are MISSING (not failures)

Independent of Phase 14 and of the eval clock-freeze fix (commit `7bb5f4f`):

- `jarvis eval gate --suite adversarial` (and `--suite all`) is **red** at HEAD — all 22 adversarial
  scenarios (`inj_*` + `voice_*`) **MISS at call #1**. This is because the committed cassette dir
  (`tests/evals/cassettes`) holds ONLY core-suite + judge cassettes; **no adversarial cassettes were
  ever recorded/committed**. It is a MISSING-baseline condition, **not** a scenario/behavior failure.
  Verified by stashing unrelated changes and replaying at original code (same 22/22 miss).
- **`jarvis eval gate --suite core` (19 scenarios, keyless, $0) remains the per-task replay gate** for
  this phase, and is green.
- **Follow-up options** (either is acceptable; to be chosen later, out of Phase-14 scope):
  1. **Record + commit** the adversarial cassettes once (live, `--record`), then `--suite adversarial`
     replays green like core; or
  2. **Document adversarial as live-only** — run it as a live ritual (the Phase-9/13 injection-proof
     pattern) and keep core as the committed keyless gate.

## Cleanup / not done (deliberate)

Task 8 is docs-only (this file + ADR-0020 + the README Status entry) — no behavior change, no
baseline ratchet, no migration (the office read model is a pure assembler). No `config/settings.yaml`,
`config/permissions.yaml`, `.env`, connector/token file, or `design/` was touched. Not done, by
design: an interactive live UI walkthrough (render-only phase; DoD + keyless pins cover it) and the
adversarial-cassette decision above.
