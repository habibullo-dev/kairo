# Architecture (as built — Phase 1)

This describes what exists after Milestone 1. For the forward-looking plan and
phase roadmap, see [`PLAN.md`](PLAN.md). For the reasoning behind each decision,
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
├─ core ───────────────────────────────────────────────────┤
│ core/agent.py    AgentLoop.run_turn — the while-loop      │
│ core/client.py   LLMClient interface + FakeClient         │
│ core/anthropic_client.py  live streaming client           │
│ core/prompts.py  system-prompt assembly                   │
│ core/events.py   typed events the loop emits              │
├─ services ───────────────────────────────────────────────┤
│ tools/           Tool base, registry, executor, builtin/  │
│ permissions/     policy + PermissionGate (allow/ask/deny) │
├─ foundation ─────────────────────────────────────────────┤
│ persistence/     SQLite sessions + messages + migrations  │
│ observability/   structlog audit log + cost accounting    │
│ config.py        settings (yaml) + secrets (.env)         │
└──────────────────────────────────────────────────────────┘
```

Interfaces depend on core; core depends on services; everything may use the
foundation. Nothing lower reaches up. This is why the REPL is thin (~an event
consumer + an approval prompt) and a future web UI is cheap: swap the interface,
keep the loop.

## The agent loop (`core/agent.py`)

One user turn, `AgentLoop.run_turn(messages)`:

1. Bind a `trace_id`; log `turn_start`.
2. Loop, bounded by `max_iterations`:
   - Call the model (streaming) with the system prompt, messages, and tool schemas.
   - Log `model_call` with token usage + computed cost.
   - Append the assistant's content blocks **verbatim** (thinking/tool_use round-trip).
   - If `stop_reason != tool_use` → emit `TurnCompleted`, return.
   - Otherwise, handle tools: resolve permission for each **sequentially** (orderly
     prompts), then execute the approved ones **in parallel**; append one
     `tool_result` block per `tool_use` id.
3. If the loop hits `max_iterations`, stop with that reason.

Invariants (each a classic agent bug when violated): tool errors/denials/unknown
tools become `is_error` results the model recovers from; exactly one result per
call; assistant blocks appended unchanged; results truncated to protect context;
the iteration guard prevents runaways.

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
(httpx + trafilatura).

## Permissions (`permissions/`)

`Policy` (from `config/permissions.yaml`) is data; `PermissionGate` interprets it.
Base decision precedence: per-tool entry → the tool's own default → policy default.
Refinements: filesystem writes are checked against an allowlist (can only tighten);
shell commands match longest-prefix rules; a tool-level `deny` is absolute. The gate
only decides — the interface prompts the human and the loop runs the tool. "Always
allow" persists the narrowest rule.

## Persistence (`persistence/`)

SQLite via aiosqlite. `sessions` + `messages` tables; schema version tracked by
`PRAGMA user_version` with an ordered migration list. The model is stateless — the
whole conversation lives here and is reconstructed each call. Message content is
stored as JSON verbatim (thinking-block signatures survive, so a resumed session
replays to the API unchanged). Saved per turn; `--resume` loads the most recent.

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

- `uv run pytest` — 120+ unit tests, no API key required (FakeClient, mocked web).
- `uv run python tests/evals/runner.py` — live smoke evals (3 scenarios × 3 runs).
- `uv run jarvis` — the assistant itself.
