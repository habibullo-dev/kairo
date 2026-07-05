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

## Task 4 — Tool framework

- **Tools are data to the model, code behind the executor.** The model receives
  only `{name, description, input_schema}`; the executor owns the side effect.
  This is the single most important safety boundary in an agent — the model can
  *request* anything but can only *do* what a tool's `run` implements, gated by
  the executor and (task 5) the permission gate.
- **`Params` is one pydantic model doing three jobs:** it generates the JSON
  schema the API advertises, validates the model's tool input before `run` sees
  it, and types `run`'s argument. One source of truth means the schema and the
  runtime contract can't drift apart.
- **`__init_subclass__` fails a misdeclared tool at import, not at call time.**
  A concrete tool missing `name`/`description`/`Params` raises `TypeError` the
  moment the class is defined — you find out on `discover()`, not three tool
  calls into a live session. Abstract intermediate bases are exempted via the
  `__abstractmethods__` check.
- **The executor turns every failure into a `ToolResult(is_error=True)`.** Bad
  input, timeout, and raised exceptions all become results the model reads and
  recovers from — never exceptions that unwind the loop. This is *the* rule that
  separates a resilient agent from one that dies on the first tool hiccup.
- **Truncation guards the context window, not the disk.** A tool returning a
  100k-char blob is capped with an explicit "[... truncated N chars ...]" note.
  Without it, one `read_file` on a huge file silently evicts the rest of the
  conversation. Char-based (not token-based) truncation keeps it offline/fast;
  precise token limits aren't needed for a safety cap.
- **Audit logging is deliberately NOT in the executor.** The agent loop owns the
  `trace_id` and orchestration, so it emits `tool_call`/`tool_result` events. The
  executor stays a pure unit — trivial to test without configuring logging.
- **Discovery filters by `__module__`.** `register_from_module` only registers
  tool classes *authored* in that module, so importing the base `Tool` (or
  re-exporting a tool) never causes a double-registration collision.

## Task 5 — Permissions

- **Policy is data; the gate is behavior.** `Policy` (a pydantic model) round-trips
  to `permissions.yaml`; `PermissionGate` interprets it. Keeping them apart makes
  the gate a pure function that's trivially table-testable, and lets the policy be
  edited/persisted without touching decision logic.
- **Base-decision precedence: per-tool entry > tool's own default > policy default.**
  This lets the shipped `permissions.yaml` be sparse (only the interesting
  overrides) while each tool still carries a sensible built-in default and the
  global default catches everything else.
- **The allowlist can only tighten, never loosen.** A write outside the allowlist
  with an `allow` base is escalated to `ask` — never the reverse. So mis-setting
  `write_file: allow` can't silently grant writes to arbitrary absolute paths; the
  worst case is an extra prompt. Paths are resolved (relative → project root) before
  the containment check, which also neutralizes `..` traversal.
- **A tool-level `deny` is absolute.** It short-circuits before shell/path
  refinement, so an allow-listed `git status` rule can't re-enable a `run_shell`
  that policy turned off entirely. Shell rules use longest-prefix-wins so a broad
  `git` rule and a specific `git status` rule can coexist.
- **`Decision` carries a reason, not just a verdict.** The reason feeds both the
  approval prompt ("shell rule 'rm ' -> deny") and the audit log, so a human (or a
  later debugging session) can see *why* a call was gated the way it was.
- **Persistence is granular for shell, coarse for tools.** "Always allow" a shell
  command persists a *prefix rule* (`persist_shell_rule`), not a blanket
  `run_shell: allow` — allowing one command shouldn't open the whole shell.
  Tradeoff noted: `save_policy` uses `yaml.safe_dump`, so hand-written comments in
  `permissions.yaml` are lost when a rule is auto-persisted.
