# Jarvis

A from-scratch, Jarvis-style agentic assistant built directly on the Anthropic
Messages API — **no agent framework**. The agent loop, tool system, permission
model, memory, and observability are all hand-built, so every moving part is
visible and understood. The goal is twofold: learn agent engineering deeply, and
end up with a genuinely useful assistant that can use tools, remember things,
manage tasks, read files, research the web, and eventually speak, listen, and
coordinate multiple agents.

The full architecture and design rationale live in
[`docs/PLAN.md`](docs/PLAN.md) and [`docs/architecture.md`](docs/architecture.md);
per-task design notes are in [`docs/learning-notes.md`](docs/learning-notes.md).

## Status

**Phase 3 (tasks & scheduling) — complete.** Jarvis can now act without being
prompted: schedule **reminders** (delivered to you at a time) and **jobs** (a
stored prompt it runs itself, unattended, on a once / cron / interval schedule)
with a `schedule_task` / `list_tasks` / `cancel_task` toolset and a `tasks` REPL
command. A background wake-loop fires due tasks in-process; missed tasks are caught
up (within a grace window) on the next start. Unattended runs are deliberately
constrained — see the safety model below and [ADR-0003](docs/decisions/0003-unattended-runs-deny-and-demote.md).
Design and rationale are in [`docs/PLAN-3-tasks.md`](docs/PLAN-3-tasks.md).

**Phase 2 (long-term memory) — complete.** Durable memory across sessions
(embeddings recall + a `remember`/`recall`/`forget` toolset), automatic background
recall, conversation compaction, and end-of-session reflection. Design in
[`docs/PLAN-2-memory.md`](docs/PLAN-2-memory.md).

**Phase 1 (MVP) — complete.** A streaming terminal assistant that plans, calls
tools (asking approval for risky ones), remembers a conversation across restarts,
and reports what it did with sources. Verified end-to-end by a live smoke-eval
suite. Later phases (research + a Markdown knowledge base, evaluation harness,
multi-agent, voice, web UI) are laid out in [`docs/PLAN.md`](docs/PLAN.md) §2.
Odysseus is tracked there as an approved external product/reference source for
the eventual local AI workstation experience.

## Requirements

- [uv](https://docs.astral.sh/uv/) — package + Python manager
- Python 3.12+ (the project pins 3.13 via `.python-version`; uv fetches it)
- PowerShell 7 (`pwsh`) — the shell tool runs commands through it
- API keys: **Anthropic** (required), **Tavily** (web search), **Voyage** (phase 2)

## Setup

```pwsh
uv sync                 # create the venv, fetch Python 3.13, install deps
cp .env.example .env     # then fill in your API keys
```

`.env`:

```
ANTHROPIC_API_KEY=...    # required
TAVILY_API_KEY=...       # for web_search
VOYAGE_API_KEY=...        # for long-term memory (embeddings)
```

## Usage

```pwsh
uv run jarvis            # start the assistant (needs a real terminal)
uv run jarvis --resume   # continue the most recent conversation
uv run jarvis --version

uv run pytest            # unit tests (no API key needed)
uv run ruff check        # lint
uv run python tests/evals/runner.py          # live smoke evals (uses the API — costs money)
uv run python tests/evals/runner.py --runs 1 # quick single pass
```

In the REPL: type a request; watch Jarvis stream its reasoning and tool calls.
Risky tools prompt for approval (`y` / `N` / `a`lways). `Ctrl+C` cancels the
current turn without quitting; `exit` or `Ctrl+D` quits. Type `memories` to list
what Jarvis has remembered (with provenance — where each memory came from), and
`tasks` (or `tasks all` / `tasks <id>`) to see scheduled tasks and their run
history.

**Tasks & scheduling:** ask Jarvis to "remind me to stretch in 20 minutes" or
"every weekday at 9am, summarize my notes.txt" — it schedules a reminder or an
unattended job (you approve the schedule, and the prompt shows the full payload
plus the computed local fire time). Reminders are delivered as a line at the
prompt; jobs run themselves in the background and report a result. Set
`scheduler.enabled: false` in `settings.yaml` to turn it off.

**Long-term memory** (needs `VOYAGE_API_KEY`): tell Jarvis something worth keeping
and it asks before saving it; on exit it reflects over the session and stores
durable facts. Next session, relevant memories are recalled automatically. Try:
tell it your favorite editor, `exit`, restart, and ask what your favorite editor
is — it answers from memory. Set `memory.enabled: false` in `settings.yaml` (or
omit the key) to turn it off.

## Safety model

Every tool call passes through a **permission gate** before it runs, and every
model call, tool call, and permission decision is written to an append-only JSON
audit log at `logs/jarvis-YYYY-MM-DD.jsonl`, correlated by a per-turn `trace_id`.

- **Decisions are `allow` / `ask` / `deny`**, configured in
  [`config/permissions.yaml`](config/permissions.yaml). Safe defaults: reads are
  allowed; writes, shell, and network (`web_search` / `web_fetch`) ask first.
- **Secrets are off-limits by a code floor.** Reads *and* writes of credential
  paths (`.env`, SSH/GPG keys, `.aws/credentials`, `.npmrc`, `*.pem`, …) are
  denied outright — a floor in `jarvis/paths.py` that policy can extend but not
  disable. Committed templates like `.env.example` are the one exception.
- **Filesystem writes** are checked against an allowlist — a write outside it is
  escalated to `ask`, never allowed silently. The gate and the tools resolve every
  path the *same* way (against the workspace root, collapsing `..` and symlinks),
  so an approval and the action it authorizes always refer to the same file.
- **Shell commands** are refined by longest-prefix rules (e.g. `git status` allow,
  `rm ` ask), matched at a token boundary. An `allow` is downgraded to `ask` if the
  command contains shell metacharacters (`; | & > <` …), so an allowlisted prefix
  can't smuggle a chained command past the gate. A tool-level `deny` is absolute.
- **"Always allow"** at the prompt persists the narrowest rule that covers it (a
  shell prefix, a resolved write directory, or a whole tool), and refuses to
  persist an over-broad grant (a drive root or your home directory).
- **File reads are bounded** to a byte ceiling read straight from disk, so a huge
  file can't exhaust memory or evict the conversation.
- **Memory writes are guarded against poisoning.** The `remember` tool asks first
  (a memory persists into every future prompt, so a fetched web page can't silently
  plant one), showing the full content at the approval prompt. End-of-session
  reflection — which forms most memories — strips tool-result bodies before the
  extractor sees them and only keeps facts the *user* stated, so untrusted fetched
  content can't be laundered into permanent memory ([ADR-0002](docs/decisions/0002-reflection-writes-bypass-the-gate.md)).
- **Scheduling a task asks, and can never be "always"-allowed.** `schedule_task` is
  a deferred-execution sink (the payload later runs with tools), so the approval
  prompt shows the full untruncated payload and the computed local fire time, and
  the "always" shortcut is refused for it.
- **Unattended jobs run under a stricter gate** ([ADR-0003](docs/decisions/0003-unattended-runs-deny-and-demote.md)):
  with no human to prompt, every `ask` becomes a `deny`; and — because it's the real
  escalation channel — an interactive "always allow" for `run_shell` / `write_file`
  does *not* extend to background runs (opt in explicitly via
  `scheduler.unattended_allow_tools`). Memory/scheduling meta-tools are hard-denied,
  so a job can't schedule more jobs or write memory on its own. Background sessions
  are marked `kind='task'`, so they never hijack `--resume` or feed reflection.
- **Tool failures, denials, and unknown tools become results the model reads** and
  recovers from — they never crash the session.

## Configuration

Non-secret settings (model IDs, limits, paths) live in
[`config/settings.yaml`](config/settings.yaml); each has a code default so the app
runs without the file. Quality-first: the most capable model at every tier
(`claude-opus-4-8` for reasoning, `claude-sonnet-5` for background work), adaptive
thinking, and high effort — API cost is treated as observability, not a constraint.

## Project layout

```
src/jarvis/
  cli/          REPL + rich rendering + background job execution (jobs.py)
  core/         agent loop, context/compaction, model clients, prompts, events
  tools/        Tool base, registry, executor, builtin/ (filesystem, shell, web, memory, tasks)
  permissions/  policy + gate + unattended gate (headless deny/demote)
  memory/       long-term memory: store, embeddings, service, reflection
  scheduler/    tasks & scheduling: store, triggers, service, background runner
  persistence/  SQLite sessions/messages/memories/tasks + migrations
  observability/ structured logging + cost accounting
  config.py     settings + secrets   ·   paths.py  path resolution + secret floor
tests/          unit tests + evals/ (live smoke scenarios)
docs/           PLAN, PLAN-2-memory, PLAN-3-tasks, architecture, learning notes, decisions/ (ADRs)
```

## License

MIT
