# ADR-0022: Workstation UI/UX Repair — the user journey (Phase 15.5)

*Status: accepted (Phase 15.5, 2026-07-09). A repair of the workstation's user journey — conversation
header, real model/mode/scope selectors, boot rehydration, one connector-truth read model, a
conversation-first Daily, full browser voice, a richer command palette, and graph/workspace
discovery. It adds a small, enumerated set of human-authority UI-state routes and NO new agent
authority: every real action still flows through the existing Gate/approval paths. ⛔ Checkpoint J2
(before Phase 16) gates on the ten-bullet evidence + a manual walkthrough.*

## Context

By Phase 15 the backend was strong but the product journey was weak (a live re-test surfaced it): a
reload showed "No messages yet" though the conversation was alive; there was no New/Clean Chat; the
composer's model chip was hardcoded HTML; mode/scope were invisible; Daily/Hub/Settings disagreed
about connectors; voice showed "Talk" while off; the palette couldn't do much; Workspace/Graph were
buried. Phase 15.5 repairs the journey while holding the safety floor unchanged.

## Decision

- **One source of truth per fact, rendered everywhere.** `GET /api/runner` now reports the active
  `session_id`/`session_title`/`model`/`effort`; `capability_truth(config, …)` is THE availability
  read model (connectors/providers/services/voice/MCP with `state` + `exposed_to_chat` + a plain
  `reason`), computed once server-side and embedded in `/api/daily`, `/api/hub`, `/api/settings`
  (+ the dedicated `/api/capabilities`), so the four surfaces can never disagree.
- **Real, server-backed model selection — Anthropic-only.** `InteractiveModelState` (the `ModeState`
  shape) holds the interactive model; the loop reads it via an injected `model_override`, frozen per
  turn and **byte-identical when unset**. `set()` enforces an Anthropic-only allowlist because the
  main chat carries private context and 10C pins `private_ok` to anthropic; external providers are
  shown disabled with the reason. The `ModelRegistry` routes (planner/judge/utility) are untouched,
  and the ledger attributes the switched model for free (provider stays anthropic).
- **The reload bug is fixed by reporting truth, not caching.** On boot the client reads the active
  `session_id` from `/api/runner` and rehydrates that transcript via the existing
  `GET /api/sessions/{id}` — once, never clobbering an in-progress chat.
- **A conversation header + conversation-first Daily.** A calm header (scope · title · model · mode ·
  capability summary + New/Resume/Rename/Pin/Archive) sits above a hero (header + chat + composer);
  the dashboard (briefing/tasks/artifacts/run/connectors) is calm secondary context below, with
  repo/eval noise behind the existing Debug toggle. The rail surfaces Workspace (active project),
  Artifacts, and a ⌘K Search affordance.
- **Full browser voice** (scope expanded by Habib): browser-mic push-to-talk (`getUserMedia` +
  `MediaRecorder` → `POST /api/voice/utterance` → the SAME voice session) and optional OpenAI TTS
  playback (`POST /api/voice/tts`, synthesizing ONLY the masked+capped safe caption). Reuses the
  Phase-7 pipeline (same STT/TTS, same egress logging); activates only when voice is enabled in
  config (never touched here).
- **A richer palette, with a pinned write allowlist.** The palette gains Actions (New Chat, Switch
  Project/Model/Mode, Open Active Workspace/Graph, Run Workflow) and unified search
  (`/api/graph/search`, entities included). This deliberately amends the Phase-11 "GET-only" rule:
  the palette may write, but ONLY to the four UI-state routes, funnelled through a single `act()`
  helper. Result routing resumes chats, opens the focused graph tab, opens artifact content, or
  navigates.
- **Graph/Workspace discovery.** A Daily active-workspace card, a teaching graph empty state, and
  per-memory "View in graph" deep-links (localStorage focus + hash — navigate-only).

## The walls (all pinned by tests)

- **No new agent authority.** The mutation-route closed set is exactly **43**: Phase-15's 37, plus
  the four Phase-15.5 UI-state ops (`/api/model`, `/api/sessions/new|rename|archive`), plus the two
  full-browser-voice routes (`/api/voice/utterance` — a turn through the UNCHANGED VoiceApprover;
  `/api/voice/tts` — a stateless safe-caption synth). No route reaches a tool/executor directly.
  *Pinned:* `test_mutation_route_closed_set`.
- **Model switching cannot escalate** — Anthropic-only allowlist; routes registry untouched
  (`test_ui_state`, `test_session_lifecycle`).
- **Server state, not cosmetics** — chips render from `/api/runner`//api/models`; the loop uses the
  override; the ledger records the switched model (`test_ui_state` seam byte-identical pin).
- **Screen is the only approval surface** — voice PREPARES; a risky voice turn escalates to the
  on-screen Gate via the unchanged VoiceApprover; TTS only ever receives the masked safe caption
  (`test_voice_ui`); caption-privacy renderer tests unchanged.
- **Connector truth is consistent** — Daily/Hub/Settings embed IDENTICAL capability rows
  (`test_connector_truth`).
- **Palette writes ⊆ the allowlist** — one `act()` funnel; never the agent-turn/approval routes
  (`test_ui_palette`).
- **No secret on any new GET** — `/api/models`, `/api/capabilities`, extended `/api/runner`, and the
  daily/hub/settings payloads are presence/state/reason only (`test_connector_truth`, the whole-GET
  sweep).
- **Untrusted content stays inert** — header/palette/graph/hub render via `el()`/textContent (chat
  titles + project names are user/model text); no `innerHTML` on new paths; no external assets.
- **Sessions are never destroyed** — archive is a status flip (migration v13, additive + guarded).
- **Screenshot DoD** — `tests/ui/workbench_dod.py` GREEN 81/81 (9 states × noir/light/neon ×
  1440/1024/390), reduced-motion stable, real-shell boot.

## Consequences

Kairo becomes a premium, calm, conversation-first workstation: you see your scope, model, mode, and
what's connected at a glance; a reload keeps your conversation; New/Resume/Rename/Pin/Archive are
one click; the model and mode are real, cost-attributed server state; voice works from the browser
mic with safe captions and optional playback; the palette searches everything and performs a small
set of reversible actions; and the Workspace/Graph are discoverable. None of it grants a new way to
act — every write is a reversible UI-state op or flows through the existing Gate. Deferred
(ADR-0022 §deferred / plan §10): an external-provider (no-private-context) chat mode, per-message
model switching, in-transcript search, message editing, a native mobile layout, and an MCP client.
ADR numbering: 0022 (Phase 15.5). Phase 16 (attention/dreaming) is BLOCKED until Checkpoint J2 is
signed off.
