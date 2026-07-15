# Kairo Phase 8 — Workstation UI

*(Follows `docs/PLAN.md` §2 row 8 — "FastAPI + WebSocket chat surface over the same core —
the payoff of the thin-interface rule." Baseline: Phase 7 complete at `6ab6995`, 738 unit
tests, live gate 36/36 PASS, Safety CLEAN. The attached "Kairo Workstation" HTML is a
visual reference only — its branding, palette, and screen inventory are kept; its density
is deliberately not.)*

## Context

Phases 1–7 built the agent and the instrument that proves it works. Phase 8 builds the
**product surface**: a local workstation UI that makes Kairo usable, calm, observable, and
safe — with **zero new autonomy**. The UI is the third peer interface (REPL, voice,
workstation), driving the same `AgentLoop` through the same two seams: events out
(`on_event`), and the injected `Approver` in. One approval path is why nothing can be
bypassed.

The distinctive risk of this phase is that **a web surface silently widens the authority
boundary**. The adversarial pre-mortem found three ways the obvious design would betray the
safety model, and the plan is built around their fixes:

1. **A localhost HTTP server is not a private surface.** The REPL's authority came from
   *being the TTY*; a port on 127.0.0.1 is reachable by any local process and — via CSRF
   and DNS rebinding — by any webpage the user visits. An unauthenticated
   `POST /api/approvals/{id}/approve` would be a remote-approval vulnerability wearing a
   product skin. Fix: the UI must *earn* TTY-equivalent authority (D2, ADR-0008), and
   approval routes are treated as the crown jewels.
2. **The UI becomes the "screen" for voice — and "screen available" must stay
   fail-closed.** The Phase 7 checkpoint §1.3 defined screen-available precisely (rendered
   preview + authenticated input + liveness, positively confirmed). The workstation is its
   second implementation; a naive `return True` because a server is running would quietly
   turn "voice prepares, screen commits" into "voice commits." Fix: D7's liveness-gated
   `UIScreenApprover`, with a disconnect-⇒-deny pin.
3. **Calm has a failure mode: hiding.** The product goal is calm-not-airport-board — but a
   UI that hides tool calls to look serene recreates the "hidden background actions"
   problem we've never had. Fix: the visibility invariant (D6) — every side-effecting
   action appears in Daily Mode at summary level; Debug Mode *reveals* detail, it never
   *enables* capability (pinned by test).

Everything ships against existing services — memory, tasks, KB/wiki, evals, sub-agents,
voice — through their existing human-authority operations. No new tools, no new model
paths, no new unattended behavior.

## Architecture (new pieces in bold)

```
src/kira/
├── ui/                          # the workstation (optional `ui` extra: fastapi + uvicorn)
│   ├── **server.py**            # app factory, 127.0.0.1-only, auth middleware, CSP
│   ├── **auth.py**              # per-launch token, cookie exchange, Host/Origin guards
│   ├── **approver.py**          # UIApprover (Gate queue) + UIScreenApprover (voice screen)
│   ├── **session.py**           # UiSession: turn engine + event serializer + ring buffer
│   ├── **readmodels.py**        # Vault/Tasks/Memory/Hub/Lab/Meetings read APIs
│   └── **static/**              # hand-written assets — no Node, no CDN, no external fonts
│       ├── index.html, kairo.css, app.js (shell/router/WS), screens/*.js, kairo-mark.svg
├── permissions/
│   └── **approvals.py**         # extracted persist-always rules (REPL + UI share one truth)
└── cli/repl.py                  # _persist_always → delegates to permissions/approvals.py

kira --ui                       # host process, peer of `kira` (REPL) and `kira --voice`
```

Existing seams reused, unchanged: `AgentLoop.run_turn(on_event)` and its typed events
(`TextDelta`, `ToolDecision`, `ToolStarted/Finished`, `TurnCompleted`,
`SubAgentEvent/Completed`); the injected `Approver`; `VoiceApprover` + the `ScreenApprover`
protocol; the REPL's turn lock shared with `BackgroundRunner`;
`KnowledgeService.unreviewed_sources/approve_source/reject_source/lint/stats/query`;
`TaskService.cancel` + `TaskStore.list(include_finished=)`; `MemoryStore.all_live/forget`;
`AgentRunStore.list`; `MeetingCapture`; the JSONL audit log.

## 1. Resolved design decisions

### D1 — Process & composition model: a host, not an attachment

`kira --ui` is a **host process** exactly like `kira --voice`: it opens the database,
composes the same collaborators via the same helpers the REPL uses, and serves until
shutdown (with the REPL's shutdown discipline — runner stopped, in-flight job finished,
reflection on exit). SQLite's single-connection/one-write-lock design means **one host at
a time**; the UI refuses to start if another Jarvis process holds the DB. Running REPL and
UI simultaneously is explicitly out of scope for this phase. `ui.enabled: false` (default)
⇒ no server, no routes, REPL byte-identical to Phase 7 — the same opt-in shape as
`voice.enabled`.

Stack: **FastAPI + uvicorn** behind an optional `ui` extra (mirrors the `voice` extra) —
pre-ratified in PLAN.md §2/§97. Frontend: **hand-written static assets** (vanilla ES
modules, CSS custom properties, system font stack, the KAIRO hexagon as inline SVG). No
Node toolchain, no CDN, no external fonts — self-contained and offline, and the safety pins
live in Python where they're testable. **[Amendment 7]** The frontend stays vanilla but is
explicitly structured as **tiny per-screen ES modules** (`static/screens/gate.js`,
`vault.js`, …) plus a small shared core (`app.js` = shell/router/WS only) — no monolithic
`app.js`, still no build step (modules load natively under CSP `'self'`).

### D2 — The private-admin-console contract (ADR-0008)

The server earns TTY-equivalent authority or refuses to serve:

- **Bind 127.0.0.1 only.** A non-loopback `ui.host` is a config *error* (fail-closed;
  enforced in `UIConfig`). Remote access is a future phase with a real auth story, not a
  YAML edit.
- **Per-launch 128-bit token** (Jupyter pattern): printed once as
  `http://127.0.0.1:8787/?token=…` at startup, exchanged for an `HttpOnly; SameSite=Strict`
  session cookie, never logged, never echoed by any route.
- **[Amendment 1] Clean-URL exchange.** The tokened URL hits a dedicated exchange route:
  validate token → set cookie → **`303` redirect to `/`** with `Cache-Control: no-store`.
  No served page ever carries `?token=` in its URL, so the token cannot persist in browser
  history (or a `Referer` — see amendment 2).
- **Every mutating route and the WebSocket require the session.** GETs of app assets are
  harmless; GETs of data also require it (memory contents are sensitive).
- **Host-header allowlist** (`127.0.0.1`, `localhost`) — defeats DNS rebinding. **Origin
  check** on the WebSocket and on mutating routes — defeats CSRF even if a cookie leaks
  scope.
- **[Amendment 2] `Referrer-Policy: no-referrer` and NO CORS.** Added to the hardening
  header set on every response. There is **no CORS middleware in the app, ever**: no
  `Access-Control-Allow-Origin` header (wildcard or otherwise) on any route; cross-origin
  requests fail the same-origin + Origin wall. Plus **CSP `default-src 'self'`** and
  standard hardening headers; no external asset may ever load (grep-pinned: no `http(s)://`
  in `static/`).
- **Approvals require more than auth** (see D3): a live WebSocket + a per-approval nonce
  bound to it. A cookie replay from a dead client, or from a stale page, cannot approve.

### D3 — One approval path, one persistence truth, replay-proof

The narrow-persist discipline currently lives inside `Repl._persist_always` (`repl.py` —
shell rules by prefix, writes by *resolved* parent dir with over-broad refusal,
`_NEVER_PERSIST = {schedule_task, cancel_task, spawn_agent}`). The UI must not reimplement
it — drift here is a safety bug. **Task 3 extracts it to `permissions/approvals.py`** (pure
functions over `gate`, `config`, `call`), the REPL delegates to it (its ~60 approval tests
must pass unchanged — the parity pin), and the UI consumes the same module.

**`UIApprover`** is the injected `Approver` for UI turns: an ASK becomes a pending item in
the Gate queue — full untruncated payload (**EXACT ACTION · EXACT PAYLOAD**), the gate's
`decision.reason` (**WHY KAIRO WANTS THIS**), and three explicit buttons: **Approve once /
Always allow (narrow) / Deny**. It waits indefinitely, exactly like the REPL prompt ("Kairo
paused this run — it resumes when you decide"). Every resolution writes an audit line with
`channel=ui` plus the existing `permission_resolved` flow, so the Gate's "earlier today"
list and the JSONL log tell the same story. Sub-agent ASKs surface labeled with the child's
title, and their run-scoped **pattern** grants (host / dir-prefix, never blanket
`run_shell`/`write_file`, never persisted) are preserved by reusing the existing sub-agent
grant path.

**[Amendment 3] Per-approval nonce.** Every pending approval (turn ASK, sub-agent ASK, or
voice escalation) gets a **decision id + single-use nonce**, minted server-side and
delivered **only over the live WebSocket** of an authenticated client. Resolving requires
(decision id, nonce, session cookie, live WS) to all match; a nonce is invalidated on use
**and on WS disconnect/reconnect** — so an approval click can never be replayed from an old
page state, a restored tab, or a stale DOM. **[Amendment 4]** The nonce for a given
approval is issued only **after the client acks "modal visible for decision id X"** — so
proof-of-visibility is a precondition of approvability, not a courtesy flag.

### D4 — Event stream: the same events evals trust

The server forwards every `AgentLoop` event to the client as versioned JSON
(`schema_version`, typed by event class) and keeps a bounded ring buffer for the Trace
screen. Crucially this includes **`ToolDecision`** — the Phase 5 tap that made *denied*
calls observable — so the Gate audit, the Trace tree, and the adversarial evals all read
the same stream. `SubAgentEvent` is unwrapped for display with the child's title (the Phase
6 pattern), so delegated activity renders inline, never hidden. The screen is private and
authenticated (same trust stance as the terminal), so payloads render in full where the UI
chooses to show them; nothing new is logged.

### D5 — Screens are read models; mutations are the existing human-authority set, closed and enumerated

The complete list of state-changing operations reachable through the UI (pinned by a
route-table test — anything not on this list failing the test is the point):

| Screen | Reads | Mutations (all pre-existing human-authority ops) |
|---|---|---|
| **Command (Daily)** | chat stream, session list/resume, runner status, memory-in-use, citations | submit turn · cancel turn (Ctrl-C parity) · **runner pause/resume [amendment 8]** |
| **Gate** | pending approvals, today's audit (JSONL + live), policy snapshot (read-only view of `permissions.yaml` + persisted rules) | resolve approval (once/always-narrow/deny) |
| **Vault** | KB stats, search with citations, source list + provenance, lint report, wiki page render, open-in-Obsidian path | `approve_source` / `reject_source` (= `kb review`) |
| **Tasks** | task list incl. finished, run history, next-fire times | `cancel` (task creation stays in chat via the gated `schedule_task` — one creation path, one approval) |
| **Memory** | `all_live` with provenance, type filters | `forget` (status flip, never DELETE) |
| **Hub** | connector status: providers as **key-presence booleans only**, cloud opt-in state, voice provider selection, session egress counters; **MCP: honest "not connected — future phase" placeholder** | — |
| **Lab** | `history.jsonl` gate records (verdict/cost/token trends), latest `report.md`, `baselines.yaml`, cumulative adversarial-power line | — (running evals stays a deliberate terminal ritual per ADR-0005; the UI offers a copy-the-command affordance) |
| **Meetings** | recording state, past meeting sources + review status | meeting start/stop (consent confirm before start; state always visible) |
| **Trace** | ring buffer: turn tree, tool calls + decisions, sub-agent trees, model calls w/ tokens/latency | — |

Hub is about **connectors, not agents** (agents live in Trace).

### D6 — Calm by default; Debug reveals, never enables

**Daily Mode is the default and the design center**: a single-column command center — one
chat stream, a quiet status bar (session cost · runner state · voice state), and the nav
rail. No simultaneous panels, no badge storm; the only standing badge is the Gate's pending
count. **Palette discipline: amber (`#FFB020`) is reserved exclusively for attention
states** — pending approval, quarantine review, recording. Cyan (`#17D2FF`) is
identity/active/links. Everything else lives in the obsidian/grey ramp. Tool activity in
Daily Mode is one quiet line per call ("read `notes.txt` · allow"), expandable in place.

**[Amendment 6] One primary attention surface at a time.** Daily Mode shows **at most one
primary attention surface**, with strict precedence: **pending approval › background/runner
status › passive telemetry**. A pending approval visually demotes (never deletes)
everything below it; when it resolves, the next tier surfaces. Amber only ever paints the
current top surface.

**The visibility invariant** (the "no hidden background actions" non-negotiable): every
side-effecting action — foreground, sub-agent, or background job — produces at least a
summary line in Daily Mode while it happens, and the background runner's in-flight state is
always in the status bar. **Debug Mode** is a per-client presentation toggle that reveals
telemetry (tokens, latency, iterations, raw event payloads, trace tree inline). Pinned by
test: the route table and capability set are byte-identical with Debug on and off — it
changes *rendering only*.

### D7 — The UI as voice's screen (checkpoint §1.3, second implementation)

**`UIScreenApprover`** implements `ScreenApprover`: `available()` is a **positive** check —
an authenticated client with a live heartbeat within `ui.heartbeat_seconds` *and* a
**currently-mounted** Gate surface (or Daily with the approval banner). **[Amendment 4]**
"Declared Gate surface" is not a hello-time claim: the client streams surface state
(mount/unmount) over the WS, and the server tracks current state per connection. Anything
less ⇒ unavailable ⇒ the unchanged `VoiceApprover` denies. `confirm()` renders the full
preview in the amber modal and resolves **only** from an authenticated click carrying a
nonce that was minted against the shown modal (D3); if the client disconnects
mid-confirmation, the escalation resolves DENY (never hangs, never falls through).
`VoiceApprover`, the calm renderer, and the TTS-privacy rule are untouched — the UI plugs
into the existing seam.

The voice surface in Daily Mode is deliberately simple: **listening state** (from the
existing `on_state` observable: idle/listening/transcribing/thinking/speaking), the **heard
transcript** (displayed as untrusted input, visually distinct from typed turns), and the
**needs-screen-confirmation handoff** (the Gate modal). Captions show exactly what the
renderer sent to TTS — the safe summary, nothing else. Push-to-talk is a button (server mic
via the existing `SoundDeviceCapture`); browser-side mic capture is out of scope this
phase. Meetings: start requires an explicit consent confirmation, recording state is always
visible, and the transcript lands as an **unreviewed** KB source (existing `MeetingCapture`
path, unchanged).

### D8 — What the UI can never do (the no-bypass pins)

Each of these is a test, not a hope:

1. **No route reaches a tool or the executor directly** — the only path to any tool effect
   is `AgentLoop.run_turn` under the `PermissionGate`, or the enumerated human-authority
   service ops (D5's closed set).
2. **No approval without authentication + live WS + valid nonce**; no approval via GET; no
   approval from a replayed cookie or replayed nonce; no approval from a stale page.
3. **No voice-only approval**: the only resolver for a voice escalation is the
   authenticated screen click carrying a modal-bound nonce (spoken "yes" test carried over
   from Phase 7, now through the full UI path).
4. **"Always" is refused** for `schedule_task` / `cancel_task` / `spawn_agent` from the UI
   exactly as from the REPL (shared module makes this structural).
5. **Debug Mode adds zero capability** (route/capability parity test).
6. **[Amendment 5] No secret crosses the wire** — a parameterized test walks *every
   registered route* and asserts the response body and headers contain none of: the launch
   token, the session cookie value, any API key value, or any env value (seeded distinctive
   fake secrets make absence meaningful, not vacuous).
7. **[Amendment 8] Emergency stop adds no authority** — the status-bar Stop maps to exactly
   two pre-existing behaviors (cancel in-flight turn = Ctrl-C parity; `BackgroundRunner.stop()`
   = finish-in-flight-then-stop), Resume = `runner.start()`. No new gate path.

### D9 — No new eval scenarios — and why that's honest

The eval harness measures *model+system behavior through the agent loop*; the UI adds no
new model path, tool, or unattended behavior, so there is nothing for a scenario to measure
that Phases 5–7 don't already gate. UI safety properties are deterministic
protocol/authorization properties — exactly what unit and integration tests measure best.
The phase therefore ends with a **PASS→PASS re-certification** of the existing live gate
(chunked, `--no-judge`, `--compare` vs the Phase 7 rev) to prove the refactors (notably the
`_persist_always` extraction) moved nothing — plus the full keyless suite, which does carry
the new UI pins.

### D10 — Testing strategy

All keyless, in-process: FastAPI's `TestClient` (httpx ASGI) for routes, its WebSocket test
support for streams, `FakeClient` for turns, the fake voice stack from Phase 7 for
escalations. The auth matrix (no token / wrong token / wrong Host / wrong Origin / dead WS),
the nonce/replay matrix, the approver parity matrix (UI vs REPL over the shared module), the
screen-available fail-closed matrix, the route-closed-set pin, the secret-absence sweep, and
the Debug-parity pin are the load-bearing tests and land **before** the frontend exists.
Frontend JS carries no safety logic (it renders and clicks) — a deliberate architectural
choice so the untested layer is the untrusted-with-nothing-to-lose layer.

## 2. Task list — Milestone 8 (for Opus 4.8, in order)

Same discipline as Milestones 1–7: each task ends green (`ruff check` + `pytest`, shown),
commits with explicit paths (never `git add -A`; `docs/PLAN.md` and the stray
consent-checkpoint file stay untouched), appends 3–5 learning-note bullets. **Safety
surfaces before capability**: auth (2) and approvals/Gate (3) land before turns (4), voice
(6), or any frontend.

1. **Plan doc + ADR-0008 + config + extra (keyless).** Commit this plan; ADR-0008; `UIConfig`
   (loopback-only validator); pyproject `ui` extra; settings.yaml block. *Tests*: config
   defaults; non-loopback host refused; loopback hosts allowed.
2. **Auth + server core.** App factory (127.0.0.1); per-launch token + **clean-URL cookie
   exchange (303, no-store)**; Host/Origin guards; CSP + hardening headers incl.
   **`Referrer-Policy: no-referrer` and NO CORS**; `/api/health`; WS hello + heartbeat; the
   secret-absence test harness. *Tests*: full auth matrix; DNS-rebinding Host rejected; WS
   without session refused; token never in logs/responses; clean-URL exchange; header sweep
   (`Referrer-Policy` present, `Access-Control-Allow-*` absent everywhere).
3. **Approval extraction + `UIApprover` + Gate API + nonce.** Extract
   `_persist_always`/`_NEVER_PERSIST` to `permissions/approvals.py`; REPL delegates
   (parity pin); `UIApprover` pending queue + resolve routes (auth + live-WS); **decision id
   + single-use nonce (mint-on-modal-ack, single-use, invalidate-on-disconnect)**; `channel=ui`
   audit; sub-agent labeling + pattern grants preserved; policy + today's-audit readers.
   *Tests*: parity matrix; approve/deny/always-narrow; never-persist refused; resolution
   requires live WS; **replay/nonce matrix**; audit lines.
4. **Turn engine + event stream + emergency stop.** `UiSession` (shared turn lock); event
   serializer incl. `ToolDecision` + unwrapped `SubAgentEvent`; ring buffer; submit/cancel;
   **runner pause/resume routes preserving awaits-in-flight semantics**; runner status.
   *Tests* (FakeClient): turn round-trip; denied call visible; cancel resets; serializer
   schema pinned; emergency stop preserves in-flight semantics + no new capability.
5. **Read-model APIs + secret-absence sweep.** The D5 table exactly; audit reader; Obsidian
   path. *Tests*: each endpoint keyless; **route-closed-set pin**; **full secret-absence
   sweep** across every registered route.
6. **Voice on the UI.** `UIScreenApprover` (liveness + **current-state** Gate mounted +
   modal-ack-gated nonce; disconnect ⇒ deny); voice status stream; push-to-talk; captions =
   renderer safe output; meeting start/stop with consent → unreviewed source. *Tests*: the
   strengthened fail-closed matrix (hello-claim alone unavailable, unmount unavailable,
   no-modal-ack ⇒ no-nonce, mid-confirm disconnect ⇒ DENY); scripted spoken "yes" cannot
   approve; escalation stays the safe line; meeting lands `unreviewed`.
7. **Frontend shell + Daily Mode + Gate (priority surfaces first).** Nav rail + obsidian
   theme (amber = attention only); chat stream w/ quiet tool lines; status bar (cost ·
   runner · voice) + **Stop/Resume**; approval banner + amber Gate modal; Gate screen;
   **one-primary-attention-surface rule**; **per-screen ES modules**; KAIRO mark inline.
   *Tests*: assets under CSP; no-external-URL grep pin; every data route auth-gated.
8. **Frontend advanced screens + Debug Mode.** Vault/Tasks/Memory/Hub/Lab/Meetings/Trace;
   Debug toggle. *Tests*: **Debug-reveals-never-enables** (route/capability parity);
   review/cancel/forget through the real endpoints (integration, keyless).
9. **CLI wiring + shutdown + docs.** `kira --ui` (prints tokened URL once; `ui.enabled`
   gate mirrors `--voice`); REPL-parity graceful shutdown; README + architecture.md;
   settings comments. *Tests*: disabled path prints hint and touches nothing; composition
   test (approver is `UIApprover`, lock shared) mirroring `test_voice_cli.py`.
10. **No-regression certification (LIVE, cheap).** Full `pytest` + `ruff`; live gate via
    `kira eval gate --profile live-chunked --no-judge --compare 6ab6995` — **all 36
    scenarios PASS→PASS**; one history line; short note appended to the baseline doc.
11. **Live demo verification ritual + learning notes.** Documented walkthrough against the
    real server: risky ask → Gate approve (audit verified); voice push-to-talk →
    escalation → screen confirm → deny-on-disconnect spot check; Vault `kb review`; task
    cancel; memory forget; Lab renders history; Debug toggle; **attention-precedence +
    emergency-stop checks**; results in the plan doc; `docs/learning-notes.md` bullets.

## 3. Verification

1. `uv run pytest` — all green, keyless (target ~800+ tests including the new pins).
2. The **auth matrix**, **nonce/replay matrix**, and **route-closed-set** pins pass — and
   deleting the auth middleware makes them fail (the tests test something).
3. The **fail-closed screen matrix**: voice escalation with UI connected+alive+modal-shown
   ⇒ modal; disconnected, stale heartbeat, unmounted Gate surface, no modal-ack, or
   mid-confirm disconnect ⇒ DENY.
4. The **secret-absence sweep** passes across every route (seeded canary secrets absent).
5. Live gate PASS→PASS vs `6ab6995` in one history line.
6. The demo ritual completes with every approval visible in Gate *and* in the JSONL audit
   log with `channel=ui`.

## Non-negotiables (for the Opus handoff)

1. **The UI cannot bypass `PermissionGate` or `VoiceApprover`** — every tool effect flows
   through `AgentLoop` under the gate; voice escalation flows through the unchanged
   `VoiceApprover`; the D5 mutation set is closed and route-table-pinned.
2. **Approvals are explicit and auditable**: full payload + reason shown before consent;
   once/always-narrow/deny only; the shared persistence module keeps UI and REPL semantics
   identical; every resolution audit-logged with its channel, **replay-proof (nonce bound
   to the live session and the shown modal)**; "always" refused for
   `schedule_task`/`cancel_task`/`spawn_agent`.
3. **Voice prepares, screen commits** — the UI screen is available only when positively
   confirmed (auth + liveness + currently-mounted Gate surface), and anything less denies.
   No UI change weakens the Phase 7 checkpoint.
4. **No hidden background actions**: the visibility invariant holds in Daily Mode; Debug
   Mode changes presentation only (pinned). Daily Mode shows one primary attention surface
   at a time (approval › runner › telemetry).
5. **Safety surfaces before capability**: Tasks 2–3 land before 4, 6, or any frontend; no
   new unsafe capability at any point (this phase adds none, and stays that way).
6. **Amber is approval/attention only.** Calm is a product requirement, not decoration —
   and calm never means hidden.

## Open questions / recorded tradeoffs

- **One host at a time** (SQLite single connection): REPL *or* UI *or* voice per process
  lifetime. Concurrent surfaces over one daemon is a future phase (it needs an IPC/auth
  story of its own).
- **Remote/mobile access explicitly out**: loopback-only is fail-closed; a real remote
  story needs TLS + real identity, not a bigger allowlist.
- **MCP/connectors**: Hub renders status honestly ("not connected — future phase"); wiring
  MCP is new capability and belongs to a phase with its own permission checkpoint.
- **Server-side mic** (existing capture path) rather than browser mic — less new surface;
  revisit if the workstation ever runs detached from the machine with the microphone.
- **Lab is view-only**: launching evals from a button invites casual, unrecorded gate runs;
  the deliberate terminal ritual (ADR-0005) stands.
- **Vanilla static frontend** trades framework ergonomics for zero toolchain, zero supply
  chain, and a testable Python safety layer. Revisit only if screen complexity outgrows it.

## Live verification (Task 11)

Executed against the real server + real model at commit `0a9a023` (workstation UI on
`127.0.0.1:8787`):

- **Server boots + auth flow (real HTTP).** `kira --ui` prints the tokened loopback URL;
  `GET /api/health` → `{"status":"ok","app":"kairo"}`; `GET /?token=…` → `303 → /` with an
  `HttpOnly; SameSite=Strict` session cookie (clean URL, no token in history).
- **Gate approve/deny + audit (real model, deterministic in-loop harness).** A real
  `AgentLoop` turn asked to `write_file` genuinely **paused at the Gate**; resolving through
  the real `POST /api/approvals/{id}/resolve` with a single-use nonce bound to a live
  connection **committed the write on APPROVE** (`demo.txt` created) and **blocked it on
  DENY** (`secret.txt` absent). Both resolutions were audited `channel=ui`
  (`write_file/allow/ui`, `write_file/deny/ui`).
- **No secret on the wire.** `GET /api/hub` returns provider *presence booleans* only; the
  launch token and the Anthropic key never appear in any response (asserted live + by the
  route-wide secret-absence sweep).

The remainder of the ritual (voice escalation fail-closed, `kb review` / task-cancel /
memory-forget flows, one-attention-surface + emergency stop, Debug-reveals-never-enables) is
exercised by the keyless suite through the *real ASGI stack* (FakeClient replaces only the
model's token generation) — 40+ UI tests across auth, the nonce/replay matrix, the turn
stream, read models, the fail-closed screen, and the route-closed-set pin.
