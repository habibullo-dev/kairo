# 01 — Backend Capability Inventory

Read-only audit at working tree (HEAD `98c4ebc` + uncommitted changes to `knowledge/service.py`, `ui/readmodels.py`, `ui/server.py`, both `vault.js` files, `config/*.yaml`). Every claim cites `path:line`. Labels:

- **LIVE** — reachable and correctly consumed in the browser UI
- **PARTIAL** — some of the capability is in the UI, the rest is not
- **API-ONLY** — a route/read model exists; no frontend code calls it (verified by grep over `src/kira/ui/static/`)
- **CLI-ONLY** — reachable only from `kira …` / REPL commands
- **INTERNAL** — no route and no CLI; reached only by other code
- **DISABLED** — behind a default-off flag
- **UNWIRED** — code exists but nothing constructs/calls it
- **OBSOLETE/UNCLEAR** — dead or of doubtful purpose

## 1. Conversation & agent loop (`src/kira/core/`, `src/kira/ui/session.py`)

| Capability | Status | Evidence |
|---|---|---|
| Chat turn with streaming deltas, tool events, approvals | **LIVE** | `POST /api/turn` (server.py:392) → `UiSession.submit`; events `session.py:81-142`; consumed `conversation.js:237-253` |
| Per-turn cancel of a running turn | **API-ONLY** | `POST /api/turn/cancel` (server.py:418) — zero grep hits in static/; the UI "Stop" button instead calls `/api/runner/pause` (app.js:609) which cancels *everything* and halts the background runner |
| Per-turn chat cost cap ($0.75 default) + `budget_stop` | **PARTIAL** | enforced only for browser chat (`ChatConfig.hard_stop_usd_per_turn`, config.py:150; wired only in `build_ui_app`); surfaced as `chat_turn_budget_usd`/`last_turn_cost_usd` on `/api/runner` (server.py:432-505). Inert for REPL/voice/sub-agents — no UI states this scope |
| Compaction (summary + elided view) | **INTERNAL** (correct) | `agent.py:454,464`; persisted per session (migrations v2). No UI indicator that a session is compacted |
| Modes Plan/Approval/Auto | **LIVE** | `POST /api/mode` (server.py:530), header + palette (palette.js:86) |
| Egress-taint demotion (private read ⇒ egress ASK, non-persistable) | **LIVE** (behavior) / **PARTIAL** (explanation) | `core/agent.py:624-722`; approval modal hides "Always" when `persistable:false` (approver.py:121-170 pins). No UI explains *why* the Always button vanished |
| Model/effort/routing selection (Auto default, Anthropic-only manual) | **LIVE** | `/api/models` (readmodels.py:1230), `POST /api/model` (server.py:551), `POST /api/effort` (server.py:580); header.js:101,123 |

## 2. Sub-agents (`src/kira/agents/`)

| Capability | Status | Evidence |
|---|---|---|
| Spawn scoped depth-1 sub-agent (via `spawn_agent` tool) | **LIVE** (through chat) | service.py:216-301; child events → `subagent_event` WS frames (session.py:129-137) |
| Sub-agent run history (`agent_runs`, both trace ids, cost) | **API-ONLY** | `GET /api/agents` (server.py:993) — zero grep hits in static/. The only browser surface of child runs is transient Trace-screen text (trace.js:31-35); history is invisible |
| Sub-agent approval forwarding with run-scoped grants | **LIVE** | approver.py:253-270; modal labels child ASKs |
| `subagent_event` / `subagent_completed` / `tool_finished` rendering | **PARTIAL** | serialized (session.py:129-142) but `onConversationEvent` ignores them (conversation.js:237-253); visible only as raw lines on the debug Trace screen |

## 3. Orchestration (`src/kira/orchestration/`)

| Capability | Status | Evidence |
|---|---|---|
| Team runs (estimate → confirm → run → cancel → detail) | **LIVE** | routes server.py:1084-2199; studio.js:28-152; UI-only — no CLI exists for orchestration |
| Live lifecycle events (started/stage/agent/round/completed) | **LIVE** (Studio+Office) / **PARTIAL** (elsewhere) | engine emits (engine.py:266-277 area); consumed app.js:158-164, office.js:405; Studio live panel renders only when that screen is open with matching context (studio.js:96-97) |
| Follow-ups: `verdict_rationale`, `synthesis_findings`, `action_items` (v20) | **PARTIAL — display-only by design** | migration `_migrate_v20` (migrations.py:1047-1073); serialized readmodels.py:805-807; rendered studio.js (+57 lines), workspace/tasks.js:31. **No route promotes an action item to a scheduled task** even though `POST /api/tasks/create` exists (server.py:1407) |
| Per-member model attribution in run detail | **LIVE** | readmodels.py:837-851 |
| Roster overrides from project settings | **UNWIRED** (documented) | only `team_budget_usd` applied (teams.py:200-219) |
| `skills_manifest_json`, `context_manifest_json` on runs | **INTERNAL** (audit-only) | store writes (v19; store.py); not in `serialize_orchestration_run` (readmodels.py:790-816) |

## 4. Skill forge (`src/kira/skills/`)

| Capability | Status | Evidence |
|---|---|---|
| Hash-pinned skill packs compiled into member prompts (off/shadow/active) | **DISABLED + INTERNAL** | catalog.py:105-186; `skills.mode: off`, `enabled: []` (settings.yaml:84-87, working tree); no in-tree runtime pack (`config/skills/packs/` holds `.gitkeep`); **no CLI or UI to list/validate/inspect packs** — the only user-visible effect is a 400 on estimate/start if a pinned pack is invalid (ui/orchestration.py:167,215) |

## 5. Knowledge / Vault (`src/kira/knowledge/`)

| Capability | Status | Evidence |
|---|---|---|
| File/URL/note ingest; browser upload; folder import | **LIVE** | service.py:277,379,700; `/api/vault/ingest` (server.py:1199), `/api/chat/attachments` (server.py:1249) |
| Review queue approve/reject (quarantine) | **LIVE** | service.py:677-686; `/api/vault/sources/{id}/*` (server.py:1183-1191); vault.js:67-68 |
| Semantic query with cited untrusted excerpts (+ NEW dependency excerpts) | **LIVE** (via chat tool) | service.py:489,518,571 (working tree) |
| Project knowledge readiness card (working tree) | **LIVE** (new) | readmodels.py `vault_overview` readiness block (readmodels.py:693 area); duplicated markup in `screens/vault.js:16-24` and `workspace/vault.js:23-36` |
| Wiki page write + lint | **PARTIAL** | `write_wiki_page` tool + `/api/vault/lint` (server.py:801) — lint renders as a raw JSON dump (vault.js:79); no wiki browsing surface at all |
| KB `rebuild_index`, interactive review walk, folder ingest command | **CLI-ONLY** | REPL `kb rebuild|review|ingest` (repl.py:711) |
| `find_by_hash` | **OBSOLETE** | store.py:223 — no caller in `src/kira` |
| `kb_sources.mime` | **OBSOLETE/UNCLEAR** | column never populated by `add_source` |

## 6. Projects & workspaces (`src/kira/projects/`, `src/kira/ui/workspaces.py`)

| Capability | Status | Evidence |
|---|---|---|
| Create/select/archive/pin/label; grid with health chips | **LIVE** | routes server.py:1569-1705; projects.js |
| Rename/description/color/icon update | **API-ONLY** | `POST /api/projects/{id}/update` (server.py:1587) — zero grep hits; the grid offers pin/label/archive only (projects.js:54,71,121). A project can never be renamed from the UI |
| Per-project service narrowing (narrow-only) | **API-ONLY** | `POST /api/projects/{id}/services` (server.py:1705) — zero grep hits; no settings surface offers it |
| Project memory export/import (Markdown) | **CLI-ONLY** | export.py; REPL `project export|import` (repl.py:629); deliberately tool-unreachable |
| Workspace isolation (server-owned context, scoped WS delivery) | **LIVE** | workspaces.py:201-213; connections.py:133-163; pinned by test_ui_workspaces.py:293-488 |
| Per-project `settings_json["model_routes"]` | **UNWIRED** | documented registry.py:4,77; nothing reads it; engine always built with `project_routes=None` (repl.py:1491) |

## 7. Scheduler / tasks (`src/kira/scheduler/`)

| Capability | Status | Evidence |
|---|---|---|
| List + cancel tasks | **LIVE** | `/api/tasks` (server.py:764), cancel (server.py:1360); tasks.js:6,28 |
| **Create** a reminder/job from the UI | **API-ONLY** | `POST /api/tasks/create` (server.py:1407) — zero grep hits. Tasks can be created only by asking the agent in chat (`schedule_task` tool) |
| Task run history | **API-ONLY** | `GET /api/tasks/{task_id}/runs` (server.py:776) — zero grep hits; job success/failure history invisible in UI |
| Digest run-now | **API-ONLY** | `POST /api/digest/run` (server.py:1347) — zero grep hits; Daily shows the digest but offers no refresh action |
| Runner pause/resume (emergency stop) | **LIVE** | server.py:604-630; app.js:609-610 |
| Background notices (reminder fired, job ok/failed) | **PARTIAL** | NoticeBoard → WS `notice` broadcast (notices.py:56) renders only as the single latest line on Daily (daily.js:151); `GET /api/notices` (server.py:634) has zero frontend consumers — no notice history view |

## 8. Memory (`src/kira/memory/`)

| Capability | Status | Evidence |
|---|---|---|
| List + forget memories | **LIVE** | `/api/memory` (server.py:752), forget (server.py:1368); memory.js:5,24 |
| **Remember** (save a memory with dedup adjudication) from UI | **API-ONLY** | `POST /api/memory/remember` (server.py:1376) — zero grep hits; saving a memory requires asking the agent in chat |
| Auto-recall context per turn | **INTERNAL** (correct) | service.py:193; recalled block is invisible in UI (by design; no indicator either) |

## 9. Memory graph (`src/kira/graph/`)

| Capability | Status | Evidence |
|---|---|---|
| Project subgraph + node cards + entity/unified search | **LIVE** | service.py:164-326, search.py:90-125; workspace/graph.js:55,170; palette.js:212 |
| NEW code-dependency map view (`view=dependencies`) | **LIVE** | service.py:232-301; graphview.js (+306), workspace/graph.js (+113); DoD state `code-map` (graph_dod.py:48) |
| Suggestion review (approve/reject) | **LIVE** | routes server.py:2113-2137; gate.js:105-107, workspace/memory.js:28-30 |
| Merge / split / undo / dedup / export / reindex / rebuild / suggest | **CLI-ONLY** | cli/graph.py (docstring: "CLI-only — no route"); `graph_merges` table has no UI reader |
| Derived-edge freshness | **PARTIAL** | rebuild fires only on folder finalize/reject (server.py:1282,984) or CLI; single-file approvals leave the graph stale; UI offers only a "Copy: kira graph rebuild" clipboard hint (graph.js:60-62) |

## 10. Attention & dreaming (`src/kira/attention/`)

| Capability | Status | Evidence |
|---|---|---|
| Unified attention queue (ASKs + intents + graph suggestions + durable rows) | **LIVE** | readmodel.py:52-152; `/api/attention` (server.py:1905); gate.js:36 |
| Resolve (done/dismiss) | **LIVE** | server.py:1939; gate.js:112-113 |
| Dreaming jobs (proposal-only, attended) | **CLI-ONLY** | cli/dream.py `kira dream run`; proposals surface in the queue; scheduling deferred past Checkpoint K (runner.py:6-7) |
| NotificationRouter (urgent push, quiet hours, per-project mute) | **UNWIRED** | routing.py:84-122 defined+exported, **never instantiated anywhere**; config keys `attention.urgent_channels`/`quiet_hours_*`/`muted_projects` (config.py:545-548) change nothing at runtime |
| Dreaming tool cage (`DREAMING_TOOLS`, `assert_caged`) | **UNWIRED** (deferred variant) | dreaming.py:32-111; every shipped builder is tool-less (builders.py:120) |

## 11. Voice (`src/kira/voice/`, `src/kira/ui/voice.py`)

| Capability | Status | Evidence |
|---|---|---|
| Browser push-to-talk utterance (conversation + dictation) | **LIVE** (flag-off) | `POST /api/voice/utterance` (server.py:2250); voice.js:75-76; `voice.enabled: false` default (settings.yaml:126) |
| Safe TTS caption playback | **LIVE** (flag-off) | server.py:2291; voice.js:103 |
| Meeting capture → unreviewed KB source | **LIVE** (flag-off) | server.py:2308; meetings.js:25 |
| Server-mic `listen_once` | **API-ONLY** | `POST /api/voice/listen` (server.py:2230) — zero grep hits; superseded by the browser-mic path but still in the mutation pin |
| Meeting → artifact on review | **UNWIRED** | meeting.py:86-106 branch requires `review_status=="reviewed"` which capture never sets; "review-promotion artifact hook is future work" |
| `retain_audio` flag | **OBSOLETE/UNCLEAR** | meeting.py:49 accepted, never used |
| Wake word | **DISABLED by design** | listening.py:59-63 `wake_active()` always False |

## 12. Actions / connectors (`src/kira/tools/builtin/`, `src/kira/actions/`)

| Capability | Status | Evidence |
|---|---|---|
| Two-phase outward writes (propose → preview → approve → execute → undo) | **LIVE** | connectors_write.py:87; `/api/intents*` (server.py:1816-1899); gate.js:99-150 |
| Intent detail view | **API-ONLY** | `GET /api/intents/{id}` (server.py:1825) — zero grep hits; the list's rendered preview is the only surface |
| `connector_writes` journal (what actually left the box) | **INTERNAL** | written by WriteExecutor, read only for undo; **no list/audit read model** (migrations v10; agent D table) |
| Egress log | **INTERNAL** | log events only (egress.py:23); no table, no UI audit view |
| Google reads / drafts / notify | **LIVE** (flag-off) | connectors_google.py; Hub cards (hub.js:49-55); all `connectors.*.enabled: false` default |
| OAuth connect / disconnect | **CLI-ONLY** | `kira connect <provider>`; Hub renders copy-paste command strings (hub.js:18-29) |

## 13. Models / providers / costs / observability

| Capability | Status | Evidence |
|---|---|---|
| Cost overview (dimensions, unpriced-distinct, budget warning) + ROI | **LIVE** | readmodels.py:253,524; costs.js:101 |
| Budget limits set from UI | **API-ONLY + EPHEMERAL** | `POST /api/budgets` (server.py:507) — zero grep hits; and it mutates only the in-process config copy (comment server.py:509-510) |
| Provider availability truth | **LIVE (with a defect)** | `capability_truth` (readmodels.py:1341); Hub/Settings call it without workspace ⇒ `exposed_to_chat` falls back to connected-implies-exposed (readmodels.py:1330,1336-1338; server.py:652,667), so it can disagree with Daily/header — violating its own "never disagree" doc (readmodels.py:1348-1350) |
| Context reuse / cache savings columns (v11) | **DISABLED** | `context_reuse.enabled: false` (config.py:478); costs read model has a `context_reuse` rollup that will be empty in default installs (readmodels.py:253 area) |
| Eval harness (`kira eval`) & Lab screen | **PARTIAL** | Lab reads files only (readmodels.py:1641); running evals is CLI-only; Lab is debug-gated (app.js:409) |
| Backup/restore | **CLI-ONLY** | `kira backup` (cli); no UI surface |
| Federated FTS search across 8 domains (messages/memories/kb/tasks/runs/digests/artifacts/graph) | **API-ONLY** | `GET /api/search` → `_federated_search` (server.py:1960); persistence/fts.py; **zero grep hits — the palette searches only `/api/graph/search`** (palette.js:212) |
| Saved views (save/delete) | **PARTIAL/API-ONLY** | `GET /api/views` consumed (projects.js:131) but `POST /api/views/save` (server.py:1981) and `POST /api/views/{id}/delete` (server.py:2009) have zero grep hits — views can render but never be created or removed from the UI |

## 14. Verified-orphan route summary (grep over `src/kira/ui/static/`, this session)

Sixteen routes with **no frontend consumer**: `POST /api/tasks/create`, `GET /api/tasks/{id}/runs`, `POST /api/memory/remember`, `POST /api/turn/cancel`, `GET /api/search`, `POST /api/views/save`, `POST /api/views/{id}/delete`, `POST /api/budgets`, `POST /api/digest/run`, `POST /api/voice/listen`, `GET /api/agents`, `GET /api/notices`, `POST /api/projects/{id}/update`, `POST /api/projects/{id}/services`, `GET /api/artifacts/{id}`, `GET /api/intents/{id}`.

That is 10 of the 47 pinned mutation routes (test_ui_readmodels.py:136-209) and 6 GET routes — roughly a fifth of the HTTP surface.
