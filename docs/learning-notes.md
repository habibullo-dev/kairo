# Learning Notes

Per-task design notes for an advanced engineer new to agent architectures.
Each entry captures the *non-obvious* decisions and their rationale.

## Task 1 — Scaffold

- **src layout (`src/jarvis/`), not flat.** With a flat layout, `import jarvis`
  can accidentally resolve to the source tree in the CWD instead of the installed
  package, so tests can pass against uninstalled/stale code. The src layout forces
  an install step (`uv sync` installs editable), guaranteeing tests exercise the
  package as it will actually ship.
- **Pinned Python 3.13 while the system runs 3.14.** uv fetches a managed 3.13
  interpreter so the project doesn't depend on whatever the machine has. 3.13 has
  the broadest binary-wheel coverage; several C-extension deps we'll add later
  (lxml via trafilatura, numpy) may lack 3.14 wheels and would otherwise compile
  from source. `requires-python = ">=3.12"` keeps the floor at the plan's target.
- **Dependencies added per-task, not all upfront.** Runtime deps enter via
  `uv add` when their task needs them, so each commit ties a dependency to the
  reason it exists. Only dev tooling (ruff, pytest, pytest-asyncio) is declared now.
- **`asyncio_mode = "auto"` in pytest.** The agent core is async; auto mode lets
  `async def test_*` run without decorating every test, keeping the loop's tests
  readable.
- **Console script points at `jarvis.__main__:main`.** One entry function backs
  both `python -m jarvis` and the `jarvis` command, so there's a single place the
  REPL gets wired in at task 8.

## Task 2 — Config

- **Secrets and settings are split by trust level, not just tidiness.** API keys
  load from `.env` via `pydantic-settings` (never committed); model IDs/limits/paths
  load from `config/settings.yaml` (safe to commit). Mixing them would either leak
  secrets into git or force the whole team to share one `.env` for non-secret tuning.
- **Keys are optional at load, required on demand.** `Secrets` fields default to
  `""` so config builds with no keys (every offline task + all unit tests). A key's
  presence is enforced only when a code path actually needs it, via
  `config.require("anthropic")`, which raises a `ConfigError` naming the missing env
  var and pointing at `.env.example`. This turns a future opaque 401 into an
  actionable startup error, without coupling offline code to key availability.
- **Loading is pure; directory creation is explicit.** `load_config` never touches
  the filesystem beyond reading; `ensure_dirs()` (the side effect) is called by the
  app at startup. That keeps tests from littering `data/`/`logs/` into the repo and
  makes `load_config` trivially safe to call anywhere.
- **`root` is injectable.** `load_config(root=...)` + `env_file=None` lets tests
  point at a temp dir and read secrets from monkeypatched env only — no dependence
  on the real `.env`. An autouse fixture clears ambient keys so a developer's
  exported `ANTHROPIC_API_KEY` can't make the "missing key" test spuriously pass.
- **Every setting has a code default; YAML only overrides.** A missing or partial
  `settings.yaml` still yields a working `Config`. `pydantic-settings` precedence
  (init args > env vars > `.env` > defaults) is relied on and pinned by a test.

## Task 3 — Observability

- **The JSONL log is the audit trail, not the UI.** structlog renders one JSON
  object per line to `logs/jarvis-YYYY-MM-DD.jsonl`. User-facing output is the
  REPL's job (rich, task 8); conflating the two would make the machine-parseable
  record depend on terminal formatting. So logging writes structured events only.
- **`trace_id` is a contextvar + a processor, not a parameter threaded everywhere.**
  Bind it once at the top of a turn; a structlog processor stamps every subsequent
  event automatically. This is what lets you later `grep trace_id=...` a log and see
  the whole turn — model calls, tool calls, permission decisions — without passing
  an id through every function signature.
- **Cost is observability, never a control input.** The project is quality-first;
  the pricing table exists so the status bar/audit can report spend. `cost_of`
  returns `0.0` for unknown models rather than raising — a bad price estimate must
  never crash the agent.
- **Price lookup tolerates dated snapshot IDs.** The API may return
  `claude-haiku-4-5-20251001`; `price_for` matches the longest known prefix so the
  table only needs the alias. Cache tokens are priced as multiples of the input
  rate (write ~1.25x for the 5-min cache, read ~0.1x), matching the four-field
  Anthropic `usage` object — so `Usage.from_response` reads it directly in task 7.
- **`configure_logging` is idempotent and closes the prior file.** Tests point it
  at a temp dir per test and reconfigure freely; without closing the old handle
  we'd leak file descriptors and trip `ResourceWarning`. `cache_logger_on_first_use`
  is off so reconfiguration actually rebinds the output.
