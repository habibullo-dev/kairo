# 04 — Implementation Roadmap

Prioritized from the parity matrix (02). Each item: files, work required, dependencies, risk, acceptance criteria. Mutation-pin note: any new/removed POST route must update the closed-set literal in `tests/unit/test_ui_readmodels.py:136-209` in the same diff — that is the repo's own rule.

Split per instructions: **[FIX]** = small, high-confidence; **[DECIDE]** = requires a product decision first.

---

## P0 — broken trust, broken connectivity, mis-scoped behavior

### P0-1 [FIX] capability_truth actually never disagrees
- **Files**: `src/kira/ui/server.py:643-668` (hub, settings handlers), `src/kira/ui/readmodels.py:1330-1338`; test in `tests/unit/test_ui_readmodels.py`.
- **Work**: resolve a workspace in `/api/hub` and `/api/settings` the same way `/api/daily` does (server.py:731) and pass `registered_tools` into `_capabilities`; keep the current fallback for the legacy composition.
- **Dependency**: none. **Risk**: low — read-only paths.
- **Accept**: new pytest: with a tool-less loop, `exposed_to_chat` is `false` on all four routes simultaneously; grep test that `_capabilities()` is never called without a workspace under the workspace composition.

### P0-2 [FIX] Workspace Vault tab renders one scope, not two
- **Files**: `src/kira/ui/server.py:861-991` (`/api/chat/knowledge`), `src/kira/ui/static/screens/workspace/vault.js:11,39`.
- **Work**: accept `?project_id=` on `/api/chat/knowledge` with the same workspace-mismatch 404 used by `/api/vault` (server.py:783-800); workspace tab passes its ctx project explicitly; delete the client-side filter.
- **Dependency**: none. **Risk**: low; watch the chat Library shelf which uses the ambient form (chat.js:424) — keep ambient default.
- **Accept**: unit test: bound workspace + foreign `project_id` ⇒ 404; DoD `workspace-vault` state unchanged; tree and readiness card agree on file count for the same project.

### P0-3 [FIX] Per-turn Stop in the composer
- **Files**: `src/kira/ui/static/ui/*` (composer area in `conversation.js`/`chat.js`), `app.js:609` (keep pause as global control in Daily/status bar only).
- **Work**: while `turn_busy`, composer shows Stop → `POST /api/turn/cancel` (route exists, server.py:418); `turn_cancelled` handler already renders the note (app.js:202-208).
- **Dependency**: none. **Risk**: low.
- **Accept**: pytest for the route already-exists behavior + new WS `turn_cancelled` contract test (05); manual: cancel mid-stream leaves runner running and other workspaces untouched.

### P0-4 [DECIDE] `web_search: allow` (working tree) — keep or revert, then say it out loud
- **Files**: `config/permissions.yaml` (uncommitted), `src/kira/ui/static/screens/settings.js`, `docs/` (README approval-posture text).
- **Work**: product decision first: silent web search is a real posture change (egress without prompt; taint demotion still applies — core/agent.py:624-722). Whichever way: Settings should render per-tool gate policy with non-default values flagged (`/api/gate/policy` exists, server.py:382, currently debug-only in gate.js:206).
- **Risk**: none technical; trust risk if left silent.
- **Accept**: Settings shows "web_search — allow (changed from default ask)"; docs match config.

---

## P1 — high-value backend features unreachable or unusable

### P1-1 [FIX] Promote a follow-up action item to a real task
- **Files**: `src/kira/ui/static/screens/workspace/tasks.js:31` area, `screens/studio.js` detail panel; server: none (`POST /api/tasks/create` exists, server.py:1407).
- **Work**: "Schedule…" button per action item → prefilled create call (kind=job/reminder, payload = item text + run link); visually mark follow-ups as planning notes vs scheduled tasks.
- **Dependency**: none. **Risk**: low — reuses an existing gated route; no new authority.
- **Accept**: clicking promote creates a `tasks` row (visible in list) carrying the source run id; follow-up chip switches to "scheduled ✓"; pytest on the create payload shape.

### P1-2 [FIX] Palette searches everything
- **Files**: `src/kira/ui/static/ui/palette.js:210-262`; server: none (`GET /api/search`, server.py:1960).
- **Work**: query `/api/search` (federated 8-domain FTS, persistence/fts.py) alongside or instead of `/api/graph/search`; extend KIND_ROUTE for `message/task/digest` results (chat→resume already exists via sessions path).
- **Dependency**: verify `/api/search` result shape → add contract test (05). **Risk**: low.
- **Accept**: palette finds a chat by message content, a task by title, an artifact by text; existing `test_ui_palette.py` recent-chats behavior unchanged.

### P1-3 [FIX] Evidence layer, part 1: task run history + notices feed
- **Files**: `screens/tasks.js` + `workspace/tasks.js` (expand row → `GET /api/tasks/{id}/runs`, server.py:776); `screens/gate.js` (+ a "Recent activity" section from `GET /api/notices`, server.py:634).
- **Dependency**: none. **Risk**: low. **Accept**: failed job shows its run row with status/error; last 50 notices listed with timestamps.

### P1-4 [FIX] Sub-agent progress in the chat thread
- **Files**: `src/kira/ui/static/screens/conversation.js:237-253`.
- **Work**: handle `subagent_event` (compact "↳ {title}: started/tool" line) and `subagent_completed` ("↳ {title} — {status}, ${cost}"); events already serialized (session.py:129-142) and delivered.
- **Risk**: low; render text via existing `el()`/textContent only. **Accept**: message_dod-style state showing a delegation sequence; no innerHTML additions.

### P1-5 [FIX] Lock `/api/workspace/{id}` + `/activity` like their siblings
- **Files**: `src/kira/ui/server.py:2017-2027`.
- **Work**: same `_workspace_for` + mismatch-404 used by `/office` (server.py:2027) and `/graph` (server.py:2039).
- **Risk**: low; legacy composition keeps current behavior. **Accept**: pytest parametrized over the four `/api/workspace/*` routes asserting uniform 404-on-foreign-project.

### P1-6 [FIX] Project rename/description from the UI
- **Files**: `screens/projects.js` or workspace Overview header; server: none (`POST /api/projects/{id}/update`, server.py:1587).
- **Accept**: rename persists across reload; grid + workspace header agree.

### P1-7 [FIX] Heartbeat interval leak
- **Files**: `src/kira/ui/static/app.js:130-139`.
- **Work**: store the interval id; clear on `onclose` before scheduling reconnect.
- **Accept**: reconnect N times ⇒ exactly one heartbeat timer (assert via counter in a DoD-style harness or code inspection note in review).

### P1-8 [DECIDE] NotificationRouter: wire or declare inert
- **Files**: `src/kira/attention/routing.py:84-122`, composition in `cli/repl.py`; config keys config.py:545-548.
- **Work**: this is the repo's class of switch that needs its own checkpoint (urgent push = unattended egress). Short-term honest fix: comment the config keys as "not yet wired" and hide any UI copy implying routing.
- **Accept (short-term)**: no config key promises behavior the runtime lacks.

---

## P2 — usability, IA, visual consistency

- **P2-1 [FIX] Attention two-lane labels** — gate.js: "Clear from list" vs "Approve & send"; one sentence of explainer. (matrix C)
- **P2-2 [FIX] Rename labels once** — "Notifications" everywhere (palette.js:23), "Knowledge" everywhere (vault.js:13, workspace.js:12 tab label); pure string edits.
- **P2-3 [FIX] Memory remember box** — memory screens → `POST /api/memory/remember` (server.py:1376); show returned dedup `action`.
- **P2-4 [FIX] Digest refresh button on Daily** — `POST /api/digest/run` (server.py:1347); disable while 409-busy.
- **P2-5 [DECIDE] Saved views: build manage UI or remove** — either save/delete affordances (routes exist) or drop the Collections row + both routes (pin shrinks by 2). Inert features erode trust.
- **P2-6 [DECIDE] Budgets: durable editing or explicitly read-only** — if editing: persist via settings write path (new work, product decision); if not: remove `POST /api/budgets` (pin −1) and label Settings budgets "configured in settings.yaml".
- **P2-7 [FIX] "Chat outputs" → "Project outputs"** label until artifacts carry a session FK (server.py:825-844 docstring).
- **P2-8 [FIX] Scope-check graph suggestion mutations** — server.py:2123-2137: verify suggestion.project against workspace before approve/reject.
- **P2-9 [FIX] Consolidate `/api/runner` fetches** — one cached fetch in app.js exposed via `api.state`; header/palette/settings/chat consume state (agent B §4 lists the 5 call sites).
- **P2-10 [FIX] esc()/helper unification** — all screens import `esc` from `dom.js`; delete vault.js:90 and lab.js:21 copies; fix tasks.js:3/memory.js:2 import chain. Security-adjacent dedup.
- **P2-11 [FIX] Auto-refresh on bus events** — workspace tabs and Vault/Tasks/Memory re-fetch on relevant WS kinds (`notice` task events, `event` turn completions) instead of manual re-entry only.
- **P2-12 [FIX] Error ≠ empty** — daily.js:214, header.js:25-32, settings.js: render a distinct "couldn't load" row on fetch failure.
- **P2-13 [DECIDE] Graph staleness surface** — show last-rebuild time (derivable: builder writes derived edges; store a timestamp) + keep the CLI hint; a gated rebuild route is new authority → checkpoint discussion.
- **P2-14 [FIX] Inline-style cleanup** — move studio.js/costs.js/settings.js inline styles into kairo.css token classes; workspace panels are the pattern to follow.

## P3 — cleanup and deferred polish

- Delete dead code: `ui/components.js`, `assets/kairo-mark.svg`, `chat.js:779-783 saveLabel`, app.js:517-518 dead setText ids. (Verify no dynamic references; all grep-clean today.)
- **[DECIDE]** Remove orphaned routes with no roadmap: `/api/voice/listen`, `/api/artifacts/{id}`, maybe `/api/intents/{id}` (keep if the connector-write audit view lands) — each removal shrinks the mutation pin/GET surface; do in one commit with the pin update.
- Sweep stale prose: pin counts (server.py:2126, test_office_layout.py:3,30, ADR-0022:55-57, PLAN-16:6,51, verification-15_5.md:40), stale comments (conversation.js:1-2, office.js:10,54).
- `graph_rebuilt` field: drop from the per-file `/api/chat/attachments` response (server.py:1322-1345) or wire it truthfully.
- Legacy CSS alias layer (kairo.css:39-55): migrate remaining `var(--amber)`-class references (gate.js:58 etc.) then delete aliases.
- Palette KIND maps: prune kinds the server never emits (verify against graph/search vocabulary first).
- `digests.project_id`, `kb_sources.mime`, `find_by_hash` (store.py:223): remove or implement — currently dead schema/code.

---

## Dependency graph (what blocks what)

- P0-1, P0-2, P1-5 are independent server fixes; land first (they change read-model/scoping semantics the UI work builds on).
- P1-2 (palette) depends on a `/api/search` shape contract test (05-T3).
- P1-1/P1-3/P1-4/P1-6 are independent UI-only; each touches one screen + zero/one route already pinned.
- P2-5/P2-6/P3 route removals must batch with mutation-pin updates.
- P1-8 and P2-13 route through the repo's checkpoint discipline (new authority class) — do not fold into ordinary UI work.

## Most Valuable Next 3 Changes (pragmatic, implementable now)

1. **Close the work loop: follow-up → task promotion + task run history (P1-1 + P1-3).** One button and one expandable row, both on existing routes, turn Studio results and the scheduler from disconnected read-outs into the task→run→evidence cycle the whole product is nominally about.
2. **Make Stop mean stop (P0-3).** Wire the composer to `POST /api/turn/cancel`. Today the only visible brake nukes every workspace's turn and the background runner — the single worst trust/ergonomics mismatch in the app, and it's a one-day fix.
3. **One truth for capabilities + vault scope (P0-1 + P0-2).** Two small server-side changes that eliminate the only places where two parts of the same UI can flatly contradict each other — cheap fixes with outsized trust payoff.
