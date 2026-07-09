# Phase 15.5 — verification (Workstation UI/UX Repair)

*Prepared 2026-07-09. Two layers: (A) a **keyless suite + screenshot DoD** that is GREEN now and is
the per-task gate, and (B) a **manual walkthrough** (below) — including the live browser mic + audio
playback, which can't be exercised keyless in CI. No `config/settings.yaml`, `config/permissions.yaml`,
`.env`, connector/token file, `design/`, `docs/PLAN.md`, or `docs/PLAN-7-*` was touched.*

## A. Keyless suite + screenshot DoD — GREEN

Full suite **1960 passed / 2 skipped** (expected skips: playwright-installed degradation; Windows
symlink privilege), ruff clean (src + tests), `jarvis eval gate --suite core` **19/19 PASS 3/3**
(keyless replay, **$0**), across all ten Phase-15.5 commits.

### Screenshot DoD — GREEN (81/81)

`tests/ui/workbench_dod.py` (full-shell: static copy + a harness that stubs `fetch`/`WebSocket`,
seeds per-state JSON, and imports the REAL `app.js`), `analyze_overlap` across **9 states × 3 themes
× 3 viewports**:

| State | What it proves |
|---|---|
| `daily-empty` | Fresh install: conversation hero + teaching empty states, no dead cards |
| `daily-populated` | Header (scope/model/mode/capability) + hero + calm dashboard tiers |
| `chat-fresh` | Post-New-Chat: empty hero, Global scope |
| `chat-project` | RELOAD rehydration shows the transcript; header shows project + title + Rename/Pin/Archive; rail shows Workspace |
| `model-selector` | The header's Anthropic-selectable model control (externals disabled w/ reasons) |
| `palette` | Open palette: Actions + Go-to + results |
| `hub-truth` | Hub grid: needs_reconnect / not-in-chat / private-context / disabled reasons |
| `graph-discovery` | The graph tab's teaching empty state (rebuild ritual + read-only explainer) |
| `voice` | Voice enabled: Talk + playback controls render |

Zero layout violations across all 81. The DoD **caught a real mobile bug** (the composer's live
model/mode chips + Send overflowed 390px) — fixed (input shrinks, chips hide under 640px where the
header carries them). Spot-checked visually: daily-populated + chat-project + hub-truth + mobile 390.

### Safety walls (each with its named test)

| Wall | Test |
|---|---|
| Mutation-route closed set = **43** (37 + 4 UI-state + 2 voice); no direct tool/executor route | `test_ui_readmodels::test_mutation_route_closed_set` |
| Reload/new/resume/rename/archive lifecycle | `test_session_lifecycle`, `test_conversation_header`, `test_migrations_v13`, `test_sessions_list` |
| Model select is real server state, Anthropic-only, seam byte-identical when unset | `test_ui_state`, `test_session_lifecycle` |
| Connector truth identical across Daily/Hub/Settings | `test_connector_truth` |
| Voice: screen-only approval; TTS synthesizes only the masked safe caption | `test_voice_ui` (+ unchanged `test_ui_voice_captions`/`test_voice_render`/`test_voice_approver`) |
| Palette writes ⊆ the four UI-state routes (single act() funnel) | `test_ui_palette` |
| No secret on any new GET | `test_connector_truth`, the whole-GET sweep |
| Untrusted content inert (no innerHTML on new paths; no external assets) | `test_conversation_header`, `test_ui_frontend` |
| Graph/Workspace discoverable; graph UI navigate-only | `test_graph_discovery`, `test_ui_shell`, `test_workspace_ui` |

## B. Manual walkthrough (run in an interactive session — Habib, at Checkpoint J2)

Fresh load → **reload mid-chat** (the conversation returns; no "No messages yet") → **New Chat** →
**Resume** an old chat → **Rename / Pin / Archive** a chat → switch **Global ↔ project** (a fresh
scoped chat starts) → switch **model** (see the chip + a real turn + the switched model in Costs) →
switch **mode** (Planning/Approval/Auto) → compare **connectors** on Daily / Hub / Settings / header
(they agree) → **voice off** state (Talk hidden, reason shown).

**Full browser voice (requires `voice.enabled: true` + a cloud provider opt-in in settings.yaml —
you enable it; I never touch it):** click **Talk** → grant the browser mic permission → speak one
utterance → confirm the heard transcript bubble + the safe reply caption; toggle **spoken replies**
and confirm playback of the caption; a risky action in a voice turn still stops at the on-screen
Gate (voice prepares, the screen approves); deny the mic and confirm the plain reason.

**Palette (⌘K):** search a chat (resumes), an artifact (opens content), a graph entity (opens the
focused graph tab); run New Chat / Switch Project / Switch Model / Switch Mode / Open Graph.
**Workspace/Graph:** open the Workspace from the rail + the Daily card; open the Graph; confirm the
empty-state guidance. **Mobile:** a 390px pass (rail collapses, header wraps, composer usable).

## Deferred (deliberate)

External-provider (no-private-context) chat mode; per-message model switching; in-transcript search;
message editing/regeneration; a native mobile layout beyond responsive CSS; an MCP client; voice
settings UI (YAML stays the source). See ADR-0022 §Consequences.

## Note on the mutation pin (41 → 43)

The plan projected pin **41** (the four UI-state routes). Habib's mid-phase expansion to **full
browser voice** added two routes (`/api/voice/utterance`, `/api/voice/tts`) on the SAME voice floor
as the already-pinned `/api/voice/listen`/`meeting` — `utterance` runs a turn through the unchanged
VoiceApprover (screen-only approval), `tts` is a stateless safe-caption synth. Net closed set: **43**,
each enumerated + test-pinned. No agent authority was added.
