# 03 — User Workflow Analysis

How the app's screens map to what a user is actually trying to do, where handoffs break, and an information architecture that fits the code that already exists. No invented redesign; every proposal is backed by an existing route, store, or read model (citations throughout).

## 1. Is the UI organized around user goals or backend modules?

Mixed. The rail (index.html:13-47) is closer to backend modules than to goals: Chat (loop), Daily (digest+read models), Projects (projects store), Studio (orchestration), Artifacts (artifact store), Knowledge (KB), Costs (ledger), Notifications (attention), Connectors (hub), Settings, Meetings (voice). Three surfaces then re-slice the same stores a second and third time: global screens, workspace tabs (workspace.js:9-14), and the Chat "Library" shelf (chat.js:451-489). The result is that a single concept — e.g. "the project's files" — exists in three renderings with two scoping models (ambient vs explicit `?project_id=`, agent-B §6), and they can disagree (workspace/vault.js:11,39 vs `/api/vault?project_id=`).

The strongest goal-oriented surface is the conversation header + palette (PLAN-15.5 §5.1/5.5): scope, model, mode, capabilities in one place, actions one keystroke away. The weakest is everything downstream of *work being produced*: results, follow-ups, history, and failures are scattered or invisible (§3 below).

## 2. Current end-to-end workflows, traced

### W1 — Ask → act → approve → result (the core loop) — WORKS
`POST /api/turn` (server.py:392) → loop events streamed scoped (session.py:222; connections.py:133-144) → approval modal with nonce + liveness (approver.py:101-196) → transcript persisted (sessions, v1) → rehydration on reload (app.js:550-558).
Breaks at the edges:
- **No per-turn brake.** Stop = `/api/runner/pause` = cancel ALL workspaces' turns + halt background jobs (app.js:609; server.py:604-620). `POST /api/turn/cancel` exists unconsumed (server.py:418).
- **Delegation goes dark.** `subagent_event`/`subagent_completed`/`tool_finished` frames arrive and are dropped by the thread renderer (conversation.js:237-253; only trace.js:31-35 shows them). A user watching a delegated task sees a frozen chat.
- **Turn cost cap** ends a turn with `stop_reason="cost_cap"` (agent.py area, per audit) — the UI renders the assistant note but nothing explains the cap or where to raise it (settings display is read-only; `POST /api/budgets` orphaned+ephemeral, server.py:507-510).

### W2 — Ingest knowledge → review → retrieve — WORKS, with a scope seam
Upload/folder import (server.py:1249) → quarantine review (vault.js:67-68) → cited retrieval in chat (knowledge/service.py:489) → NEW dependency excerpts (service.py:518,571) and readiness card (readmodels.py `vault_overview`).
Breaks:
- Workspace Vault tab's file tree uses ambient `/api/chat/knowledge` while its readiness card is explicitly scoped — non-active project shows contradictory halves (matrix C).
- Graph staleness after single-file approvals (rebuild only on folder finalize/reject — server.py:1282,984); the UI's only remedy is a clipboard hint (graph.js:60-62).
- Wiki pages are written by the agent (`write_wiki_page` tool) but there is **no wiki reading surface anywhere** in the UI; lint output is a raw JSON dump (vault.js:79).

### W3 — Task → research → implementation → review → approval → evidence/history (the flagship loop) — BROKEN IN FOUR PLACES
The backend has every stage; the UI connects them one-way and drops the loop's ends:
1. **Task creation**: only by asking the agent in chat; `POST /api/tasks/create` orphaned (server.py:1407). Tasks screen is list/cancel only (tasks.js:6,28).
2. **Research/implementation**: Studio runs work well (estimate→confirm→run, studio.js:145-152), but starting a run from a *task* or from a *chat conclusion* requires manual re-typing of the brief; the palette's "Run Workflow" merely navigates (palette.js).
3. **Review→follow-ups**: verdict, rationale, findings and action_items are rendered (studio.js, workspace/tasks.js:31) but action items are inert planning text (engine.py:188-213) with no promote-to-task button, despite `/api/tasks/create` existing. The loop dead-ends exactly where it should feed itself.
4. **Evidence/history**: task run outcomes (`/api/tasks/{id}/runs`, orphaned), sub-agent history (`/api/agents`, orphaned), notices history (`/api/notices`, orphaned), connector-write journal (`connector_writes`, no read model at all — migrations v10) — the "what actually happened" layer is persisted and invisible.

### W4 — Approvals & attention — WORKS, semantics muddy
Live ASKs, write intents, graph suggestions, durable items in one queue (readmodel.py:52-152; gate.js). Two-lane semantics (resolve = metadata clear vs approve/reject = real authority, gate.js:99-113) are not explained on screen; the Phase-16 screen has zero tests of any kind (agent E §6). Notification routing (urgent push/quiet hours) is configured but UNWIRED (routing.py:84-122) — a user setting quiet hours changes nothing.

### W5 — Cost & trust — mostly works
Costs/ROI screens are honest (unpriced distinct, readmodels.py:253,524). Gaps: budget editing route ephemeral+orphaned; `capability_truth` disagreement between Hub/Settings and Daily/header (matrix C, P0); working-tree `web_search: allow` silently removes an approval prompt the docs still promise (matrix C, P0); egress has no UI audit view (logs only, egress.py:23).

### W6 — Voice — works when enabled, one dead limb
Push-to-talk and dictation → `/api/voice/utterance` (voice.js:75-76), TTS captions, meeting capture to quarantine. `/api/voice/listen` (server-mic) is orphaned; meeting→artifact promotion is unreachable code (meeting.py:86-106).

### W7 — Graph memory — read works, curation is a CLI cliff
Subgraph/node/search/suggestions all live. Merge/split/undo/dedup/export/rebuild are CLI-only by design (cli/graph.py); the UI neither exposes them nor tells the user they exist, except one copy-паste hint.

## 3. Disconnected islands

- **Trace & Lab** — debug-gated (app.js:409), fed by a ring buffer / files; no links in or out.
- **Meetings** — its own screen with one button; output lands in Vault review with no cross-link from Meetings to the created source.
- **Notifications (gate.js)** — well-fed by the queue, but nothing routes *to* it contextually (e.g., a `budget_stop` in chat doesn't link to the queue or costs).
- **Saved views/Collections row** — renders data that no UI can create (matrix A).
- **The three renderings of "project content"** (global screens / workspace tabs / chat Library) — no shared state, no cross-navigation consistency, duplicated code (esc ×3, openArtifact ×2, readiness ×2).

## 4. Missing-but-warranted vs present-but-questionable

**Backend features that should be visible and are not** (all have working routes/stores today): federated search; task creation + run history; follow-up promotion; notices history; sub-agent history; project rename; digest refresh. (Citations in 01/02.)

**Present but redundant/misleading/premature:**
- Saved-views Collections row (inert end-to-end).
- `in_flight` field and the dead `composer-model`/`chat-mode` writes (app.js:517-518).
- Server-mic voice route.
- The Chat Library's "Outputs" tab label (project-scoped, not chat-scoped, server.py:825-844).
- Two token vocabularies in CSS (legacy alias layer kairo.css:39-55) and inline-style screens — premature visual debt rather than features.
- MCP row in Hub is honest ("future phase", readmodels.py:1122) — keep.

## 5. Information architecture proposal (grounded in existing capabilities)

Keep the shell (rail + workspace + palette). Reorganize around four user questions, using only data that already has routes:

1. **"What needs me?"** → Notifications stays the single queue; add the notices feed (`/api/notices`, exists) and label the two lanes (clear vs approve). Badge logic already exists (app.js:277).
2. **"What is happening / what happened?"** → one **Activity** concept: merge the workspace Activity tab (activity_feed, readmodels.py:386) with task runs (`/api/tasks/{id}/runs`), sub-agent runs (`/api/agents` + project scoping first), orchestration runs, and connector-write journal (needs a small read model over `connector_writes`, v10). This is the evidence/history layer W3-4 lacks — every piece is already persisted.
3. **"Work with the agent"** → Chat + Studio converge: follow-ups get a promote-to-task action (`/api/tasks/create`); a task row gets "start a run" prefilling Studio (client-side prefill already exists via hash args, studio.js:43). Sub-agent progress renders inline in the thread (events already delivered).
4. **"Find anything"** → palette switches to `/api/search` (federated, 8 domains) with graph entities as one result kind; result routing table already exists (palette.js:34-45).

De-duplicate the three project-content renderings by making the workspace tabs the canonical implementation and having the Chat Library shelf embed the same modules (workspace/* panels already take an explicit ctx — _util.js pattern), rather than a third copy.

Naming: pick one user-facing word per concept — "Notifications" (drop "Gate" from the palette), "Knowledge" (drop "Vault" from H1s) — pure label edits (index.html:26,32; vault.js:13; palette.js:23,27).

## 6. Highest-leverage workflow gaps (ranked)

1. Follow-ups → tasks promotion (closes W3's loop with one existing route).
2. Palette on federated search (unlocks chats/tasks/artifacts/digests findability).
3. Per-turn Stop (right-sized brake; route exists).
4. Sub-agent visibility in the thread (events already arrive).
5. Evidence layer: task runs + notices history + connector-write journal read model.
6. Vault-tab scope fix (turns a contradictory screen into a trustworthy one).
