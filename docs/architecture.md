# Kira Architecture

This document preserves the detailed Phase 1–9 foundation—the agent loop, safety gates,
persistence, knowledge, delegation, voice, workstation, and daily connectors—and amends current
behavior where later phases changed the operator contract. The committed product now includes
Phases 10–15.6 and Phase 16 Tasks 1–9. Development is stopped at mandatory **Checkpoint K**:
the five proposal-only dreaming jobs exist for attended runs, but none is scheduled unattended.

Dated plans remain historical design evidence rather than current operator instructions. For the
foundation plans see [`PLAN.md`](PLAN.md),
[`PLAN-2-memory.md`](PLAN-2-memory.md), [`PLAN-3-tasks.md`](PLAN-3-tasks.md),
[`PLAN-4-knowledge.md`](PLAN-4-knowledge.md), [`PLAN-5-evals.md`](PLAN-5-evals.md),
and [`PLAN-6-multi-agent.md`](PLAN-6-multi-agent.md); for the reasoning behind each
decision see [`learning-notes.md`](learning-notes.md).

The product-facing subsystem names are Kira **Core** (`core/`), **Command** (`cli/`), **Gate**
(`permissions/`), **Vault**
(`memory/` + `knowledge/`), **Trace** (`observability/` + audit tables), **Lab**
(`tests/evals/`), and **Orchestrator** (`agents/`). The canonical import namespace, product, and CLI
identity are all Kira / `kira`; only the explicitly documented command, database, and log-read
compatibility boundaries retain the former name. Hub is now the shipped connector and
capability-readiness screen.

## Current-system map (Kira 0.1.0, schema v33)

Later phases extend the same local-first core. Model-proposed tool calls still use the shared Gate
and executor; explicitly human-owned lifecycle and connector-intent routes remain bounded service
endpoints rather than model authority.

| Area | Current responsibility | Design record |
|---|---|---|
| Projects and intelligence | Project-scoped chats, memory, tasks, artifacts, graph, sealed snapshots, durable read-only assessment jobs, and snapshot-validated reports. Project reset archives the predecessor and creates a clean successor without erasing lineage. | [ADR-0011](decisions/0011-projects-and-scoped-memory.md), [Kira User Guide](KIRA-USER-GUIDE.md) |
| Models, routing, and cost | Layered role routes, private-context and final-authority pins, local model/service ledgers, provider-specific context reuse, availability fallbacks, and pre-call refusal when exact pricing is unknown or a call would exceed budget. Manual main chat is Anthropic-only; Auto uses eligible Gemini or Anthropic tiers. | [ADR-0013](decisions/0013-model-registry-and-cost-ledger.md), [ADR-0016](decisions/0016-provider-integration.md), [ADR-0023](decisions/0023-cost-aware-routing.md) |
| Orchestration and Studio | Teams, bounded multi-stage runs, worst-case budget estimates, read-only council/review stages, one optional writer stage, exact post-synthesis crash checkpoints, and human-reviewed results. | [ADR-0014](decisions/0014-orchestration-on-spawn.md), [ADR-0015](decisions/0015-team-tool-intelligence.md), [ADR-0020](decisions/0020-ai-team-office.md) |
| Authenticated workplace | Chat-first loopback UI, owner authentication, projects, search, artifacts, connector-write audit, services, Studio, Office, graph, and one unified Notifications surface. Screens project existing services; they do not grant the model a second authority path. | [ADR-0017](decisions/0017-workstation-ui.md), [ADR-0021](decisions/0021-memory-graph.md), [ADR-0022](decisions/0022-workstation-journey.md) |
| Connectors and Remote Operator | Google reads plus previewed Calendar/Drive/Docs writes and Gmail drafts, outbound Telegram/Kakao notifications, and a separately opted-in Telegram companion with bounded reads, ephemeral conversation context, inert proposals, and exact expiring approval codes. | [ADR-0009](decisions/0009-connectors-and-egress.md), [Remote Operator](REMOTE-OPERATOR.md) |
| Persistence and lifecycle | Canonical `data/kira.db`, dual-name fail-closed migration, schema v33, owner auth, Kira backup v2 verification, quarantine-first whole-data reset, and identity-bound reset recovery. Restore is not supported. | [README](../README.md), [Kira User Guide](KIRA-USER-GUIDE.md) |
| Attention and dreaming | One read-time queue over ephemeral approvals and durable intents, graph suggestions, report pointers, proposals, reviews, and alerts; resolution of an attention row grants no source authority. Routing is center-only, digest, or minimized count-only push. Five tool-less proposal builders are attended-only before Checkpoint K. | [Phase 16 plan](PLAN-16-attention.md), [Kira User Guide](KIRA-USER-GUIDE.md) |

For the current product status and the phase-by-phase user contract, start with
[`README.md`](../README.md) and the [Kira User Guide](KIRA-USER-GUIDE.md). This map describes the
committed baseline only; uncommitted work belongs in its change set and tests, not in an
architecture claim.

## The one idea

**An agent is a loop, and everything else is infrastructure around that loop.**
The model proposes actions (tool calls); the harness executes them under a
permission gate and feeds results back; repeat until the model is done. Tools,
permissions, memory, persistence, and observability are layers wrapped around that
loop.

## Layers (strict dependency direction)

```
interfaces → core → services → foundation
```

```
┌─ interfaces ─────────────────────────────────────────────┐
│ cli/repl.py      drives a turn, prompts approvals, status │
│ cli/render.py    events → rich (panels, streaming text)   │
│ cli/jobs.py      JobRunner — runs a job as one headless turn│
├─ core ───────────────────────────────────────────────────┤
│ core/agent.py    AgentLoop.run_turn — the while-loop      │
│ core/context.py  ContextManager — compaction (view+summary)│
│ core/client.py   LLMClient interface + FakeClient         │
│ core/anthropic_client.py  live streaming client           │
│ core/prompts.py  system-prompt assembly                   │
│ core/events.py   typed events the loop emits              │
├─ services ───────────────────────────────────────────────┤
│ tools/           Tool base, registry (+ScopedRegistry),   │
│                  executor, builtin/                        │
│ permissions/     policy + PermissionGate + UnattendedGate │
│                  + SubAgentGate (the double gate)          │
│ memory/          store · embeddings · service · reflection│
│ scheduler/       store · triggers · service · runner      │
│ knowledge/       store · chunking · converters · links ·  │
│                  service (+ convert_worker subprocess)     │
│ agents/          SubAgentService + agent_runs store (P6)  │
├─ foundation ─────────────────────────────────────────────┤
│ persistence/     SQLite sessions/messages/memories/tasks/ │
│                  kb + migrations + shared write lock       │
│ observability/   structlog audit log + cost accounting    │
│ config.py        settings (yaml) + secrets (.env)         │
│ paths.py         path resolution + secret floor  ·  net.py SSRF guard │
└──────────────────────────────────────────────────────────┘
```

This diagram captures the Phase 1–9 foundation. Interfaces depend on core; core depends on
services; everything may use the foundation. Nothing lower reaches up. That separation is why the
REPL, voice controller, and shipped web workplace can drive the same loop through different event
and approval adapters without creating another execution path. Phase 3 keeps the direction honest:
the `scheduler/` runner fires
due tasks but takes job *execution* as an injected callback, so it never imports
core; the callback that builds the unattended `AgentLoop` (`cli/jobs.py`) lives in
the interface layer, where core + services are already composed.

Later composition adds `projects/`, `models/`, `routing/`, `orchestration/`, `services/`, `search/`,
`graph/`, `intelligence/`, `attention/`, and `remote/` around this foundation. These modules add
scoping, routing, coordination, and read models; model-proposed tool execution still reaches
capabilities only through the shared Gate and executor.

## The agent loop (`core/agent.py`)

One user turn, `AgentLoop.run_turn(messages)`:

1. Bind a `trace_id`; log `turn_start`.
2. **Auto-recall (once):** if memory is on, embed the new user message and build a
   background block of relevant memories for the system prompt (Phase 2).
3. **Freeze compaction (once):** if a `ContextManager` is present, decide this turn's
   cut + summary and hold them stable for the turn (Phase 2).
4. Loop, bounded by `max_iterations`:
   - Compute the **compacted view** of the messages (frozen cut + tail elision) and
     the system prompt (identity → summary → recall). The full history is untouched.
   - Call the model (streaming) with that view + tool schemas; `observe` the usage.
   - Log `model_call` with token usage + computed cost.
   - Append the assistant's content blocks **verbatim** (thinking/tool_use round-trip).
   - If `stop_reason != tool_use` → emit `TurnCompleted`, return.
   - Otherwise, handle tools: resolve permission for each **sequentially** (orderly
     prompts), then execute the approved ones **in parallel**; append one
     `tool_result` block per `tool_use` id.
5. Stop on `max_iterations`, or on `max_context` if a turn can't fit even after
   elision.

Invariants (each a classic agent bug when violated): tool errors/denials/unknown
tools become `is_error` results the model recovers from; exactly one result per
call; assistant blocks appended unchanged; results truncated to protect context;
the iteration guard prevents runaways. The memory/context collaborators are
**optional** — with both absent, the loop is byte-for-byte the Phase 1 loop.

## Model boundary (`core/client.py`, `core/anthropic_client.py`)

The loop talks to an `LLMClient` interface — never the SDK directly. `FakeClient`
(scripted) backs deterministic model-facing tests; `AnthropicClient` is the foundational live
streaming implementation (adaptive thinking + `output_config.effort`, SDK retries,
content-block serialization that preserves thinking signatures). Going live changed
zero loop code.

The current `models/` registry and `routing/` policy resolve that same interface per role and turn.
`ModelRegistry` applies partial role-route overlays in the order built-in defaults ← settings ←
project ← run. Planner and judge remain final-authority roles, utility remains a private-context
role, and all three are pinned to a trusted provider; coder must resolve to a tool-capable provider.
An invalid or unavailable role route fails closed instead of silently downgrading.

Interactive routing is a separate axis from Gate permission mode. Routing is Auto or Manual
(default Auto); permission mode is Plan, Approval, or Auto (default Approval). Manual main chat
accepts only the allowlisted Fable, Opus, Sonnet, and Haiku Claude ids. Auto sends simple,
non-sensitive, tool-free work to Gemini Flash; simple work needing tools to Haiku; private,
personal, email, calendar, finance, coding, or hard work to Sonnet; expert work to Opus; and hard or
expert planning to Fable. Every Auto tier is private-context eligible. OpenAI is private-context
eligible in the provider catalog but is neither an Auto tier nor a manual main-chat destination;
Qwen, DeepSeek, and Z.ai remain scoped non-private workers and never hold final decision authority.

Classifier failure or uncertainty escalates to Sonnet. An unavailable Gemini simple tier falls back
to Haiku; other unavailable Auto tiers fall back to Sonnet. Pricing is a different boundary: capped
browser chat preflights the classifier and selected model calls, and refuses an unknown exact-model
price or projected over-cap call before spending instead of rerouting to a more expensive model.
The shipped chat defaults are eight iterations, 4,096 output tokens, and a $0.75 per-turn hard cap;
an explicit zero disables that dollar cap.

Every catalog provider is visible to readiness views. Optional providers become route-eligible only
when catalog-known, explicitly enabled, credential-present, and backed by at least one priced model;
adapter compatibility is declared statically by the catalog's `api_style`, not discovered by a live
probe. Interactive startup still requires `ANTHROPIC_API_KEY`. Context reuse follows each provider's
declared caching mode and passes only the stable prompt prefix at the stable/volatile seam. It never
changes model, tool, privacy, or Gate authority, and turning it off preserves the pre-feature request.

## Tools (`tools/`)

A tool is a `Tool` subclass with a name, description, a pydantic `Params` model
(which generates the JSON schema *and* validates input), a `permission_default`,
and an async `run`. The `ToolRegistry` auto-discovers concrete tools under
`tools/builtin/` and injects a `ToolContext` (for config/secrets). The
`ToolExecutor` is the guarded boundary: validate input → run with a timeout →
capture errors → truncate output. Built-ins: `read_file`, `write_file`,
`list_dir`, `glob_search`, `run_shell` (pwsh), `web_search` (Tavily), `web_fetch`
(httpx + trafilatura). Filesystem reads are bounded to a byte ceiling read
straight from disk (memory safety), and list/glob output is capped.

Later phases add project, connector, graph, service/search, orchestration, and proposal tools through
the same registry contract. Availability is structural: disabled, unpriced, incompatible, or
credential-less capabilities do not silently become callable. The five current dreaming builders
make one tool-less model call; a separate open-ended dreaming cage can expose only the available
subset of its explicit 5-tool, non-egress, non-private read allowlist.

## Permissions (`permissions/`)

`Policy` (from `config/permissions.yaml`) is data; `PermissionGate` interprets it.
Base decision precedence: per-tool entry → the tool's own default → policy default.
Refinements: a **sensitive-path floor** (`kira/paths.py`) denies reads and writes
of secrets/keys regardless of policy; filesystem writes are checked against an
allowlist (can only tighten); filesystem reads outside the project/read allowlist
escalate to an explicit approval; shell commands match longest-prefix rules at a token
boundary, with an `allow` downgraded to `ask` when shell metacharacters could chain
or redirect; a tool-level `deny` is absolute. The gate and the filesystem tools
resolve paths through the *same* `resolve_path` (against the workspace root), so a
decision and the action it authorizes always name the same file. The gate only
decides — the interface prompts the human and the loop runs the tool. "Always allow"
persists the narrowest rule, and refuses to persist an over-broad write directory
(and refuses entirely for `schedule_task`, `cancel_task`, and `spawn_agent`).

For unattended runs, `UnattendedGate` wraps the normal gate. Its non-reopenable `HARD_DENY` set is
exactly `schedule_task`, `cancel_task`, `remember`, `forget`, `spawn_agent`,
`gmail_create_draft`, `gmail_update_draft`, `send_notification`, `calendar_create_event`,
`calendar_update_event`, `calendar_cancel_event`, `drive_create_doc`, and `drive_update_doc`.
A standing `allow` for shell/file/knowledge writes or any egress tool is demoted unless it is in the
explicit scheduler opt-in set; ordinary scheduler ASKs reach a `HeadlessApprover` that never reads
stdin and denies. The optional parking path instead stops before any tool in that assistant batch
executes and durably stores the exact call and transcript continuation. It grants nothing: a later
one-use resolution must claim the bound continuation before execution, and Remote Operator demotes
otherwise-eligible standing side-effect/egress allows to these exact ASKs. The key insight
([ADR-0003](decisions/0003-unattended-runs-deny-and-demote.md)) is that a policy `allow` resolves
before any approver, so denying ASKs alone is not an unattended safety boundary.

For delegated sub-agents (Phase 6), `SubAgentGate` (`permissions/subagent.py`) is the
*second* gate — it wraps whichever gate the parent used and can only narrow: hard-deny
`spawn_agent`+meta tools (depth 1), enforce the run's tool scope, delegate to the inner
gate so every floor survives composition, then upgrade an ASK only for a run-scoped
**pattern** grant (`web_search`/`query_knowledge_base` for that run, a `web_fetch` host, or a read
directory-prefix; `run_shell`/`write_file` never grantable; nothing persisted). See
[ADR-0006](decisions/0006-sub-agents-are-scoped-visible-and-doubly-gated.md).

Later approval transports keep the same rule: transport is not authority. Browser ASKs require a
live mounted surface plus a single-use connection-bound nonce. Remote Operator can resolve only an
exact expiring capability bound to one inert proposal or parked tool call. Resolving an attention
row changes queue metadata only; source acceptance still uses the source's existing gated route.
Current dreaming builders pass `tools=[]`, enforce a hard budget, and can produce only untrusted
artifacts and attention proposals through bounded host code.

## Memory (`memory/`) — Phase 2

Three tiers around the loop. **Working memory** is the message list, compacted by
`core/context.py`: it produces a per-request *view* (token-weighted cut at a real
user turn; oldest tool-result bodies elided when a single turn overflows) while the
full history stays the source of truth, and the dropped prefix is represented by a
configured `models.utility` summary carried in the system prompt (frozen per turn, persisted
so `--resume` doesn't re-summarize). **Long-term memory** is an embeddings-indexed
`memories` store: `MemoryStore` (unit-normalized float32 vectors, cosine = one numpy
matmul) under a `MemoryService` that owns remember (with configured utility-model dedup
adjudication), recall, and auto-recall. The `Embedder` seam (Voyage live, a
deterministic fake in tests) mirrors the `LLMClient` pattern. **Episodic memory** is
the transcript; on exit `memory/reflection.py` distills durable facts via a forced
tool call — after **stripping tool-result bodies** so untrusted fetched content
can't be laundered into permanent memory (see [ADR-0002](decisions/0002-reflection-writes-bypass-the-gate.md)).
Semantic memory is optional: without `VOYAGE_API_KEY`, its tools and automatic recall are absent;
transcript persistence and context compaction still operate.

## Scheduling (`scheduler/`) — Phase 3

Lets the agent act without being prompted. `TaskStore` persists `tasks` (kind
reminder|job, a schedule, provenance, a lifecycle status) and `task_runs` (per-
execution history) — two deliberately-split status machines, nothing ever deleted.
`triggers.py` is the only APScheduler user: pure `validate` / `compute_next` over
its cron/interval/date triggers (we take the hard timezone/DST math, not its
scheduler — SQLite is the single source of truth). `TaskService` owns the semantics
on an **injected clock** (so the whole lifecycle unit-tests without sleeping): first-
fire computation, due classification (fire / fire-late / missed within a grace
window), advancement from the *scheduled* time (no interval drift), a consecutive-
failure cap, and a startup sweep that aborts crash-orphaned runs without re-running
them. Deliberately parked `pending` or claimed `approved` continuations survive restart and are
excluded from that orphan sweep; only ordinary `approval_state='none'` running rows are aborted.
`BackgroundRunner` is an asyncio wake loop. Ordinary reminders and jobs are admitted under the
shared **turn lock**, so their model turns cannot overlap an interactive turn; the Daily Digest is
the deliberate exception and keeps only its short persistence/notification windows under that lock
so connector backoff cannot freeze the workplace.
reminders notify-then-record (at-least-once), jobs open their run row first (crash-
detectable). A **job** runs as one unattended `AgentLoop` turn in a fresh
`kind='task'` session behind the [ADR-0003](decisions/0003-unattended-runs-deny-and-demote.md)
gate. If parking is enabled, each resumed ASK needs a separate one-use exact resolution; approved
earlier calls remain bound to the saved continuation. Optional: `scheduler.enabled: false` removes
the runner and task tools.

## Knowledge (`knowledge/`) — Phase 4

An external, Obsidian-compatible Markdown knowledge base that compounds over time.
`converters.py` is the **only** third-party-converter import site: deterministic
first (`.md`/`.txt` passthrough; MarkItDown with plugins/LLM off; Docling optional;
web via trafilatura), and it runs the parser in a **killable subprocess**
(`convert_worker.py`) with input + decompression-bomb caps — `asyncio.to_thread`
can't cancel a runaway parser. `chunking.py` is a pure, fence-aware heading chunker;
`links.py` extracts/resolves Markdown + `[[wikilinks]]`. `KnowledgeService` runs the
pipeline — ingest (raw artifact first, then convert, then DB row, so a crash orphans
a file, never dangles a row), query (cited excerpts framed as untrusted, NOT
instructions), `write_page` (jailed to the wiki dir, front-matter generated from DB
state), lint, and rebuild. `KnowledgeStore` reuses the memory vector pattern
(unit-float32 BLOBs, matmul) filtered by `embedding_model`. Two safety properties are
structural (see [ADR-0004](decisions/0004-converters-are-gated-io-and-the-kb-is-a-contained-injection-sink.md)):
conversion is gated like a read (the `ingest_source` `path` hits the sensitive-path
floor) and sandboxed; and the KB is a contained injection sink — provenance is
DB-derived (never from content), `write_file` is denied under the KB dir, and
unattended ingests are quarantined `unreviewed` until `kb review`. Optional:
`knowledge.enabled: false` (or no `VOYAGE_API_KEY`) ⇒ no KB tools.

## Agents (`agents/`) — Phase 6 (Kira Orchestrator)

Delegation: the primary agent spawns scoped sub-agents. `SubAgentService.spawn` is the
`JobRunner` pattern specialized for interactive, depth-1 delegation — it builds one child
`AgentLoop` turn from the parent's client/executor and a `ScopedRegistry` (the child's
tool allowlist), with `memory=None` (context isolation: no parent history, no auto-recall)
and a fresh context manager. The child runs under a semaphore-then-timeout (queue-wait
doesn't burn the deadline) and returns a report wrapped in untrusted-content delimiters,
its header composed from the run record (a child can't forge its own status). Safety is a
**double gate**: the human approves the spawn (ASK, never "always"-able, full prompt +
scope shown), and every child tool call still passes `SubAgentGate`
(`permissions/subagent.py`) — hard-denies `spawn_agent`+meta tools (depth 1, three ways),
enforces scope, delegates to the parent gate so every floor survives, and upgrades an ASK
only for a run-scoped **pattern** grant (host / dir-prefix; never `run_shell`/`write_file`;
or a per-run `web_search`/`query_knowledge_base` grant; never persisted). Nothing a child does is hidden: events forward to the parent sink as
`SubAgentEvent` (attempts included), the transcript persists as a `kind='subagent'` session
(never `--resume`d, never reflected), and an `agent_runs` audit row records both parent and
child trace ids. `spawn_agent` is in the unattended `HARD_DENY` set — no background swarm.
See [ADR-0006](decisions/0006-sub-agents-are-scoped-visible-and-doubly-gated.md). Optional:
`sub_agents.enabled: false` ⇒ no `spawn_agent` tool, loop byte-identical to Phase 5.

## Voice (`voice/`) — Phase 7 (Kira Command-adjacent)

Voice is an **interface**, a peer of the REPL that drives the same `AgentLoop` through the
same two seams (events out, an injected `Approver` in) — never a new authority, so every gate
and floor from earlier phases applies unchanged beneath it. The whole layer exists to honor
one contract ([the permissions checkpoint](PLAN-7-voice-permissions-checkpoint.md) /
[ADR-0007](decisions/0007-voice-is-an-untrusted-read-only-surface.md)): a microphone is a
hostile fetch, so a *finalized* transcript enters the model via `frame_transcript` in the same
untrusted-content shape as `web.py`'s `_FETCH_HEADER`, and risky actions are never approved by
voice.

The safety pieces:

- **`VoiceApprover`** (`voice/approver.py`) is the injected `Approver`. It escalates *every*
  ASK to a `ScreenApprover` and is **fail-closed** — `screen is None or not screen.available()`
  ⇒ `DENY`. It has no audio path, so a spoken "yes" cannot approve anything; the screen (the
  terminal, via `TerminalScreenApprover`, reusing the REPL's own `_call_summary`) commits by an
  authenticated keystroke. This is the one-approval-path guarantee: the single seam means voice
  can't bypass the escalation.
- **`VoiceSession`** (`voice/session.py`) runs one turn per finalized utterance (finalized-only:
  a partial/empty transcript never drives a turn or a tool), sharing the REPL's turn lock so a
  voice turn can't interleave a background job.
- **Calm renderer** (`voice/render.py`) is the `VoiceOutput`: it speaks only a safe summary,
  masks secrets, length-caps, and — the TTS-privacy rule — never voices `call.input`, tool
  firehose, secrets, commands, or message bodies. Its `announce_escalation` (structural, never
  the preview) is the only thing said on an ASK.
- **Listening** (`voice/listening.py`) is push-to-talk only; `wake_active()` returns `False`
  (wake-word deferred), and both the listener and meeting capture refuse an unattended context
  (no silent mic).
- **Adapters** (`voice/stt.py`, `voice/tts.py`, `voice/capture.py`) sit behind `STTProvider` /
  `TTSProvider` protocols and lazy-import their engines, so the base install loads without the
  `voice` extra. Local is the default (on-device faster-whisper STT; dependency-free
  `PrintSynthesizer` TTS) with no egress; cloud (OpenAI STT and TTS, or ElevenLabs TTS) is gated at
  config load by `voice.cloud_providers` and counts/logs egress. `voice/factory.py` maps config →
  adapter; the caller passes keys (the factory never reads the environment).
- **Meeting capture** (`voice/meeting.py`) files a consented recording as an **unreviewed** KB
  source (reusing the ADR-0004 quarantine) and holds no loop or scheduler, so a meeting's
  "action items" can never self-execute.

`cli/repl.py::build_voice_session` composes all of this from the REPL's already-built
collaborators (client, registry, gate, executor, context manager, memory) plus a voice loop
whose system prompt carries `VOICE_GUIDANCE`. Optional: `voice.enabled: false` ⇒ no voice
surface, REPL unchanged.

## Workstation UI (`ui/`) — Phase 8

A local web workstation, the third **interface** after the REPL and voice — a peer that
drives the same `AgentLoop` through the same two seams (events out, an injected `Approver`
in) and adds **no new authority**. The entire layer honors one contract
([ADR-0008](decisions/0008-the-workstation-ui-is-an-authenticated-local-peer.md)): a
localhost port is not private, so the UI authenticates or refuses to serve, and every screen
projects an existing service. Mutations are an enumerated, route-pinned set of human-authority
operations with service-level validation; the one read-model bookkeeping exception is Lab's
idempotent registration of the latest eval report as an artifact.

- **`auth.py` + `owner_auth.py` + `server.py`** — the private-admin-console floor:
  loopback-only bind (a non-loopback `ui.host` is a config error), one singleton owner account,
  an Argon2id passphrase verifier, and digest-only durable sessions. The per-launch token is
  consumable exactly once and can issue only a 10-minute, purpose-bound enrollment or recovery
  grant via a **clean-URL 303**; it never creates application authority. Normal login creates a
  30-day sliding / 90-day absolute `HttpOnly; SameSite=Strict` session; the idle deadline is touched
  at most once every 24 hours, while the absolute deadline never extends. Five-minute password
  step-up freshness rotates the session id. Recovery bumps the credential epoch and revokes every
  older session. Active WebSockets revalidate the durable session and logout/recovery/rotation immediately invalidate
  their approval nonces and browser workspaces. A Host allowlist (anti DNS-rebinding), an Origin
  check on mutations + the WS (anti-CSRF),
  strict CSP, `Referrer-Policy: no-referrer`, and **no CORS middleware at all**. FastAPI +
  uvicorn behind the optional `ui` extra; the frontend is hand-written static assets (no
  build step, no CDN).
- **`connections.py`** — live-WS registry with heartbeat liveness + per-connection mounted
  surfaces. Liveness is load-bearing for ephemeral browser Gate ASKs and the voice screen: each is
  resolvable only from a *currently live, watching* client. Parked task approvals, previewed write
  intents, pending graph suggestions, and durable attention rows survive according to their stores;
  live Gate ASKs and the process-local `NoticeBoard` do not become restart history. Unified
  Notifications deliberately renders both durable and current-session sources.
- **`approver.py`** — the Gate. `ApprovalManager` queues an ASK and awaits a human decision
  (like the REPL prompt, over the network), replay-proof: a resolution needs a **single-use
  nonce** minted only over the live socket *after* the client acks the modal is shown, bound
  to that connection, invalidated on use and on disconnect. `UIApprover` (the injected
  turn approver) runs the *shared* `persist_always` on "always" — identical to the REPL
  (`permissions/approvals.py`, the parity pin). `UIScreenApprover` is voice's screen:
  `available()` is a positive live-mounted-surface check, `confirm()` is fail-closed (the
  surface vanishing mid-confirmation ⇒ DENY). No spoken "yes" can approve.
- **`workspaces.py` + `session.py`** — each authenticated cookie can own isolated, opaque browser
  workspaces with their own project, session, context revision, voice state, and `UiSession` turn
  engine. Each engine drives the loop, serializes every event
  (incl. `ToolDecision` denials and unwrapped `SubAgentEvent`s) to versioned JSON for a ring
  buffer + workspace-scoped live push, and shares the process-wide turn lock used by ordinary
  interactive and scheduled `AgentLoop` turns and the orchestration writer. Daily Digest network
  work and orchestration analysis/review/synthesis are deliberate concurrency exceptions.
  Cancellation has Ctrl-C parity. The status-bar emergency stop maps only to existing brakes
  (cancel + `BackgroundRunner.stop()`).
- **`readmodels.py` + `gate_api.py`** — current surfaces are Chat, Daily, Projects, a project
  Workspace with Overview/Chats/Artifacts/Memory/Tasks/Vault/Studio/Office/Graph/Costs/Activity
  tabs, Notifications, Connectors, Meetings, Settings, and debug-only Trace/Lab. Hub exposes
  connector, provider, service, voice, MCP, and capability presence/status plus provider policy
  metadata; credential presence is boolean and credential values never cross the wire. Lab cannot
  execute evals, but `GET /api/lab` may idempotently register the latest report in `ArtifactStore`.
- **`voice.py`** — the `UiVoice` controller (status, push-to-talk, meeting capture → an
  unreviewed KB source) over the Phase-7 pieces, with the workstation as the fail-closed
  screen.
- **`static/`** — the frontend: Chat-first responsive shell, per-screen ES modules, Kira assets,
  unified Notifications, and local-only Noir/Light/Neon appearance preferences. Debug remains a
  presentation-only toggle: it reveals telemetry and adds no capability. The browser carries no
  authority policy; enforcement remains testable Python.

`cli/repl.py::build_ui_app` composes it from the REPL's own collaborators with the UI
approver seams swapped in (one shared gate; child ASKs escalate to the UI screen);
`run_ui` opens the canonical database, prints a one-use enrollment link only when setup is needed
and otherwise prints both normal sign-in and a separately labeled process-bound recovery URL. It
serves on loopback and lets the runner finish in-flight work before stopping. Browser workspace
sessions remain persisted and reflectable, but UI shutdown currently does not iterate them for
reflection; the exit reflection hook covers only the bootstrap session. Optional:
`ui.enabled: false` ⇒ no server, REPL unchanged.

## Connectors, Digest & Reporting (`connectors/`, `digest/`, `reporting/`) — Phase 9

**`connectors/`** — narrow, audited adapters to the outside world, behind the PermissionGate.
`base.py`: `ConnectorRegistry` (the `ToolContext.connectors` seam; `status()` is presence-only)
+ the `Notifier` protocol + `ConnectorError`/`ConnectorAuthError` (friendly-message-only).
`oauth.py`: shared authorization-code + PKCE loopback flow (Google + Kakao). `tokens.py`:
`TokenStore` — atomic `os.replace` write, best-effort 0600, single-flight refresh, tokens under
the sensitive-path floor. `google/`: `client.py` (bearer, 401→refresh→retry-once) plus capped
Calendar, Gmail, Drive, and Docs adapters. Gmail exposes drafts only—there is no send scope, method,
tool, or route. Gmail draft create/update tools are direct draft-only egress and require an ASK.
Calendar and Drive/Docs write tools only create typed, exact-preview intents; a separate attended
route approves and executes the stored payload, journals the result, and offers bounded rollback
where supported. `telegram.py` and
`kakao.py` are outbound notifiers; inbound Telegram control is isolated under `remote/`. `demo.py`
provides visibly badged fakes, and `factory.py` ensures demo never masks an effective live
configuration. Read tools are `reads_private` and taint the turn; connector writes are `egress` +
ASK + HARD_DENY unattended. See [ADR-0009](decisions/0009-connectors-and-egress.md).

**Data-flow permissions.** `Tool.egress`/`Tool.reads_private` ClassVars drive three rules in the
gate layer: per-turn taint in `AgentLoop` (private read ⇒ egress ALLOW→non-persistable ASK),
`UnattendedGate` egress demotion, and a cross-cutting sensitive floor (`run_shell` token-path
DENY, `glob`/`list_dir` redaction). `observability/egress.py`: the `log_egress` "what left the
box" ledger (category + destination type only).

**Telegram Remote Operator.** Remote control and its operator are independently off by default and
poll only while Kira is already running locally; this is not a wake-up or cloud-hosting path. Enabling
remote control requires exactly one positive decimal private `allowed_chat_id`. The outbound
notification chat id grants no inbound authority. Every controller start discards the pending
Telegram backlog before reporting ready. Duplicate/lower update ids, unknown chats, groups/channels,
and unsupported update shapes cannot create work; supported attachments are separately opt-in.

The ordinary text model receives no general project, shell, filesystem, connector, or approval
authority. Its isolated registry can expose only `remote_propose_work` and, when separately opted in,
`remote_live_search`; the structural gate denies every other tool. Live search normalizes at most
300 characters, makes one bounded public Tavily query per fresh message, returns at most five
untrusted results, and audits egress without logging the query. Attachment turns use a separate
empty registry, so document, image, and transcript content cannot influence live search.
Deterministic status, project, task, job, briefing, inbox, and calendar reads are handled by
host-owned paths instead of broadening model tools.

The controller may keep a bounded, RAM-only recent conversation (4 delivered turns and 6,000
characters by default) so short follow-ups are coherent; context is committed only after successful
delivery and is cleared by `/clear`, stop/restart, or expiry. Approved jobs stay bound to their stored
project and receive only the configured subset of the closed local engineering allowlist
(`read_file`, `list_dir`, `glob_search`, `write_file`, `run_shell`). Hard-denied and egress tools
remain unavailable. An otherwise-eligible standing side-effect allow is demoted to an exact ASK, so
the job parks before execution and needs a separately bound approval. Restart reconciliation restores
status monitors and interrupted proposal/task binding; it never replays Telegram messages or work.

Host-owned inbox reads return a rendered reply plus at most eight ordered Gmail IDs. Only after
Telegram successfully delivers that numbered list does the controller place those IDs and the
filter in one RAM-only, 30-minute reference slot. A narrow action-rejecting resolver maps adjacent
phrases such as “summarize each of them” or “show number 2” to the exact displayed IDs. The helper
fetches only those bodies, caps each and aggregate input, removes quoted history and links, and
produces local extractive text; nothing enters a model, tool, proposal, approval, log, or database.
Failed delivery never commits an unseen selection.

Natural action requests can produce only an inert, bounded proposal. The host may schedule it only
after the configured owner sends an expiring single-use code bound to that exact proposal. Any
risky tool call later parked by the unattended Gate needs a second exact capability bound to the
saved tool id, name, canonical input hash, and continuation. Casual chat cannot approve either step.
By default proposals expire after 30 minutes, approval codes after 15 minutes, and no more than
three approved jobs may be active at once.

**`digest/`** — the Daily Digest. `builder.py`: fail-soft collectors (schedule/email/repo/tasks/
kb, each `ok|degraded|failed`) + one **tool-less** `models.utility` summarize; UI/DB-first
delivery then best-effort notifiers; `ensure_digest_task` (host-created only). `store.py`:
minimized `digests` rows (snippets/counts/status — never raw bodies). Fired by `BackgroundRunner`
with job semantics, off the turn lock. See [ADR-0010](decisions/0010-digest.md).

**`reporting/repo.py`** — `RepoReader`, a hardened read-only git reader (argv not shell,
`GIT_CONFIG_NOSYSTEM`, hooks/fsmonitor/ext-transport disabled, timeout) for the Daily
"what changed" card. Commit subjects are untrusted data (UI escapes, digest frames).

**UI at the Phase 9 boundary.** `GET /api/daily` (repo/eval-freshness/tasks/digest/connectors) +
`GET /api/notices` (background events reach the browser via `ui/notices.py` `NoticeBoard`);
mutations `POST /api/vault/ingest` + `POST /api/digest/run` grew the then-closed set to 13. Hub gained
connector status. Daily renders Briefing/Today/What-changed/Workflows (all untrusted content via
`textContent`); the eval chip is a copy-command, never a run button (ADR-0005 stands).

## Project intelligence (`projects/`, `intelligence/`) — current extension

Project scope now reaches beyond row filtering. `projects/snapshot.py` seals one bounded,
deterministic view of the live project corpus. `intelligence/coordinator.py` serializes durable
assessment jobs through one predefined read-only orchestration shape. The feature is disabled by
default and requires standing opt-in; importing project content alone never authorizes cloud
fan-out. Queue identity is the idempotency tuple `(project_id, snapshot_hash, profile_version)`.
One durable worker owns the queue and applies the configured cost cap and bounded maximum attempts;
startup runs its reconciliation only after the host's orchestration orphan sweep.

Callers cannot choose an arbitrary team, workflow, writer, or context source. The fixed assessment
team has no shell, host-filesystem write, egress, or remediation authority, and all project labels,
model findings, and evidence are treated as untrusted data. A report is publishable only if the
completed run, job claim, and current project snapshot still match. Publication commits the
sanitized report, its attention pointer, and terminal job state together. Older reports become stale
when project content advances and may become current again only after a proven content reversion.
Suggestions remain inert UI data until a person deliberately starts a separate attended Studio run.

## Attention and dreaming (`attention/`) — Phase 16 Tasks 1–9

The `attention_queue(...)` read model unions four sources at read time: ephemeral live browser Gate
ASKs, durable previewed connector write intents, durable pending graph suggestions, and open durable
`attention_items` (including project-report pointers, proposals, alerts, and reviews). Each item
points to its source authority instead of copying it. Only `attention_items` use the metadata-only
attention resolve route; Gate ASKs, intents, and graph suggestions keep their existing approval or
review routes. Durable rows have a validated lifecycle (`open`, `done`, `dismissed`, `snoozed`,
`expired`), project isolation, trust class, priority, and idempotent dedupe key. The routing matrix
narrows delivery to center-only, digest, or minimized count/category pushes, with quiet hours and
project mutes; payload and evidence bodies never enter the push.

Dreaming is proposal-only by construction. The five current jobs use deterministic collectors and
one bounded `tools=[]` summary call. A separate cage for future open-ended review builds a fresh
registry from the available subset of `DREAMING_TOOLS`; every admitted tool is non-egress and
non-private. Unpriced work is blocked, and a hard budget halt creates one deduplicated alert. Each
artifact and attention row remains model-generated untrusted data and is never auto-injected into
later context. The five jobs—morning briefing, nightly review,
bottleneck, ROI summary, and self-improvement—are defined for one attended `kira dream run` chunk.
They are **not scheduled**. The repository deliberately stops at Checkpoint K before the scheduling
task, observation window, ADR, and closeout verification.

## Persistence (`persistence/`)

SQLite via aiosqlite. `sessions` + `messages` + `memories` + `tasks`/`task_runs` +
`kb_sources`/`kb_chunks`/`kb_wiki_links` + `agent_runs` + `digests` and the later project,
orchestration, artifact, connector-write, graph, and attention records are all local tables.
`database_identity.py` selects canonical `kira.db` inside `<paths.data_dir>` (default
`data/kira.db`) under both current and legacy instance locks. It can promote the legacy filename in
that same directory (default `data/jarvis.db`) and leave a small compatibility guard. Two
different real database identities, orphaned sidecars, unexpected links/junctions, or unknown
cutover state fail closed; if both regular names are a verified same-inode interrupted publication
and the canonical file has no sidecars, startup completes that cutover instead of discarding data.

The current committed schema baseline is v33, tracked by `PRAGMA user_version` with an ordered
migration list. v2 introduced memory; v3 tasks + `sessions.kind`; v4 the knowledge base; v5
sub-agents; later migrations add the platform records incrementally. v31 adds the singleton owner,
passkey-ready credential storage, digest-only sessions, and one-use auth grants; v32 adds durable
project-reset lineage; v33 adds the at-most-once meeting-capture origin index. v5 remains
the highest-blast-radius
migration: `sessions.kind`'s CHECK can't be ALTERed to add `'subagent'`, so the table is
rebuilt via the standard procedure (foreign_keys OFF *outside* a transaction, atomic
rebuild, `foreign_key_check`, foreign_keys ON) — child FKs survive the drop+rename. The
model is stateless — the whole conversation lives here and is reconstructed each call.
Before any DDL upgrades an older real database, `connect()` creates a fail-closed online snapshot;
snapshot failure blocks migration rather than risking the only copy of user state.
Message content is stored as JSON verbatim (thinking-block signatures survive, so a
resumed session replays to the API unchanged). Saved per turn; `--resume` loads the most
recent **interactive** session (a `kind='task'` or `kind='subagent'` session never wins,
and `subagent` never feeds reflection. A `task` transcript feeds reflection only when
`scheduler.reflect_job_sessions` is explicitly enabled; `REFLECTABLE_KINDS` is the hard ceiling
that permanently excludes child transcripts).
Authoritative records retain lineage through status transitions. Explicitly derived rows such as
`kb_chunks`/`kb_wiki_links` and later rebuildable indexes or caches may be delete-and-replaced; the
no-delete claim does not extend to those projections. The runtime intentionally shares **one
aiosqlite connection and one asyncio write lock** under its long-lived instance lock. Each
connection enables WAL, a bounded busy
timeout, foreign keys, and `synchronous=NORMAL`; multi-statement writes use `BEGIN IMMEDIATE` under
the process lock so coroutines cannot interleave or tear a session's history.

Lifecycle operations preserve recoverability rather than overwriting in place. `backup.py` creates
Kira backup format v2 with an online-consistent `kira.db`, recursive `knowledge/` and `artifacts/`,
and only `evals/history.jsonl`. Environment/configuration, logs, connector tokens, and
secret-shaped names are outside or rejected by the allowlisted inventory. The manifest contains the
canonical inventory with a SHA-256 entry for every included file; it is not a signature or MAC.
Verification is read-only, Restore is not supported, and the archive remains private because safe
filenames can still contain private user-authored content.

Whole-data reset is offline, old-owner-password and exact phrase `RESET ALL KIRA DATA` gated,
quarantines configured durable roots, and records an identity-bound manifest outside the reset
roots so an interrupted
transition can roll back without deleting either copy. External knowledge or log roots require a
second exact-path confirmation. Project reset is narrower: a fresh owner step-up, the exact project
name, and an explicit retain-repositories choice are required before one project is archived and a
clean successor is created atomically with predecessor history and lineage retained.

## Observability (`observability/`)

structlog writes redacted JSON objects to `<paths.logs_dir>/kira-YYYY-MM-DD.jsonl` (the default is
`logs/kira-YYYY-MM-DD.jsonl`). Default lifecycle is a 10 MiB active segment, up to three gzip
archives per day, and 30-day retention. Legacy `jarvis-YYYY-MM-DD.jsonl` names remain readable and
prunable for compatibility but never join the new Kira write/rotation ladder.

A `trace_id` contextvar, stamped by a processor, ties every event in a turn together (`turn_start`,
`model_call`, `permission_decision`, `tool_call`, `tool_result`, `turn_end`). Redaction recurses
through sensitive keys and inline secret-shaped strings before JSON rendering. Mapping
`tool_call.input` is reduced to `{redacted, keys, key_count}`; a non-mapping input retains only
`{redacted, type}`. Model and service ledgers attribute provider/model/service usage, including
reported cache-creation/read tokens. Cost is computed from the configured pricing table; unknown or
unpriced work remains explicitly unknown rather than becoming $0.

## Data flow of one turn

```
you → attended interface (CLI | UI | voice) → AgentLoop.run_turn(messages)
                                               │
                                               ├─ route → LLMClient.create(...)
                                               │            └─ stream events → active renderer
                                               ├─ PermissionGate.check(...)
                                               │            └─ ASK → interface approver
                                               ├─ ToolExecutor.execute(...)
                                               │            └─ ToolStarted/ToolFinished
                                               └─ append tool_results, loop
                                                            │
                                                            ▼
                                              TurnResult → persist session/messages
                                              + cost/audit records under one trace_id
```

## Evaluation (`tests/evals/`) — Phase 5

The instrument that says whether the agent works and whether a change regressed it.
Repo-native, no framework, no new deps. The harness and default gate use committed cassettes for
deterministic keyless replay; live or cassette-recording runs are separate, explicit, budget-capped
human rituals. Gate/run network modes require a finite positive `--max-cost-usd`; provider smoke
uses a $3 default cap and rejects an invalid override; the two A/B probes require explicit caps.

- **`runner.py`** — runs each scenario N times in an isolated workdir, produces a
  `ScenarioRunRecord` (tokens, latency, tool calls, **attempts**, judge verdict), and
  owns the check evaluator. Checks are input-level (`tool_not_attempted_with`,
  `tool_result_matches`, `memory_absent`, …), not just name-level, so an injection the
  gate *denied* is still visible via the `ToolDecision` event (emitted for every call
  before execution, unlike `ToolStarted`).
- **`judge.py`** — LLM-as-judge: rationale-first forced verdict (thinking-off), median
  of 3 Opus votes + majority pass, one uncounted Sonnet cross-check, specimen
  delimiters, and calibration fixtures. Failed calibration marks judge scoring JUDGE-INVALID and
  skips those scores, while deterministic checks still decide the gate. Honest that 3 votes buy
  variance reduction, not independence.
- **`report.py`** — the two-tier gate engine (safety all-N; quality 3/3-PASS /
  2/3-FLAKY-pass / ≤1/3-FAIL with two-consecutive promotion), token ceilings, judge
  floors (shadow until ratcheted), and `--compare <rev>` deltas against matching local history.
  Dirty endpoints and changed scenario hashes produce warnings while deltas remain visible; a judge
  model mismatch suppresses judge deltas. The report states its own statistical power.
- **`retrieval.py`** — drives `MemoryStore.search` / `KnowledgeStore.search` directly
  for MRR / recall@k, a determinism self-check (⇒ N=1), and a min_similarity floor
  sweep read as data with an explicit decision rule.
- **`recorder.py`** — versioned JSONL records + git-rev/dirty provenance + fail-closed
  pricing; **`baselines.yaml`** is the one committed contract (results/history are
  gitignored under `data/evals/`).
- Adversarial suite (`scenarios/adversarial/`) + under-querying probes; dual metric and
  the KB-auto-injection verdict are recorded in
  [ADR-0005](decisions/0005-how-we-know-it-works.md).
- **Delegation coverage (Phase 6):** `runner.py` unwraps `SubAgentEvent` into the *same*
  merged attempts/executed streams (child tool ids namespaced), so every existing check
  covers a child's actions and a child's `ToolDecision` attempts are observable; child
  usage/cost fold into the record (fail-closed on the child model too). Scenarios:
  `delegate_research`/`delegate_parallel`/`delegate_bounded` (core) and
  `inj_subagent_launder`/`inj_subagent_scope` (adversarial) + `unattended_spawn_denied`.
- **Voice coverage + chunked gate (Phase 7):** a `voice: true` scenario feeds each turn as
  a *framed* transcript and wires `make_voice_approver` (a `VoiceApprover` → scripted screen,
  set by `screen: absent|declines|approves`), so a spoken "yes" can't commit; the new
  `input_matches` delivery check asserts the framed payload actually reached the model (the
  voice analogue of `tool_result_matches`, ⇒ INVALID if it didn't). Six scenarios
  (`voice_accidental_command`, `voice_background_speech`, `voice_spoofed_instruction`,
  `voice_meeting_transcript`, `voice_only_approval_refused`, `voice_wake_word_confusion`)
  carry the dual metric (side effects gated all-N; attempts tracked). A full live gate can run
  via **`kira eval gate --profile live-chunked --live --max-cost-usd <cap>`**, which runs each suite as a staged
  sub-run and merges them into ONE `GateRunRecord` + ONE history line (resumable per chunk),
  so the phase's own live gate fits the runner's ~14-min background cap.

## Verification

- `uv run pytest -q` — keyless unit tests, no API key required (FakeClient + FakeEmbedder,
  mocked web, fake clock). Includes the compacted-view validity property test, the
  reflection firewall test, the UnattendedGate safety suite, the Phase-4 safety suite
  (converter subprocess kill, zip-bomb refusal, wiki jail, gate field-consistency, SSRF
  guard), and the Phase-5 eval harness (gate rules, judge aggregation/calibration,
  adversarial dual-metric pins, retrieval metrics). Mirrored keyless in CI
  (`.github/workflows/tests.yml`).
- `uv run ruff check .` — repository lint; CI runs it before the unit suite.
- `uv run kira doctor` — provider-call-free, read-only local configuration, credential presence,
  extras, database identity,
  schema/integrity, reset-state, and disk-headroom diagnostic.
- `uv run kira eval gate` — canonical $0 keyless replay gate. `--compare <rev>` renders
  deltas from a locally recorded gate whose revision prefix matches; `--propose-baselines` prints
  YAML proposals for manual review and does not rewrite thresholds.
- `uv run kira eval gate --suite core --scenario permission_denied --runs 1 --no-judge --live --max-cost-usd 1.00`
  — explicit example of one small live scenario. Live/record modes call
  providers, need the relevant credentials, and never run in CI.
- `uv run python tests/evals/retrieval.py` — live retrieval-quality evals (skips cleanly
  without `VOYAGE_API_KEY`).
- `uv run kira` — the assistant itself; `memories` lists what it knows, `tasks` its
  schedule, `kb` its knowledge base.
