# Kairo Phase 15.5 — UI/UX Repair: from dev dashboard to premium AI workstation

*(Plan of record, authored by Fable 5 on 2026-07-09. To be committed as
`docs/PLAN-15_5-uiux-repair.md` in Task 1. Baseline: Phase 15 complete at `5c3d822` — suite 1916
green, ruff clean, core replay gate 19/19 $0, mutation-route pin **37**, migrations at **v12**,
workspace tab allowlist **11**, ADR-0021 shipped. This phase is a MANDATORY gate: Phase 16 does not
start until Phase 15.5 is implemented, screenshot-tested, and signed off at ⛔ Checkpoint J2.
NEVER touch: `docs/PLAN.md`, `docs/PLAN-7-voice-consent-checkpoint.md`, `mcp_sample.json`,
`design/`, `config/settings.yaml`, `config/permissions.yaml`, `.env`, or any connector/token file.)*

## 0. Context — what this phase is (and is NOT)

The backend through Phase 15 is strong: projects, memory graph, connectors, orchestration teams,
costs, voice, Gate approvals, artifacts, workspace tabs, evals. But the live re-test after Phase 15
showed the **product journey** is weak: Daily is a pile of cards with the conversation at the
bottom, a reload forgets the chat, there is no New Chat, the composer's model chip is a hardcoded
`<span>`, mode/scope are invisible, three surfaces disagree about connectors, the palette can't do
anything, and Workspace/Graph are buried. Phase 15.5 repairs the **user journey** — clarity,
navigation, and read views — while adding **as little new authority as possible** (four small
human-authority UI-state routes, enumerated below; everything else reads or navigates).

This is NOT: a redesign of the Gate/approval flow, a new engine, graph-fed prompts (Phase 16), an
MCP client, or new connector capability. The safety floor is unchanged.

## 1. Grounding — observed defect → verified mechanical cause

Every fix below is anchored to a cause inspected in the code on 2026-07-09:

| # | Observed (Habib's re-test) | Mechanical cause (verified) |
|---|---|---|
| 1 | Reload → Conversation says "No messages yet" despite Recent Chats | `app.js` `state.chat` is browser-memory only; boot never rehydrates. The server keeps `UiSession.messages`/`session_id` alive, but `GET /api/runner` doesn't report the active `session_id`, so the client can't reload the transcript it is *in*. |
| 2 | No obvious New Chat / Clean Chat | `UiSession.start_new_session(project_id)` exists (`ui/session.py:219`) but **no route or button exposes it** — it only fires as a side effect of `/api/projects/select`. |
| 3 | No model selector; chip is fake | `daily.js:39` hardcodes `<div class="chips"><span>opus-4-8</span><span>effort high</span></div>`. The loop actually reads `config.models.main` each turn (`core/agent.py:252`) — there is no runtime override seam. |
| 4 | No mode selector | `POST /api/mode` exists and broadcasts `mode_changed` over WS — but no UI control calls it and `app.js` has **no `mode_changed` handler** (the chip only updates on the 4s poll). |
| 5 | Chat scope unclear (global vs project) | The active project lives only in the tiny status-bar chip (`st-project`); switching requires the Projects screen; the conversation area itself shows no scope. |
| 6 | Daily vs Hub vs Settings disagree on connectors | Three **different** read models: `daily_overview` exposes `{google, notifiers}` only; `hub_status` and `settings_overview` each shape their own view. None reports whether a connected thing is actually **exposed to the current chat** (tool registered). |
| 7 | Voice says off but Talk is visible | `st-mic` defaults hidden and `pollStatus` hides it — but there is a first-paint/poll gap, the "voice off" pill gives **no reason**, and the Meetings rail item + Talk affordances imply capability that isn't wired. |
| 8 | Palette is shallow | `ui/palette.js`: static 13-screen NAV + `/api/search` results that navigate to a *generic screen* (a chat hit → `#daily` **without resuming the chat**); no artifacts/graph/runs routing, **zero actions** (deliberately GET-only in Phase 11). |
| 9 | Daily feels like an empty dashboard | 11 stacked single-column zones; **Conversation is zone 11 of 11**; repo/eval "What changed" noise is always visible; no next-best-action. |
| 10 | Workspace/Artifacts/Graph/Search hidden | Rail = Daily/Projects/Studio/Costs/Settings + Gate/Trace/Hub/Lab/Meetings. No Artifacts (screen exists!), no Active Workspace entry, no Graph path outside the workspace tab, no visible search affordance. |
| 11 | No Rename/Archive for chats | `SessionStore` has `set_pinned` only; sessions have no `archived` column. |

## 2. Architecture — one truth per fact, read everywhere

The theme of this phase: **every piece of workstation state gets exactly one server-side source of
truth, and every surface renders from it.** No more per-surface shapes that drift.

```
src/jarvis/
├── ui/
│   ├── session.py          # UiSession — gains nothing; start_new_session already exists
│   ├── state.py            # NEW — InteractiveModelState (the ModeState pattern): the runtime
│   │                       #   {model} override for the INTERACTIVE loop only; consulted by
│   │                       #   AgentLoop at turn start via an injected callable. Anthropic-only
│   │                       #   allowlist (private_ok pin, 10C). Never touches routes registry.
│   ├── readmodels.py       # + capability_truth(config, services): THE one connector/provider/
│   │                       #   service/voice/MCP availability read model (presence + state +
│   │                       #   exposed_to_chat + plain-language reason). Daily / Hub / Settings /
│   │                       #   conversation header ALL consume it. + interactive_models(config):
│   │                       #   the selectable-model list with honest availability states.
│   ├── server.py           # + GET /api/models, GET /api/capabilities;
│   │                       #   + POST /api/model, /api/sessions/new, /api/sessions/{id}/rename,
│   │                       #     /api/sessions/{id}/archive   (pin 37 → 41, exactly once)
│   │                       #   + /api/runner reports session_id/session_title/model/effort
│   └── static/
│       ├── index.html      # rail: + Artifacts, + Workspace (active project), palette hint;
│       │                   #   Talk stays default-hidden with an off-reason line
│       ├── app.js          # boot rehydration (load the active session transcript);
│       │                   #   WS handlers for mode_changed / project_changed / model_changed
│       ├── ui/header.js    # NEW — the conversation header component (scope · title · model ·
│       │                   #   mode · capability summary · New/Resume/Rename/Pin/Archive)
│       ├── ui/palette.js   # v2: result routing that RESUMES chats, artifact/graph/run domains,
│       │                   #   + an ACTIONS section (pinned mutation allowlist, §6.4)
│       └── screens/daily.js# conversation-FIRST relayout (§5.3); zones re-tiered; noise → Debug
├── persistence/
│   ├── sessions.py         # + set_title, + set_archived, list filter excludes archived
│   └── migrations.py       # v13 (additive): ALTER sessions ADD archived INTEGER NOT NULL DEFAULT 0
└── core/agent.py           # reads the model via the injected override callable (default:
                            #   config.models.main — byte-identical when no override is set)
```

## 3. Load-bearing design decisions

**D1 — Real model selection, Anthropic-only switching (the 10C `private_ok` pin holds).** The
interactive conversation injects private context (memory, project state), and Phase 10C pinned
`private_ok=True` to **anthropic only** — enforced, not advisory. Therefore: the composer's model
selector switches freely among a pinned `INTERACTIVE_MODELS` allowlist of Anthropic models
(fable-5, opus-4-8, sonnet-5, haiku-4-5, resolved from the provider catalog), while OpenAI /
Gemini / Qwen / DeepSeek / Z.ai appear **visible but disabled** with the plain-language reason
("receives your private conversation context — not enabled for the main chat"; plus key/pricing
state when relevant). This honors "GPT/Gemini/Qwen/DeepSeek/Z.ai when available" *honestly* — the
catalog machinery (key present ∧ enabled ∧ priced, fail-closed) already computes availability.
A no-private-context external chat mode is **deferred** (§10). Switching NEVER touches the
`ModelRegistry` routes (planner/judge/utility keep their authority pins).

**D2 — The override is a `ModeState`-shaped runtime state, not a config mutation.**
`InteractiveModelState` holds the current model; `AgentLoop` consults an injected
`model_override: Callable[[], str | None]` exactly where it reads `config.models.main` today
(two sites). Default `None` ⇒ byte-identical behavior (pinned). `POST /api/model` validates
against `INTERACTIVE_MODELS`, sets the state, broadcasts `model_changed`. **Cost attribution is
free**: the ledger already records `response.model` per call, and the provider stays `anthropic`,
so `model_calls` rows attribute correctly with zero ledger change (pinned by test).

**D3 — The reload bug is fixed by reporting truth, not by client caching.** `GET /api/runner`
gains `session_id` + `session_title` (+ current `model`, `effort`). On boot, if `session_id` is
set, `app.js` loads that transcript via the existing `GET /api/sessions/{id}` and fills
`state.chat`. No localStorage transcript cache (stale-data risk), no new state store — the server
already knows the conversation; the client finally asks.

**D4 — Four new mutation routes, landed in ONE task, pin 37 → 41 exactly once.**
`POST /api/model` (D2), `POST /api/sessions/new` (exposes the existing `start_new_session` under
the CURRENT project scope; 409 while a turn is in flight), `POST /api/sessions/{id}/rename`
(`set_title`), `POST /api/sessions/{id}/archive` (`set_archived`; archived chats leave Recent
lists, `?include_archived=1` still lists them — never a DELETE). All four are human-authority
UI-state ops in the `sessions/pin` mold: no tool, no executor, no Gate reach, body-validated,
loopback-Origin + session-auth like every mutation. The closed-set test updates once.

**D5 — One capability read model ends the connector disagreement.**
`capability_truth(config, services)` returns, for **connectors** (Google Calendar/Gmail/Drive
split out, Telegram, Kakao), **providers** (anthropic/openai/gemini/qwen/deepseek/zai),
**services** (the enabled catalog trio+), **voice**, and **MCP** (an honest "no MCP client
exists" row): `{name, state: connected|not_configured|needs_reconnect|disabled|unpriced,
exposed_to_chat: bool, reason: str}`. `exposed_to_chat` derives from the SAME registration truth
the loop uses (is the tool registered for the interactive session), so "Settings says Google
exists but Daily says no connectors" becomes impossible: Daily's card, Hub's grid, Settings'
policy list, and the conversation header's summary chip all render from this one function
(consistency pinned by a shared-fixture test). Presence booleans + env NAMES only — never a key
value (secret-swept).

**D6 — Daily becomes conversation-first; one primary attention surface stands.** New zone order
(§5.3): approval banner (unchanged, amber, always #1 when present) → **Conversation + composer as
the hero** → a compact secondary column (Briefing / Today / Approvals-link) → a tertiary strip
(artifacts / latest run / connectors / cost) → Workflows. Repo/eval "What changed" moves behind
the EXISTING Debug toggle (`body.debug` class — zero new mechanism) with an urgent-only exception
(a red eval-stale chip may surface). Recent chats move into the header's Resume menu (and stay in
the palette). The "one primary attention surface" rule is preserved: the approval banner always
outranks; nothing else pulses.

**D7 — The palette becomes the workstation's second door — with a pinned write allowlist.**
Phase 11's palette was deliberately GET-only. Phase 15.5 **deliberately amends** that pin:
palette actions may POST to exactly `{/api/sessions/new, /api/mode, /api/model,
/api/projects/select}` — the four reversible UI-state routes — and NOTHING else (no `/api/turn`,
no approvals, no Gate-reaching action; "Run Workflow" only prefills Studio via navigation). The
old "GET-only" structural test is replaced by an exact-allowlist structural test. Search results
get real routing: a chat result **resumes** that chat (existing resume route + the D3 loader), an
artifact opens `#artifacts` (or its content GET), a graph entity opens the graph tab focused on
it, a run opens Studio.

**D8 — Voice honesty.** `voice.status()` gains a `reason` field (config-off / missing deps / no
mic / not wired). Talk stays default-hidden (already true) and the status pill, when off, renders
the reason ("Voice is off — enable it in settings.yaml"); when on, the existing `voice_state`
stream renders the full ready/listening/transcribing/thinking/speaking(/error) vocabulary.
Caption privacy (`VoiceRenderer` masking) is UNTOUCHED — display-only changes.

## 4. Routes & pins summary

| Change | Detail |
|---|---|
| Mutation pin | **37 → 41, exactly once** (Task 2): `POST /api/model`, `POST /api/sessions/new`, `POST /api/sessions/{id}/rename`, `POST /api/sessions/{id}/archive` |
| New GETs (read-only) | `GET /api/models` (selectable + honestly-disabled models), `GET /api/capabilities` (the D5 truth) — both secret-swept |
| Extended GET | `GET /api/runner` += `session_id`, `session_title`, `model`, `effort` |
| WS events | += `model_changed`; `app.js` gains handlers for `mode_changed` / `project_changed` / `model_changed` (all already-broadcast or new-broadcast state echoes) |
| Migration | **v13** (additive): `ALTER TABLE sessions ADD COLUMN archived INTEGER NOT NULL DEFAULT 0` (+ version-pin sweep 12→13 across the known ~11 assertion sites) |
| Tab allowlist | unchanged (11) |
| PLAN_SAFE / Gate / modes / connectors / eval contracts | untouched |

## 5. UI changes per surface

### 5.1 Conversation header (`ui/header.js`, mounted above the chat on Daily)
One calm bar: **scope selector** (Global ▾ / project list — posts `/api/projects/select`, which
already starts a fresh scoped chat; confirm-if-transcript-nonempty) · **chat title** (from
`session_title`; inline Rename; Pin ★; Archive) · **New Chat** (posts `/api/sessions/new`) ·
**Resume ▾** (recent chats via `GET /api/sessions`, resume loads transcript — the existing
`api.resumeChat`) · **model chip** (current model; opens the D1 selector) · **mode chip**
(Planning / Approval / Auto segmented control → `/api/mode`) · **capability summary** (e.g.
"6 tools · Google ✓ · voice off" — from `/api/capabilities`; click → Hub). Every value renders
from server payloads — a structural test bans hardcoded model strings in the JS.

### 5.2 Composer controls (in `daily.js`, replacing the fake chips)
Left of Send: the same model + mode chips (shared component with the header), scope indicator,
and a subtle "clean chat" affordance when the transcript is non-empty. Chips reflect
`/api/runner` + WS echoes; optimistic UI only after a 200.

### 5.3 Daily relayout (`daily.js`)
Zones: 1) approval banner (amber, unchanged) → 2) **Conversation hero** (header + chat + composer;
`60ch`-max message column, roomier bubbles, tool lines as quiet pills) → 3) secondary column:
Briefing (digest), Today (tasks), pending-approvals link → 4) tertiary strip (one row of compact
cards): recent artifacts · latest run · connector health (from `/api/capabilities`) · cost today →
5) Workflows chips → 6) "What changed" (repo/eval) renders ONLY under `body.debug`, except an
urgent red eval-stale chip. Empty states teach the next action (already good — kept).

### 5.4 Rail (`index.html`)
Primary: Daily · Projects · **Workspace** (→ `#workspace/{activeProjectId}`; hidden when global
scope) · **Artifacts** · Studio · Costs · Settings. Tools: Gate · Trace · Hub · Lab · Meetings.
Plus a "⌘K Search" affordance at the rail foot that opens the palette. (Vault/Tasks/Memory stay
hash-only + palette-reachable; the rail stays calm.)

### 5.5 Palette v2 (`ui/palette.js`)
Sections: **Actions** (New Chat · Switch Project ▸ · Switch Model ▸ · Switch Mode ▸ · Open Active
Workspace · Open Graph · Run Workflow ▸ prefills Studio) → **Go to** (existing NAV + workspace
tabs of the active project) → **Results** (federated `/api/search` + `GET /api/graph/search`
entity hits, labeled `Entity`, opening the graph tab focused on the node; chat results resume;
artifact results open the artifacts screen/content; run results open Studio). Writes ⊆ the D7
allowlist, pinned.

### 5.6 Voice — full browser experience (`index.html` + `app.js` + `ui/voice.js` + server)
*(Scope expanded 2026-07-09 by Habib: "Full browser voice". Reuses the Phase-7 pipeline — same
STT/TTS providers, same egress logging, same VoiceApprover→UIScreenApprover contract — moving the
audio to/from the browser. Adds NO new authority and NO new egress class. Activates ONLY when voice
is enabled (config, user-controlled); I never touch `config/settings.yaml` or the consent checkpoint.)*

- **Availability + state (always honest):** off ⇒ Talk hidden, pill shows "voice off — {reason}";
  on ⇒ Talk visible, pill cycles the FULL vocabulary (idle/listening/transcribing/thinking/
  speaking) + an **error** state ("🎤 error — {reason}"). `voice.status()` gains `reason`, `stt`,
  `tts` (presence/names only). No caption/renderer privacy change.
- **Browser-mic push-to-talk:** Talk requests `getUserMedia({audio})` (explicit browser permission,
  with prompting/denied/unsupported states), records one utterance (`MediaRecorder`), and uploads it
  to a NEW `POST /api/voice/utterance` (multipart audio) → the EXISTING `STT.transcribe` → the same
  voice turn path the server-mic `listen` used. Audio egress is the same class the server path
  already logs. The server-mic `/api/voice/listen` stays as a fallback.
- **Optional OpenAI TTS playback:** when a safe caption is produced, the browser may fetch its audio
  from a NEW `POST /api/voice/tts` that synthesizes ONLY the already-safe caption (post-mask/cap) via
  the existing `TTS.synthesize`, and plays it in an `<audio>` element. Because only the SAFE caption
  is ever synthesized, no secret/payload can reach TTS. Playback is opt-in (a toggle); local/subtitle
  TTS returns no audio (captions stay text — honest).
- **Failure reasons everywhere:** mic denied, no cloud provider, STT/TTS error, unsupported browser
  — each renders a plain reason, never a raw error body.
- **Safety invariant (unchanged, pinned):** voice PREPARES; the screen is the ONLY approval surface.
  A risky action in a voice turn still escalates to the on-screen Gate via the unchanged
  VoiceApprover→UIScreenApprover; a spoken/heard transcript is untrusted; no unattended mic.
- **Testability:** keyless tests cover the new endpoints (fake audio bytes + fake STT/TTS), the
  safety framing (TTS only ever receives the safe caption; utterance routes through the approver),
  and the JS structure (permission flow, no inline handlers). Live browser mic + audio playback is a
  MANUAL check at Checkpoint J2 (can't be exercised keyless in CI).

### 5.7 Graph & Workspace discovery
Daily tertiary strip gains an **Active workspace** card (project name, tab links incl. Graph,
falls back to "Select a project"). The graph tab's empty state gains action chips ("Rebuild the
graph" → copies `jarvis graph rebuild`; "Learn what the graph shows" → inline one-liner). Memory
screen rows, artifact rows, and vault sources get a quiet "View in graph →" link (GET/navigate
only, deep-linking `#workspace/{pid}/graph` focused on the node). No new authority.

## 6. Safety model (non-negotiables → enforcement)

1. **No new authority beyond the four enumerated UI-state routes** — pin 37→41 exactly once;
   each route is body-validated, Origin-checked, session-authed, tool/executor-free.
   *Pinned:* `test_mutation_route_closed_set`, per-route tests.
2. **All writes through existing Gate/approval paths** — the palette/header/composer never call
   `/api/turn` implicitly, never resolve approvals, never touch Gate policy.
   *Pinned:* palette exact-allowlist structural test; header/composer route-target tests.
3. **Model switching cannot escalate** — `INTERACTIVE_MODELS` is an Anthropic-only pinned
   allowlist (private_ok, 10C); `POST /api/model` rejects anything else; routes registry
   (planner/judge/utility) untouched. *Pinned:* allowlist test + registry-untouched test.
4. **Server state, not cosmetics** — chips render from `/api/runner`//api/models` payloads; a
   turn after `POST /api/model` records the new model in `model_calls` (ledger attribution).
   *Pinned:* end-to-end FakeClient test asserting the ledger row + a no-hardcoded-model-string
   structural check.
5. **No secrets on any new GET** — `/api/models`, `/api/capabilities`, extended `/api/runner`
   swept against key + prompt canaries; capability rows are presence/state/reason only.
6. **Untrusted content stays inert** — headers/palette/graph links render via `el()`/textContent;
   no innerHTML on any new/modified path (chat titles are user/model text!); no external assets.
7. **Sessions are never destroyed** — archive is a status flip; no DELETE route exists; pin/
   rename/archive are metadata.
8. **Voice caption privacy untouched** — display-only changes; `VoiceRenderer` masking pinned
   tests stay green unmodified.
9. **One attention surface** — the approval overlay/banner remains the only pulsing surface; new
   menus are passive. *Pinned:* structural check (no new modal auto-opens).
10. **Determinism** — the screenshot DoD harness stays seeded/offline (no wall-clock in fixtures).

## 7. Test strategy

- **Pins that move (each exactly once):** mutation closed set 37→41; migration version 12→13
  (sweep the ~11 assertion sites — the v12 lesson); palette GET-only → exact-allowlist.
- **New unit suites:** `test_ui_state.py` (InteractiveModelState + allowlist + loop-override seam
  byte-identical-when-unset), `test_session_lifecycle.py` (new/rename/archive/pin/resume + list
  filtering + 409-while-busy + lazy-create semantics), `test_capability_truth.py` (one fixture →
  identical rows across daily/hub/settings payloads; exposed_to_chat matches tool registration;
  reasons present; secret sweep), `test_conversation_header.py` + palette v2 structural tests
  (server-sourced values, allowlisted POSTs, resume-routing), `test_runner_state.py` extension
  (session_id/model/effort in the payload), voice-status reason tests.
- **End-to-end keyless:** FakeClient turn after `POST /api/model` → `model_calls` row carries the
  selected model; reload simulation (fresh client, `GET /api/runner` → `GET /api/sessions/{id}`
  rehydrates the exact transcript); `POST /api/sessions/new` → next turn creates a fresh session
  row under the active project.
- **Existing suites must stay green unmodified** (voice render, gate, modes, eval contracts) —
  any needed change to THEIR assertions is a design smell to escalate, not patch.
- **Full gate per task:** suite + ruff + `uv run jarvis eval gate --suite core` (keyless replay,
  $0) green before every commit; per-task commits with explicit paths.

## 8. Screenshot definition of done (`tests/ui/workbench_dod.py`)

Self-contained harness (the office/graph `*_dod.py` pattern: seeded JSON + static copy + real JS
in headless chromium + `analyze_overlap`), **9 states × 3 themes (noir/light/neon) × 3 viewports
(1440/1024/390) = 81 shots**, zero overlap/overflow violations, reduced-motion stable:

| State | What it proves |
|---|---|
| `daily-empty` | Fresh install: conversation hero + teaching empty states, no dead cards |
| `daily-populated` | Briefing/tasks/artifacts/run/connectors placed per the D6 hierarchy |
| `chat-fresh` | Post-New-Chat: empty hero, header shows Global scope + model + mode |
| `chat-project` | Scoped chat with transcript; header shows project + title + capability chip |
| `model-selector` | The open selector: Anthropic models selectable; externals disabled WITH reasons |
| `palette` | Open with a query: Actions + Go-to + mixed Results (chat/artifact/entity/run) |
| `hub-truth` | Hub grid rendering `capability_truth` states incl. a needs_reconnect + a not-exposed reason |
| `graph-discovery` | Daily workspace card + the graph tab's improved empty state |
| `voice` | Talk state (off-with-reason AND a listening/transcript-bubble state) renders cleanly |

Mobile (390) must show: composer + controls usable, header collapsing gracefully, rail behavior
sane. Spot-check at least 4 shots visually (not just the probe) before the checkpoint.

## 9. Milestones + tasks (per-task commits; suite + ruff + core gate green each task)

**M0 — truth substrate**
1. **Plan doc + migration v13 + session store + runner truth.** Commit this doc; v13 additive
   migration + version-pin sweep; `SessionStore.set_title/set_archived` + archived-aware listing;
   `/api/runner` += session_id/session_title/model/effort. *Accept:* pins 12→13 all green; runner
   payload shape pinned; archived chats leave `GET /api/sessions` by default.
2. **Server state + ALL FOUR mutation routes (pin 37→41 once) + read models.**
   `InteractiveModelState` + the loop's `model_override` seam (byte-identical when unset, pinned);
   `GET /api/models` + `GET /api/capabilities` (D5, secret-swept); `POST /api/model` +
   `/api/sessions/new|rename|archive`; `model_changed` broadcast. *Accept:* closed-set test at 41;
   ledger-attribution e2e test green; capability consistency test green (daily/hub/settings same
   source); allowlist rejects non-Anthropic.
3. **Client rehydration + conversation header + composer controls.** Boot loads the active
   transcript (D3); `ui/header.js` (§5.1) + real composer chips (§5.2) + WS handlers for
   mode/project/model_changed. *Accept:* reload shows the live conversation; every chip value is
   server-sourced (structural); New/Resume/Rename/Pin/Archive all work through their routes.
   **→ Non-blocking preview:** post header/composer screenshots to Habib for course-correction;
   continue unless he objects.

**M1 — surfaces**
4. **Connector truth everywhere.** Daily card, Hub grid, Settings list, header summary all render
   `capability_truth`; "connected but not exposed to chat" reasons in plain language; Settings'
   raw catalog dump demoted to a collapsed "advanced" section. *Accept:* consistency + sweep tests.
5. **Daily conversation-first relayout** (§5.3) + rail update (§5.4). *Accept:* zone order pinned
   structurally; "What changed" debug-gated; approval banner still outranks everything.
6. **Full browser voice** (§5.6, scope expanded by Habib): status `reason`/`stt`/`tts`; Talk
   gating + full state vocabulary + error; browser-mic push-to-talk (getUserMedia → `POST
   /api/voice/utterance` → existing STT + turn) with permission/denied/unsupported states; optional
   OpenAI TTS playback (`POST /api/voice/tts`, SAFE-caption-only) in an `<audio>`; safe transcript
   bubbles + captions; clear failure reasons. All behind the existing voice-enabled/cloud opt-in
   (config, user-controlled — untouched here). *Accept:* voice-off renders reason; the two new
   endpoints route through the unchanged VoiceApprover (screen-only approval) and TTS only ever
   receives the safe caption (keyless pins); caption/renderer privacy tests untouched and green;
   live browser mic + playback is a manual Checkpoint-J2 step.

**M2 — discovery**
7. **Palette v2** (§5.5): actions (allowlisted writes), resume-routing, entity/run/artifact
   domains. *Accept:* exact-allowlist structural pin; chat result resumes; entity result opens
   the focused graph tab.
8. **Graph/Workspace discovery** (§5.7): Daily workspace card, graph empty-state chips,
   "View in graph" links from memory/artifacts/vault. *Accept:* GET/navigate-only (no api.post
   added to those panels' pins).

**M3 — proof**
9. **Screenshot DoD** (`tests/ui/workbench_dod.py`, §8) + the polish pass its findings demand.
   *Accept:* 72/72 green + 4 visual spot-checks.
10. **Docs + closeout:** ADR-0022 (workstation journey: truth read models, the four UI-state
    routes, the palette allowlist amendment), `docs/verification-15_5.md` (DoD grid + the manual
    verification checklist below), README Status entry.

   **⛔ CHECKPOINT J2 — MANDATORY FULL STOP (before any Phase 16 work).** Evidence, each with its
   named test/screenshot: (i) mutation pin exactly 41, the four routes enumerated; (ii) reload/
   resume/new-chat lifecycle proven (fresh load, reload mid-conversation, New Chat, Resume);
   (iii) model/mode selectors are real server state — ledger row shows the switched model;
   (iv) global vs project chat scoping visible and correct; (v) connector truth consistent across
   Daily/Hub/Settings/header incl. a not-exposed-reason case; (vi) voice-off state honest;
   (vii) palette: search + actions work, writes ⊆ allowlist; (viii) Workspace/Graph discoverable
   (rail + palette + Daily card + empty-state guidance); (ix) screenshot DoD 72/72 + spot-checks;
   (x) suite + ruff + core replay gate green; no secret on any new GET. **WAIT for Habib's
   sign-off. Phase 16 is blocked until then.**

### Manual verification checklist (Habib, at Checkpoint J2)
Fresh load → reload mid-chat → New Chat → Resume an old chat → rename/pin/archive a chat →
switch Global↔project (fresh scoped chat starts) → switch model (see the chip + a real turn +
Costs attribution) → switch mode (Planning/Approval/Auto chip + behavior) → compare connectors on
Daily/Hub/Settings/header → voice-off state → palette: search a chat/artifact/entity + run each
action → open Workspace + Graph from Daily/rail/palette → mobile pass at 390px.

## 10. Now vs deferred (explicit)

**Now:** everything in §9. **Deferred:** external-provider chat mode (no-private-context session
type with its own taint rules — needs its own safety design); per-message model switching or
mid-conversation provider handoff; chat search inside a transcript; message editing/regeneration;
a mobile-native layout beyond responsive CSS; MCP client (still avoided); voice settings UI
(YAML stays the source); Daily card user-customization; palette fuzzy-ranking tuning beyond FTS.

## 11. Opus 4.8 implementation handoff

Execute Tasks 1–10 in order; **MANDATORY full stop at ⛔ Checkpoint J2** with the ten-bullet
evidence — Phase 16 only on Habib's explicit sign-off. Per-task commits with EXPLICIT paths
(never `git add -A`) ending `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`;
adversarial self-review before each commit; suite + ruff + `uv run jarvis eval gate --suite core`
(keyless replay, $0) green every task; never commit red; never touch the NEVER-touch list (§ top).
Reuse, never fork: the `ModeState` pattern for `InteractiveModelState`; the `sessions/pin` route
shape for the four new mutations; `hub_status`/`services_status` shaping for `capability_truth`;
`el()`/textContent + token CSS; the `*_dod.py` self-contained screenshot harness; the provider
catalog's fail-closed availability (never re-derive it by hand). The loop's `model_override`
seam MUST be byte-identical when unset (pin it before wiring the route). Bump the mutation pin
and the migration version pins exactly once each. Do NOT drift into Phase 16 (attention/dreaming),
graph-fed prompts, or new connector scopes. ADR-0022 reserved for this phase. When Task 2 lands,
re-verify the whole-GET secret sweep covers the new routes automatically (it walks `app.routes`);
if it skips parameterized GETs, sweep `/api/models` + `/api/capabilities` explicitly like
`test_graph_routes` does.
