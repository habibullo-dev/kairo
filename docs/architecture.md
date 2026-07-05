# Architecture (as built — Phase 3)

This describes what exists after Milestone 3 (tasks & scheduling) on top of the
Milestone 1 MVP and Milestone 2 (long-term memory). For the forward-looking plans
see [`PLAN.md`](PLAN.md), [`PLAN-2-memory.md`](PLAN-2-memory.md), and
[`PLAN-3-tasks.md`](PLAN-3-tasks.md); for the reasoning behind each decision see
[`learning-notes.md`](learning-notes.md).

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
│ tools/           Tool base, registry, executor, builtin/  │
│ permissions/     policy + PermissionGate + UnattendedGate │
│ memory/          store · embeddings · service · reflection│
│ scheduler/       store · triggers · service · runner      │
├─ foundation ─────────────────────────────────────────────┤
│ persistence/     SQLite sessions/messages/memories/tasks  │
│                  + migrations + shared write lock         │
│ observability/   structlog audit log + cost accounting    │
│ config.py        settings (yaml) + secrets (.env)         │
│ paths.py         unified path resolution + secret floor   │
└──────────────────────────────────────────────────────────┘
```

Interfaces depend on core; core depends on services; everything may use the
foundation. Nothing lower reaches up. This is why the REPL is thin (~an event
consumer + an approval prompt) and a future web UI is cheap: swap the interface,
keep the loop. Phase 3 keeps the direction honest: the `scheduler/` runner fires
due tasks but takes job *execution* as an injected callback, so it never imports
core; the callback that builds the unattended `AgentLoop` (`cli/jobs.py`) lives in
the interface layer, where core + services are already composed.

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
(scripted) backs the whole unit-test suite; `AnthropicClient` is the live
streaming implementation (adaptive thinking + `output_config.effort`, SDK retries,
content-block serialization that preserves thinking signatures). Going live changed
zero loop code.

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

## Permissions (`permissions/`)

`Policy` (from `config/permissions.yaml`) is data; `PermissionGate` interprets it.
Base decision precedence: per-tool entry → the tool's own default → policy default.
Refinements: a **sensitive-path floor** (`jarvis/paths.py`) denies reads and writes
of secrets/keys regardless of policy; filesystem writes are checked against an
allowlist (can only tighten); shell commands match longest-prefix rules at a token
boundary, with an `allow` downgraded to `ask` when shell metacharacters could chain
or redirect; a tool-level `deny` is absolute. The gate and the filesystem tools
resolve paths through the *same* `resolve_path` (against the workspace root), so a
decision and the action it authorizes always name the same file. The gate only
decides — the interface prompts the human and the loop runs the tool. "Always allow"
persists the narrowest rule, and refuses to persist an over-broad write directory
(and refuses entirely for `schedule_task`/`cancel_task` — never silence-able).

For unattended background runs, `UnattendedGate` wraps the gate: every `ask` becomes
a `deny` (a `HeadlessApprover` that never touches stdin), interactive `allow`s for
`run_shell`/`write_file` are demoted to `deny` unless explicitly opted in, and the
state-mutating meta tools (`schedule_task`/`cancel_task`/`remember`/`forget`) are
hard-denied regardless of policy. The key insight ([ADR-0003](decisions/0003-unattended-runs-deny-and-demote.md)):
the real escalation channel is a policy `allow` resolved *before* any approver runs,
not an `ask` — so deny-the-ask alone is insufficient; the demotion is what closes it.

## Memory (`memory/`) — Phase 2

Three tiers around the loop. **Working memory** is the message list, compacted by
`core/context.py`: it produces a per-request *view* (token-weighted cut at a real
user turn; oldest tool-result bodies elided when a single turn overflows) while the
full history stays the source of truth, and the dropped prefix is represented by a
`claude-sonnet-5` summary carried in the system prompt (frozen per turn, persisted
so `--resume` doesn't re-summarize). **Long-term memory** is an embeddings-indexed
`memories` store: `MemoryStore` (unit-normalized float32 vectors, cosine = one numpy
matmul) under a `MemoryService` that owns remember (with sonnet-5 dedup
adjudication), recall, and auto-recall. The `Embedder` seam (Voyage live, a
deterministic fake in tests) mirrors the `LLMClient` pattern. **Episodic memory** is
the transcript; on exit `memory/reflection.py` distills durable facts via a forced
tool call — after **stripping tool-result bodies** so untrusted fetched content
can't be laundered into permanent memory (see [ADR-0002](decisions/0002-reflection-writes-bypass-the-gate.md)).
Memory is optional: no `VOYAGE_API_KEY` ⇒ the tools aren't registered and the loop
runs exactly as in Phase 1.

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
them. `BackgroundRunner` is a ~40-line asyncio wake loop that fires due tasks under
the shared **turn lock** (so background and interactive turns never overlap);
reminders notify-then-record (at-least-once), jobs open their run row first (crash-
detectable). A **job** runs as one unattended `AgentLoop` turn in a fresh
`kind='task'` session behind the [ADR-0003](decisions/0003-unattended-runs-deny-and-demote.md)
gate. Optional: `scheduler.enabled: false` ⇒ no task tools, and the loop is
byte-identical to Phase 2.

## Persistence (`persistence/`)

SQLite via aiosqlite. `sessions` + `messages` + `memories` + `tasks`/`task_runs`
tables; schema version tracked by `PRAGMA user_version` with an ordered migration
list (v2 adds memory + compaction/reflection bookkeeping; v3 adds tasks + a
`sessions.kind` marker). The model is stateless — the whole conversation lives here
and is reconstructed each call. Message content is stored as JSON verbatim (thinking-
block signatures survive, so a resumed session replays to the API unchanged). Saved
per turn; `--resume` loads the most recent **interactive** session (a background
job's `kind='task'` session never wins, and is excluded from reflection by default —
one column that stops a job transcript hijacking the session or poisoning memory).
Memories/tasks are never deleted — status flips keep lineage auditable. All stores
share **one connection and one write lock**: a second connection would deadlock, and
multi-statement writes go through a `transaction()` helper (`BEGIN IMMEDIATE` under
the lock) so Phase 3's first real write concurrency can't tear a session's history.

## Observability (`observability/`)

structlog writes one JSON object per line to `logs/jarvis-YYYY-MM-DD.jsonl`. A
`trace_id` contextvar, stamped by a processor, ties every event in a turn together
(`turn_start`, `model_call`, `permission_decision`, `tool_call`, `tool_result`,
`turn_end`). Cost is computed from a per-model pricing table for observability only.

## Data flow of one turn

```
you → REPL → AgentLoop.run_turn(messages)
                │  ├─ LLMClient.create(...)  ──stream──▶ TextDelta events → render
                │  ├─ PermissionGate.check(...)  → ask? → approver (REPL prompt)
                │  ├─ ToolExecutor.execute(...)  → ToolStarted/ToolFinished events
                │  └─ append tool_results, loop
                ▼
        TurnResult → SessionStore.save_messages → status line
        (every step logged to logs/*.jsonl with the turn's trace_id)
```

## Verification

- `uv run pytest` — 240+ unit tests, no API key required (FakeClient + FakeEmbedder,
  mocked web). Includes the compacted-view validity property test and the reflection
  firewall test.
- `uv run python tests/evals/runner.py` — live smoke evals (file task, web research,
  permission denial, memory tool roundtrip, cross-session recall).
- `uv run jarvis` — the assistant itself; `memories` lists what it knows.
