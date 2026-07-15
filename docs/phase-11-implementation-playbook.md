# Phase 11 — Implementation Playbook (historical)

> **Historical design record.** Phase 11 is complete. This document preserves the rules, counts,
> paths, and checkpoints used while building the Kira Workstation; it is evidence, not current
> operating instruction. Current behavior is defined by code, tests, and the operator documentation.

*Repo-local implementation guidance preserved from the Kira Workstation phase
(`docs/PLAN-11-workstation.md`). This was not a product feature or runtime skill system.*

The phase used this working rhythm: **understand → implement in strict order → adversarially review
(subagents) → verify (suite + ruff + keyless replay gate) → commit with explicit paths.** It required
green commits and excluded the phase-specific forbidden files listed at the bottom.

---

## 1. Phase discipline (historical)

- **Stop at checkpoints.** Checkpoint E (after T4, backend safety) and Checkpoint F (after T10,
  visual sign-off) are mandatory STOPs — report evidence and WAIT for Habib. Do not roll past a
  checkpoint on your own initiative.
- **Do not expand scope.** Build the task in front of you. A "while I'm here" refactor of an
  unrelated subsystem is out of scope — note it for later instead.
- **Do not turn UI work into backend rewrites.** Phase 11 is a UI/UX + product-surface phase on a
  strong backend. If a screen seems to "need" a schema change or a new store, that is a signal to
  stop and re-scope with Habib — not to quietly rewrite the backend. The one backend stretch
  (migration v9 + search/artifacts) is done and behind Checkpoint E; later tasks should ride it,
  not reopen it.
- **Per-task commits, explicit paths.** One task = one focused commit (plus a separate cassette
  commit only if a legitimate scenario-adjacent change forces a re-record). Phase 11 also used a
  Claude co-author trailer; that phase-specific convention is not current policy.

## 2. Phase 11 safety invariants (snapshot)

- **The UI adds NO new authority.** It reads and navigates. Every write / generation / action goes
  through the **existing** Gate/turn/mutation routes. A new screen never reaches a tool, executor,
  or file write directly.
- **Reads/navigation are immediate; writes/generation/actions go through the Gate/turn path.** At
  Phase 11 close, the command palette and search **navigated and used GET only**. Later phases
  superseded that palette rule with a test-pinned, exact mutation allowlist.
- **The mutation-route closed set is a pin.** Phase 11 closed at **30**
  (`test_ui_readmodels.py::test_mutation_route_closed_set`); the current value lives only in that
  test. Any new mutation must be metadata-class, mirror an existing one
  (e.g. `sessions/{id}/pin`), and be added to that set in the same commit. No generic key-value or
  "run anything" route. No eval-run route (the eval chip stays copy-command — ADR-0005).
- **The secret sweep stays intact.** `test_no_secret_crosses_the_wire_on_any_get` auto-covers new
  non-parameterized GETs; **you must add a manual sweep test for every new parameterized GET**
  (it skips `{param}` paths). No secret, token, or session id ever crosses the wire.
- **Appearance is client-side only** (localStorage). Do **not** add a server theme/density/settings
  mutation route — that would be new authority.
- **Amber = attention/decisions only.** The Gate approval flow (nonce + live heartbeat) is unchanged
  and reachable from every screen. Debug/trace is presentation-only and default-hidden.
- **No external resources** in any UI asset (no CDN/font/remote fetch); the only allowed literal URL
  is the `http://www.w3.org/` SVG namespace. Untrusted strings (transcripts, digests, memory, search
  snippets, artifact titles/content) render via `textContent`/escaped paths only — never raw HTML.

## 3. Search / artifacts invariants (from Checkpoint E — do not regress)

- **Project scoping lives in SQL / the JOIN, never in `MATCH`.** `query_domain` + each domain's
  scope clause are the single source of truth. A project-B query must never return a project-A row.
  Adversarially pinned (`test_fts_index.py`, `test_search_service.py`) — keep those green.
- **Snippets only.** Never return a full body over the wire. Chat content (JSON blocks) is projected
  to plain prose (tool_use / thinking / tool_result stripped), capped, whitespace-collapsed.
- **Quarantined content is never searchable or servable.** Unreviewed meeting transcripts (ADR-0004)
  are excluded from search in SQL and refused by the content route. Do not add a producer that
  indexes unreviewed content as a discoverable/servable artifact.
- **The content route never leaks paths or secrets.** `serialize_artifact` omits the raw
  `local_path` (ships `has_content`). `/api/artifacts/{id}/content` serves only a registered id, via
  `ArtifactStore.content_path` (re-confines to a managed root + refuses sensitive paths), with a
  fixed text/image media allowlist (never sniffed), size-capped. Never introduce a path- or
  id-from-body file read.
- **`content_hash` is a non-unique version fingerprint**; artifact identity is
  `(origin_type, origin_id)`. Producer hooks are guarded (`if self.artifacts`) and **fail-soft** — a
  bookkeeping-artifact failure must never break or raise out of its producer.

## 4. UI / UX discipline

- **`design/` was READ-ONLY visual direction.** Phase 11 used a transient external prototype and
  design assets that are no longer present in this repository. They were reference-only and their
  heavy PNGs were never shipped; backgrounds use CSS gradient veils and system fonts.
- **Premium but calm.** One primary attention surface per screen, ordered by priority. Amber only for
  decisions; cost is teal monitoring (present, not stressful). Motion is subtle and respects
  reduce-motion.
- **Debug/trace hidden by default**, presentation-only (no capability keys off it).
- **Nothing empty.** Every screen has a designed empty state that teaches the next action.
- **Tokens, not styles.** All appearance (theme light/noir/neon, density, accent, bg-intensity,
  motion, layout) is CSS custom properties + `data-*` + localStorage. No per-screen hex.

## 5. Vanilla modular frontend discipline

- **Keep it vanilla** — ES modules + plain CSS, **no framework, no build step, no bundler.**
- **Split, don't grow.** Do not pile everything into `app.js`. Introduce/maintain the leaf
  `static/ui/` layer (`dom.js` with `esc`/`escAttr`, `components.js`, `theme.js`, `format.js`,
  `bus.js`, `keys.js`, `palette.js`); keep screens thin; one `workspace.js` orchestrates its tab
  panels. Strict import DAG: `ui/* ← screens/* ← app.js`.
- **One escaping helper, quote-safe.** Use the shared `esc`/`escAttr` from `ui/dom.js` for ALL dynamic
  interpolation; delete duplicated copies as you migrate a screen. Never interpolate untrusted text
  into an attribute without `escAttr`.
- Load-bearing shell pieces (approval modal + nonce flow, `setSurface` tracking, `renderRunnerState`,
  the WebSocket heartbeat) keep their semantics — refactor around them, don't rewrite them.

## 6. Phase 11 eval / cost discipline

- **Keyless replay is the default gate.** `uv run kira eval gate --suite core` is $0 and must be
  green after every task. A missing cassette fails closed (never a silent live call).
- **No unnecessary live/record runs.** `--live` / `--record` cost real money and are explicit,
  capped (`--max-cost-usd`), and reserved for a legitimate scenario change or the phase-closeout
  ritual. **Do not launch `--record` casually.** If a scenario-adjacent change requires re-recording,
  do it once, capped, sweep, and commit cassettes in a dedicated commit — do not change eval
  scenarios in Phase 11.
- No eval scenario changes are expected in Phase 11; the live judged gate is a terminal ritual on
  Habib's machine (chunked; background runs die ~14 min).

## 7. Phase 11 subagent / orchestration discipline

- **Use subagents for scoped review and discovery**, not for editing the working tree in parallel.
  Good uses: a parallel "understand" pass that maps the exact code a task will touch; an adversarial
  review panel over a finished diff before committing.
- **Avoid overlapping edits.** Do not have two agents (or an agent and yourself) editing the same
  frontend/backend file concurrently — the implementation is sequential and stateful (each task
  leaves the suite green and is committed). If isolated parallel edits are truly needed, use worktree
  isolation; otherwise serialize.
- Adversarially verify findings (independent reviewers, default-to-refute) before trusting them; fix
  every confirmed finding before the commit.

## 8. Phase 11 forbidden files (historical)

During Phase 11 work, agents were instructed never to modify or commit:

`docs/PLAN.md` · `docs/PLAN-7-voice-consent-checkpoint.md` · `mcp_sample.json` ·
`config/settings.yaml` · `config/permissions.yaml` · `design/`

Stage only the explicit paths a task changed. Never `git add -A`/`git add .`.
