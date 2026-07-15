# 00 — Executive Summary: Frontend↔Backend Parity Audit

> **Historical design record.** This audit preserves the names, paths, counts, and line references
> used at its recorded snapshot. It is evidence, not current Kira implementation status; see the
> [documentation index](../README.md).

Read-only audit of the working tree at historical HEAD `98c4ebc` (including uncommitted changes), 2026-07-12. Full evidence in [01](01-backend-capability-inventory.md) (capability inventory), [02](02-frontend-backend-parity-matrix.md) (parity matrix), [03](03-user-workflow-analysis.md) (workflows/IA), [04](04-implementation-roadmap.md) (roadmap), [05](05-test-and-observability-gaps.md) (test gaps). Every claim there carries `path:line`; orphan-route claims were re-verified by direct grep at that baseline.

## Current status — 2026-07-12

The detailed audit below is retained as the baseline evidence, not as a live product-status
report. Current HEAD `e78cfcd` is fourteen commits beyond `98c4ebc`; where this addendum conflicts
with a finding or roadmap item below, this addendum is current.

### Landed since the audit

- Workspace capability truth, read-route confinement, per-turn cancellation, scoped activity,
  attended task/memory/project controls, sub-agent progress, task history, notices, connector
  audit, project metadata, and federated palette search are live (`a02ff29` through `fe5c8f2`).
- Project chat/Vault now use verified, reviewed, one-hop project-local dependency evidence;
  readiness and the review queue are correctly scoped, and graph-edge filtering is bound
  correctly (`6105969`).
- The route-consumption contract prevents new backend routes from becoming silently orphaned;
  heartbeat/runner reads, visible-surface refresh, shared escape helpers, dead UI cleanup, and
  Vault CSP compliance have also landed (`acaf45b`, `3f1065e`, `9b156a5`, `2bf532f`, `e78cfcd`).

### Still open or deliberately deferred

- The uncommitted `web_search: allow` policy rewrite is an authority decision, not part of this
  work. HEAD remains `ask`; retaining `allow` needs an explicit product decision and visible
  Settings/docs posture.
- NotificationRouter production wiring, saved-view/budget-product decisions, graph freshness,
  legacy route removal, and broader stale-prose/schema cleanup remain open.
- `/api/agents` is intentionally not browser-exposed yet; the visible evidence surfaces are
  scoped task history, notices, connector writes, and attended progress. Federated search is
  live; graph-entity focus is a separate product decision.

## Is the frontend actually connected to the backend?

**Yes at the core, with real integrity — and increasingly frayed at the edges.** The main loop (turn → streamed events → nonce-gated approvals → persisted transcript → rehydration), workspace isolation, orchestration runs, the vault review queue, the intent queue, and the graph views are genuinely wired, correctly scoped, and pinned by strong tests (test_ui_workspaces.py:293-488, test_ui_approvals.py:121-251). No frontend call targets a nonexistent route.

But the connection quality drops sharply one ring out: **16 of ~90 routes (10 of the 47 pinned mutations) have zero frontend consumers**, several flagship backend capabilities are UI-invisible (federated search, task creation, run history, graph curation), one read model violates its own consistency promise, and the working tree contains an unannounced approval-posture change. The pattern across recent phases: backend capabilities land complete with routes and tests; the last mile into the browser ships partially or not at all, and nothing fails when that happens (no route-consumption test exists — see 05-T1).

## Top 10 findings by user impact

1. **"Stop" is a sledgehammer; the per-turn brake is unwired.** The only visible stop control calls `/api/runner/pause`, cancelling *every* workspace's turn and halting background jobs; `POST /api/turn/cancel` exists and nothing calls it (app.js:609; server.py:418,604-620).
2. **Capability truth can contradict itself.** Hub/Settings compute `exposed_to_chat` without the workspace's registered tools while Daily/header get exact truth — the read model's own docstring promises they "can never disagree" (server.py:652,667 vs 731; readmodels.py:1330-1350).
3. **Working tree silently flips `web_search` from ask to allow.** An egress tool now runs with no prompt while README/UI education still describe ask-first; nothing in the UI surfaces non-default gate policy (config diff; README.md:424 area).
4. **Federated search is built and unreachable.** `GET /api/search` spans 8 FTS domains (messages, memories, KB, tasks, runs, digests, artifacts, graph); the palette only queries `/api/graph/search` — chats-by-content, tasks, and digests are unfindable (server.py:1960; palette.js:212).
5. **The task loop dead-ends twice.** Tasks can't be created from the UI (`POST /api/tasks/create` orphaned) and the new follow-up action items are inert planning text with no promote-to-task button — the flagship task→run→review→follow-up cycle never closes (server.py:1407; engine.py:188-213; workspace/tasks.js:31).
6. **Delegation goes dark in the thread.** `subagent_event`/`subagent_completed`/`tool_finished` frames are delivered and dropped by the conversation renderer; users watch a frozen chat during sub-agent work (session.py:129-142; conversation.js:237-253).
7. **The evidence layer is persisted and invisible.** Task run history (`/api/tasks/{id}/runs`), sub-agent history (`/api/agents`), notice history (`/api/notices`) are orphaned routes; the `connector_writes` journal has no read model at all (server.py:776,993,634; migrations v10).
8. **Workspace Vault tab contradicts itself.** Its readiness card is project-locked while its file tree fetches the *active* project's knowledge ambient and filters client-side — a non-active project shows "N files" next to an empty tree (workspace/vault.js:11,39).
9. **Scoping is two systems pretending to be one.** Sibling routes enforce workspace locks (`/office`, `/graph`) while `/api/workspace/{id}` and `/activity` check nothing; ten other GETs trust `?project_id=`; `/api/agents` is fully global (server.py:2017-2039,993).
10. **Config and code promise behavior the runtime doesn't perform.** `NotificationRouter` (urgent push, quiet hours, per-project mute) is never instantiated — its config keys are inert; `in_flight` on `/api/runner` is always null in production; `POST /api/budgets` mutates an in-memory copy that resets on restart (routing.py:84-122; server.py:438,507-510).

## Keep / Fix / Remove / Build next

**Keep** — the security spine exactly as is: nonce+liveness approvals, workspace-scoped WS delivery, origin/host guards, secret-absence sweeps, the mutation-route pin, bodies-free read models, honest MCP/"future phase" copy. Also keep: Studio/Office live views, the chat lifecycle, the new code-map graph view, quarantine review flows.

**Fix (small, high-confidence)** — capability_truth workspace pass-through; vault-tab scope; composer Stop → turn/cancel; sub-agent lines in the thread; follow-up→task promote button; palette on `/api/search`; task-run history + notices feed; project rename; workspace/{id} scoping parity; heartbeat interval leak; error-vs-empty rendering; the esc()/helper dedup.

**Remove (or consciously decide)** — `/api/voice/listen` (superseded), `/api/artifacts/{id}` and `/api/intents/{id}` (unused detail routes), saved-views routes+row (inert end-to-end: render-only, no create/delete UI), `POST /api/budgets` (ephemeral), `in_flight`, `graph_rebuilt` (hardcoded false), dead `ui/components.js` + dead element writes (app.js:517-518), stale pin-count prose (server.py:2126; ADR-0022:55-57).

**Build next (backed by existing backend)** — the evidence/Activity layer (all four stores already persist it); `connector_writes` audit read model; skill-pack status surface before `skills.mode` leaves `off`; graph-staleness indicator; NotificationRouter wiring behind its own checkpoint — or explicitly mark its config inert until then.

## Most Valuable Next 3 Changes

1. **Close the work loop** — follow-up→task promotion + task run history (04 P1-1, P1-3). Two small UI additions on existing routes convert Studio results and the scheduler from disconnected read-outs into the product's core cycle.
2. **Make Stop mean stop** — wire the composer to `POST /api/turn/cancel` (04 P0-3). The worst trust/ergonomics mismatch in the app; roughly a one-day fix.
3. **Eliminate self-contradiction** — capability_truth workspace pass-through + vault-tab scope fix (04 P0-1, P0-2), then pin both with tests 05-T5/T4 so the class of defect can't return.

And one meta-change that outweighs any single fix: land **05-T1 (route-consumption manifest test)**. The orphan-route class grew to 16 silently because nothing fails when the last mile doesn't ship; a 60-line pin makes that impossible from now on.
