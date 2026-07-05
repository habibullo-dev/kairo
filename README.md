# Jarvis

A from-scratch, Jarvis-style agentic assistant built directly on the Anthropic
Messages API — no agent framework. The goal is twofold: learn agent engineering
deeply, and end up with a genuinely useful assistant that can use tools, remember
things, manage tasks, read files, research the web, and eventually speak, listen,
and coordinate multiple agents.

The full architecture, phase roadmap, and design rationale live in
[`docs/PLAN.md`](docs/PLAN.md).

## Status

Phase 1 (MVP) — in progress. See [`docs/PLAN.md`](docs/PLAN.md) section 8 for the
task list and [`docs/learning-notes.md`](docs/learning-notes.md) for design notes.

## Requirements

- [uv](https://docs.astral.sh/uv/) (package + Python manager)
- Python 3.12+ (the project pins 3.13 via `.python-version`)
- PowerShell 7 (the shell tool runs `pwsh`)

## Setup

```pwsh
uv sync                 # create the venv and install dependencies
cp .env.example .env    # then fill in your API keys
```

## Usage

```pwsh
uv run jarvis           # start the assistant (REPL lands in task 8)
uv run pytest           # run the test suite
uv run ruff check       # lint
```

## License

MIT
