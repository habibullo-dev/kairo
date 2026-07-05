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

**Phase 1 (MVP) — complete.** A streaming terminal assistant that plans, calls
tools (asking approval for risky ones), remembers a conversation across restarts,
and reports what it did with sources. Verified end-to-end by a live smoke-eval
suite (3/3 scenarios). Later phases (long-term memory, tasks/scheduling, deeper web
research, evaluation harness, multi-agent, voice, web UI) are laid out in
[`docs/PLAN.md`](docs/PLAN.md) §2.

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
VOYAGE_API_KEY=...        # phase 2 (memory)
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
current turn without quitting; `exit` or `Ctrl+D` quits.

## Safety model

Every tool call passes through a **permission gate** before it runs, and every
model call, tool call, and permission decision is written to an append-only JSON
audit log at `logs/jarvis-YYYY-MM-DD.jsonl`, correlated by a per-turn `trace_id`.

- **Decisions are `allow` / `ask` / `deny`**, configured in
  [`config/permissions.yaml`](config/permissions.yaml). Safe defaults: reads are
  allowed; writes, shell, and anything external ask first.
- **Filesystem writes** are checked against an allowlist — a write outside it is
  escalated to `ask`, never allowed silently.
- **Shell commands** are refined by longest-prefix rules (e.g. `git status` allow,
  `rm ` ask). A tool-level `deny` is absolute.
- **"Always allow"** at the prompt persists the narrowest rule that covers it (a
  shell prefix, a write directory, or a whole tool), so one approval never grants
  more than you approved.
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
  cli/          REPL + rich rendering
  core/         agent loop, model clients (fake + Anthropic), prompts, events
  tools/        Tool base, registry, executor, builtin/ (filesystem, shell, web)
  permissions/  policy + gate
  persistence/  SQLite sessions/messages + migrations
  observability/ structured logging + cost accounting
  config.py     settings + secrets
tests/          unit tests + evals/ (live smoke scenarios)
docs/           PLAN, architecture, learning notes, decisions/ (ADRs)
```

## License

MIT
