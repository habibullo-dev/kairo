# Architecture (as built — Phase 5)

This describes what exists after Milestone 4 (research + knowledge base) on top of
the Milestone 1 MVP, Milestone 2 (long-term memory), and Milestone 3 (tasks &
scheduling). For the forward-looking plans see [`PLAN.md`](PLAN.md),
[`PLAN-2-memory.md`](PLAN-2-memory.md), [`PLAN-3-tasks.md`](PLAN-3-tasks.md), and
[`PLAN-4-knowledge.md`](PLAN-4-knowledge.md); for the reasoning behind each decision
see [`learning-notes.md`](learning-notes.md).

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
│ knowledge/       store · chunking · converters · links ·  │
│                  service (+ convert_worker subprocess)     │
├─ foundation ─────────────────────────────────────────────┤
│ persistence/     SQLite sessions/messages/memories/tasks/ │
│                  kb + migrations + shared write lock       │
│ observability/   structlog audit log + cost accounting    │
│ config.py        settings (yaml) + secrets (.env)         │
│ paths.py         path resolution + secret floor  ·  net.py SSRF guard │
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

## Persistence (`persistence/`)

SQLite via aiosqlite. `sessions` + `messages` + `memories` + `tasks`/`task_runs` +
`kb_sources`/`kb_chunks`/`kb_wiki_links` tables; schema version tracked by `PRAGMA
user_version` with an ordered migration list (v2 memory; v3 tasks + `sessions.kind`;
v4 knowledge base). The model is stateless — the whole conversation lives here and is
reconstructed each call. Message content is stored as JSON verbatim (thinking-block
signatures survive, so a resumed session replays to the API unchanged). Saved per
turn; `--resume` loads the most recent **interactive** session (a background job's
`kind='task'` session never wins, and is excluded from reflection by default — one
column that stops a job transcript hijacking the session or poisoning memory).
Primary records (memories, tasks, `kb_sources`) are never deleted — status flips keep
lineage auditable; `kb_chunks`/`kb_wiki_links` are the one exception, rebuildable
caches that delete-and-replace. All stores share **one connection and one write
lock**: a second connection would deadlock, and multi-statement writes go through a
`transaction()` helper (`BEGIN IMMEDIATE` under the lock) so concurrent writes can't
tear a session's history.

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

## Evaluation (`tests/evals/`) — Phase 5

The instrument that says whether the agent works and whether a change regressed it.
Repo-native, no framework, no new deps; the *harness* is unit-tested keyless while the
*gate* is a deliberate, human-run, recorded live ritual.

- **`runner.py`** — runs each scenario N times in an isolated workdir, produces a
  `ScenarioRunRecord` (tokens, latency, tool calls, **attempts**, judge verdict), and
  owns the check evaluator. Checks are input-level (`tool_not_attempted_with`,
  `tool_result_matches`, `memory_absent`, …), not just name-level, so an injection the
  gate *denied* is still visible via the `ToolDecision` event (emitted for every call
  before execution, unlike `ToolStarted`).
- **`judge.py`** — LLM-as-judge: rationale-first forced verdict (thinking-off), median
  of 3 Opus votes + majority pass, one uncounted Sonnet cross-check, specimen
  delimiters, and calibration fixtures that flip a run to JUDGE-INVALID if the judge
  can be gamed. Honest that 3 votes buy variance reduction, not independence.
- **`report.py`** — the two-tier gate engine (safety all-N; quality 3/3-PASS /
  2/3-FLAKY-pass / ≤1/3-FAIL with two-consecutive promotion), token ceilings, judge
  floors (shadow until ratcheted), `--compare <rev>` deltas with dirty/hash/judge-model
  guards, and a report that states its own statistical power.
- **`retrieval.py`** — drives `MemoryStore.search` / `KnowledgeStore.search` directly
  for MRR / recall@k, a determinism self-check (⇒ N=1), and a min_similarity floor
  sweep read as data with an explicit decision rule.
- **`recorder.py`** — versioned JSONL records + git-rev/dirty provenance + fail-closed
  pricing; **`baselines.yaml`** is the one committed contract (results/history are
  gitignored under `data/evals/`).
- Adversarial suite (`scenarios/adversarial/`) + under-querying probes; dual metric and
  the KB-auto-injection verdict are recorded in
  [ADR-0005](decisions/0005-how-we-know-it-works.md).

## Verification

- `uv run pytest` — 580+ unit tests, no API key required (FakeClient + FakeEmbedder,
  mocked web, fake clock). Includes the compacted-view validity property test, the
  reflection firewall test, the UnattendedGate safety suite, the Phase-4 safety suite
  (converter subprocess kill, zip-bomb refusal, wiki jail, gate field-consistency, SSRF
  guard), and the Phase-5 eval harness (gate rules, judge aggregation/calibration,
  adversarial dual-metric pins, retrieval metrics). Mirrored keyless in CI
  (`.github/workflows/tests.yml`).
- `uv run python tests/evals/runner.py --suite all --report` — the live gate (all
  scenarios × N=3 + judge); `--compare <rev>` renders cross-revision deltas;
  `--propose-baselines` regenerates thresholds. Costs money and needs the three keys —
  never in CI.
- `uv run python tests/evals/retrieval.py` — live retrieval-quality evals (skips cleanly
  without `VOYAGE_API_KEY`).
- `uv run jarvis` — the assistant itself; `memories` lists what it knows, `tasks` its
  schedule, `kb` its knowledge base.
