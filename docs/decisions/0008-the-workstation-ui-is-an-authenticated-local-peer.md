# ADR-0008: The workstation UI is an authenticated local peer; approvals are explicit, audited, and cannot be bypassed

- **Status:** Accepted
- **Date:** 2026-07-07
- **Amended:** 2026-07-14 (singleton owner identity replaces direct launch-token sessions)
- **Context phase:** Phase 8 (workstation UI)

## Context

Phase 8 adds a local web workstation — the third interface after the REPL and voice. The
goal is explicitly *not* new autonomy: it is to make Kairo usable, calm, observable, and
safe. That framing is the whole risk. A web surface is the easiest place to silently widen
the authority boundary, because the two things that made the REPL safe don't automatically
transfer:

- The REPL's authority came from **being the TTY** — a synchronous, physically-present,
  authenticated human at the keys. A port on `127.0.0.1` has none of that for free: it is
  reachable by any local process, and via CSRF / DNS rebinding by any web page the user's
  browser visits. An unauthenticated `POST /api/approvals/{id}/approve` is a remote
  code-execution-by-approval bug wearing a product skin.
- Voice's safety (ADR-0007) rests on "screen available" being *precise and fail-closed*
  (checkpoint §1.3). The workstation is the second implementation of that screen. A naive
  "a server is running, so a screen is available" turns *voice prepares, screen commits*
  into *voice commits*.

This ADR records the decisions that make the UI a strictly-non-widening peer. It does not
restate the Phase 7 voice checkpoint
([`docs/PLAN-7-voice-permissions-checkpoint.md`](../PLAN-7-voice-permissions-checkpoint.md))
or ADR-0002–0007; it composes with them. Where code and a prior safety contract conflict,
the prior contract wins.

## Decision

### 1. The UI is an interface, not a new authority — one approval path

`kira --ui` is a host process and peer of the REPL/voice. It drives the same `AgentLoop`
through the same two public seams — the event stream out, the injected `Approver` in — and
reaches nothing else (not the gate internals, not the tools, not the executor). The safety
consequence is structural, identical to voice's: **there is exactly one approval path**, so
no amount of HTTP/WebSocket plumbing can bypass the escalation. Every existing floor (gate,
sensitive-path, write allowlist, shell-metacharacter rule, unattended `HARD_DENY`,
sub-agent double gate, reflection firewall) applies unchanged beneath a UI turn. The UI can
only *narrow*, never widen.

### 2. The private-admin-console contract: authenticate, or refuse to serve

The server earns TTY-equivalent authority or does not run:

- **Loopback bind only.** A non-loopback `ui.host` is a config error (enforced in
  `UIConfig`, fail-closed). Off-box access needs TLS + real identity — a future phase, not
  a YAML edit.
- **Singleton owner identity.** First run requires the per-launch 128-bit token, but the token is
  consumable exactly once and creates only a 10-minute, purpose-bound enrollment grant via a
  **clean-URL `303` redirect** (`Cache-Control: no-store`). The owner then chooses the only account
  name and an Argon2id-protected passphrase. After enrollment, ordinary access is passphrase login;
  the launch token may create only a recovery grant and never application authority.
- **Digest-only durable sessions.** Successful enrollment/login/recovery creates an opaque
  `HttpOnly; SameSite=Strict` cookie whose SHA-256 digest, credential epoch, 30-day sliding idle
  deadline, and 90-day absolute deadline are stored in SQLite. Recovery changes the passphrase,
  increments the epoch, and revokes all older sessions atomically. Password step-up rotates the
  session id. Neither a cookie bearer, passphrase, launch token, nor grant bearer is written to
  disk.
- **Every workstation mutation, data GET, app asset, and WebSocket require the owner session.**
  Anonymous access is limited to health plus the exact setup/login/recovery shell and its three
  authored assets. Open WebSockets revalidate the durable session before every frame; logout,
  recovery, and step-up invalidate live approval nonces and browser workspaces immediately.
- **Host-header allowlist** (defeats DNS rebinding) and **Origin check** on the WebSocket
  and mutating routes (defeats CSRF even if a cookie's scope leaks).
- **`Referrer-Policy: no-referrer`, CSP `default-src 'self'`, and no CORS whatsoever** (no
  `Access-Control-Allow-*` header on any route). No external asset can load; the frontend
  is fully self-contained (pinned: no `http(s)://` in `static/`).

### 3. One persistence truth; replay-proof, visibility-gated approvals

The narrow-persist discipline (shell rules by prefix; writes by *resolved* parent dir with
over-broad refusal; `_NEVER_PERSIST = {schedule_task, cancel_task, spawn_agent}`) is
extracted from the REPL into `permissions/approvals.py` and shared, so UI and REPL approval
semantics cannot drift. The REPL's existing approval tests are the parity pin.

An ASK becomes a Gate item showing the **full untruncated payload** and the gate's reason,
resolvable only as **Approve once / Always allow (narrow) / Deny**. Beyond authentication,
a resolution requires:

- a **live WebSocket** (heartbeat within `ui.heartbeat_seconds`) — a cookie replay from a
  dead client cannot approve; and
- a **single-use, per-approval nonce** minted server-side and delivered *only* over that
  live WS, invalidated on use and on disconnect/reconnect — so a click cannot be replayed
  from a stale page; and
- **proof of visibility**: the nonce is issued only after the client acks that the approval
  modal for that decision id is on screen — you cannot approve what was never shown.

Every resolution is audit-logged with `channel=ui` through the existing
`permission_resolved` flow. Sub-agent ASKs are labeled with the child's title and keep
their run-scoped *pattern* grants (host / dir-prefix, never blanket `run_shell`/`write_file`,
never persisted).

### 4. Screens are read models; mutations are the existing human-authority set, and it is closed

The only state-changing operations the UI exposes are ones a human could already trigger:
submit/cancel a turn, resolve an approval, `kb review` (approve/reject a source), cancel a
task, forget a memory, start/stop a consented meeting, and pause/resume the background
runner. This set is **closed and pinned by a route-table test** — a new route that reaches
a tool or the executor directly, or any mutation outside this list, fails the test. Task
creation stays in chat through the gated `schedule_task` (one creation path, one approval).
Running evals stays a deliberate terminal ritual (ADR-0005); the UI only *views* results.

### 5. The UI as voice's screen — positive, fail-closed availability

`UIScreenApprover` implements the `ScreenApprover` protocol. `available()` is a **positive**
check: an authenticated client, a live heartbeat, and a **currently-mounted** Gate/approval
surface (tracked from live mount/unmount messages, not a one-time hello claim). Anything
less — no client, stale heartbeat, unmounted surface, uncertainty — is *unavailable*, and
the unchanged `VoiceApprover` denies. `confirm()` resolves only from an authenticated click
carrying the modal-bound nonce; a mid-confirmation disconnect resolves **DENY**, never
hangs. `VoiceApprover`, the calm renderer, and the TTS-privacy rule are untouched. Voice
never *assumes* a screen just because a server is up.

### 6. Calm by default; Debug reveals, never enables

Daily Mode is the default: one chat stream, a quiet status bar, one standing badge (Gate
pending count). Amber is reserved for attention states only (pending approval, quarantine
review, recording). Daily Mode shows **one primary attention surface at a time** —
precedence approval › runner status › passive telemetry. The **visibility invariant**
holds: every side-effecting action (foreground, sub-agent, or background) surfaces at least
a summary line while it happens; the runner's in-flight state is always in the status bar.
**Debug Mode is a presentation toggle only** — it reveals telemetry (tokens, latency, raw
payloads, the trace tree) but the route table and capability set are byte-identical with it
on or off (pinned by test). Calm never means hidden.

### 7. Emergency stop maps to existing brakes, adds no authority

A standing Stop in the status bar maps to exactly two pre-existing behaviors: cancel the
in-flight turn (Ctrl-C parity) and `BackgroundRunner.stop()` (finish-in-flight-then-stop,
never a torn write); Resume calls `runner.start()`. It is the REPL's Ctrl-C + shutdown
discipline given a button — no new gate path, no new capability.

### 8. No new eval scenarios; re-certify the gate instead

The UI adds no model path, tool, or unattended behavior, so there is nothing new for a
scenario to measure that Phases 5–7 don't already gate. UI safety is a set of deterministic
authorization/protocol properties, best measured by unit/integration tests (the auth,
nonce/replay, screen fail-closed, route-closed-set, secret-absence, and Debug-parity pins).
The phase ends by re-running the existing live gate (`--compare` the Phase 7 rev) to prove
the one behavioral refactor — extracting `_persist_always` — moved nothing: all scenarios
PASS→PASS.

## Consequences

- The UI ships as a strictly-non-widening peer: authenticated, loopback-only, approvals
  replay-proof and visibility-gated, voice screen fail-closed, the mutation set closed and
  pinned. ADR-0002–0007 stay intact.
- A little more friction than a "just click yes anywhere" dashboard — deliberately. The
  friction (a real session, a live socket, a shown modal, a one-time nonce) is the feature.
- One host at a time (SQLite single connection): REPL *or* UI *or* voice per process
  lifetime; a shared daemon is a future phase with its own IPC/auth story.
- The frontend carries no safety logic — it renders and clicks — so the untested layer is
  the one with nothing to bypass. All enforcement is in testable Python.

## Alternatives considered

- **Trust localhost (no auth).** Rejected — localhost is not private; CSRF/DNS-rebinding
  make an unauthenticated approval route a remote-approval bug.
- **A frontend framework + build step.** Rejected for this phase — it adds a supply chain
  and a toolchain, and tempts safety logic into untested JS. Vanilla ES modules keep
  enforcement in Python and the surface offline/self-contained.
- **"Screen available = server is running" for voice.** Rejected — it silently converts
  *voice prepares, screen commits* into *voice commits*. Availability must be positively
  confirmed (auth + liveness + mounted surface) and fail-closed.
- **A one-time WS-hello capability claim for the Gate surface.** Rejected as too weak — a
  page can claim it once and navigate away; availability tracks live mount/unmount and the
  nonce is bound to the actually-shown modal.
- **Launching evals from the UI.** Rejected — it invites casual, unrecorded gate runs; the
  deliberate, recorded terminal ritual (ADR-0005) stands; the UI views results only.
