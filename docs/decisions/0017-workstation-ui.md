# ADR-0017: Kairo Workstation — product surface with zero new authority

- **Status:** Accepted; implemented in Phase 11 (Tasks 1–15)
- **Date:** 2026-07-08
- **Context phase:** Phase 11 (Workstation UI/UX + product surface)

## Context

Kairo's backend was strong but the product surface was the weakness: Projects was a bare switcher,
chats had full server support but no screen, there was no artifacts store, no global search, one
hardcoded theme, and Daily was good but not a command center. Phase 11 makes the workstation feel
premium, project-first, and searchable — the hard constraint being that a UI is the easiest place
to accidentally grant authority (a stray write route, an injected script from untrusted content, a
leaked path or key). The decision below is how we added a large surface while adding **no new
authority**.

## Decision

### 1. The UI reads and navigates; every write goes through the pre-existing gated routes

The frontend carries no enforcement. Reads and navigation are immediate; every write/generation/
action goes through the existing PermissionGate/turn routes. The set of state-changing routes is a
**closed, test-pinned set** (`test_mutation_route_closed_set`) — Phase 11 added exactly the
metadata mutations it needed (project pin/label, artifact pin/label, saved-view save/delete) and
nothing that reaches a tool/executor. The command palette and every read screen are GET/navigate-
only (pinned); a "write"-flavoured entry navigates to the surface that owns the write.

### 2. Untrusted content is rendered as text, never as markup

All cross-project / external content (chat titles, memory, artifact titles + bodies, digest text,
commit subjects, run summaries, connector names) reaches the DOM only via `textContent` / the
shared `el()` builder (string children → `createTextNode`) / a same-origin `<img>`. No screen
interpolates untrusted data into `innerHTML`; the one shared escaper (`ui/dom.js` `esc`/`escAttr`)
is the single place escaping lives. A digest/artifact `external_uri` is shown as text and is never
opened or linkified (it would be a phishing/exfil surface). CSP stays `default-src 'self'` (no
inline handlers, no eval, no external resources — SVG brand marks are same-origin assets).

### 3. Artifacts: identity, confinement, and a hardened content route

An artifact's identity is `(origin_type, origin_id)`; `content_hash` is a non-unique version
fingerprint. A row is either a confined local file (`local_path` under a managed root, sensitive-
path-refused at registration) XOR an `external_uri` deep link. File bytes are served ONLY through
`/api/artifacts/{id}/content`: registered-id-only, quarantine-refusing, path-re-confined, a
text+image media allowlist (no html/svg/js), size-capped, `nosniff`+CSP applied — so a served body
can never execute. Producers (digest/orchestration/wiki/meeting/eval-report) register fail-soft and
guarded; the meeting hook indexes reviewed transcripts only.

### 4. Search + cost + status are metadata read models; scope lives in SQL

Federated search is FTS5 external-content tables; scope/visibility/quarantine are enforced in the
query JOIN, never in the MATCH (injection-proof sanitiser). Snippets only. The Cost Center and the
Settings status sections are pure read models over the ledger / hub status — presence booleans,
never a key value (the secret-absence sweep covers every non-parameterised GET). ROI and the
per-project health chips are small aggregates that degrade independently.

### 5. Appearance is client-side; there is no server theme route

The token system (`ui/theme.js`) persists theme (noir/light/neon), density, layout, motion, and
accent to `localStorage` and applies them to `<html>` — so `<body>` stays plain and appearance
grants no authority. A compat shim aliases the old obsidian token names to the active theme so
every existing screen became theme-aware without a rewrite. The debug/trace toggle is a
presentation-only body class.

### 6. Vanilla, modular frontend — no framework, no build, no CDN

Hand-written ES modules: `ui/*` leaf helpers ← `screens/*` ← `app.js` shell. One keyboard
dispatcher (`ui/keys.js`), one event bus (`ui/bus.js`), a lazy per-tab Workspace. The rail is a
primary area (Daily · Projects · Studio · Costs · Settings) + a utility area; Vault/Tasks/Memory
live as Workspace tabs. The desktop-first shell was made responsive (status bar wraps, main column
`minmax(0,1fr)`, rail collapses to an icon strip ≤720px) with a `scrollWidth ≤ innerWidth` +
bounding-rect no-overlap assertion pinned by the T2 screenshot harness across 1440/1024/390 × three
themes.

## Consequences

- The palette/GET-only + closed-mutation-set + textContent invariants are enforced by tests, not
  convention — a future screen that mutates outside the set, or renders untrusted HTML, fails CI.
- Amber stays reserved for the single primary attention surface (approvals/review); cost is teal
  monitoring; identity/active is accent. This palette discipline is a review checkpoint.
- Message editing/regenerate/branching, an office-style team view, semantic search, connector
  writes, and a fuller mobile nav are explicit non-goals / later phases (the Activity feed is the
  Phase-14 office substrate).

Relates to ADR-0008 (UI transport/auth core), ADR-0011 (project scoping), ADR-0013/0015 (cost
ledger / team tool intelligence — the Cost Center + Studio surfaces).
