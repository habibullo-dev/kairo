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

## Task 6 — Agent loop (mocked)

- **The loop talks to an `LLMClient` interface, never the SDK.** That single seam
  is what lets the entire loop be tested end-to-end against a scripted `FakeClient`
  with zero network, and swapped for the real streaming client in task 7 without
  touching loop code. `FakeClient` also records every `create` call so tests can
  assert exactly what the loop sent back to the model.
- **Assistant content blocks are appended verbatim.** The response's
  `content_blocks` go onto the history unchanged — the API requires `tool_use`
  blocks to round-trip exactly. A test pins this (`== call_block.content_blocks`)
  so a future "helpful" transform can't silently break tool continuation.
- **Every failure path becomes a `tool_result`, never an exception.** Tool errors
  (executor), denials, and unknown-tool requests all produce an `is_error` result
  block the model reads. Tests assert the loop *continues* to a final answer after
  each — that resilience is the whole point of the loop.
- **Exactly one `tool_result` per `tool_use` id, in order.** `asyncio.gather` over
  the calls preserves both count and order; a parallel-tools test checks the two
  results come back as `[a, b]` matching the requests. Mismatches here are the
  single most common cause of API 400s in hand-rolled agents.
- **Permissions resolve sequentially; approved tools run in parallel.** Splitting
  `_handle_tools` into a sequential permission phase and a parallel execution phase
  avoids firing several human approval prompts at once, while still parallelizing
  the actual work. With no approver wired, an `ASK` safely defaults to `DENY`.
- **The max-iteration guard is not optional.** `range(max_iterations)` bounds the
  loop; a test scripts all-tool-use responses and asserts it stops at N with
  `stop_reason="max_iterations"`. A runaway tool loop is a matter of when, not if.
- **The loop owns audit logging.** It binds the `trace_id` and emits
  `turn_start`/`model_call`/`permission_decision`/`tool_call`/`tool_result`/
  `turn_end`; the executor and gate stay pure. A test uses
  `structlog.testing.capture_logs` to assert the full event set appears.
- **Events decouple the loop from any UI.** The loop emits typed events
  (`TextDelta`, `ToolStarted`, `ToolFinished`, `TurnCompleted`) to an optional
  sink; the REPL/web UI/tests each render them however they like. The loop imports
  no rendering library — this is what makes task 8's REPL and a future web UI cheap.

## Task 7 — Live Anthropic client

- **Going live changed one file, not the loop.** `AnthropicClient` implements the
  exact `LLMClient` interface the loop was tested against, so the switch from
  `FakeClient` is a constructor swap. That's the payoff of the task-6 seam.
- **Streaming is mandatory here, not optional.** With `max_tokens` at 32k the SDK
  refuses a non-streaming call (HTTP-timeout guard), and streaming is also what
  feeds the REPL live text. We iterate `stream.text_stream` for deltas and take
  `await stream.get_final_message()` for the complete, all-blocks message.
- **Opus 4.8 request shape is specific.** Adaptive thinking only
  (`thinking={"type":"adaptive"}`; `budget_tokens` is a 400), depth via
  `output_config={"effort": ...}` (default `high`, quality-first). No
  `temperature`/`top_p`. The live smoke test confirmed the API accepts this shape.
- **Content blocks are serialized from documented attributes, not a blind dump.**
  `_serialize_block` reads `block.text` / `.thinking`+`.signature` / `.id`+`.name`+
  `.input`, emitting only the fields the API accepts on resend — avoiding null/extra
  fields a `model_dump()` might include. Unknown block types fall back to
  `model_dump` so a new type degrades instead of crashing.
- **Thinking blocks must round-trip unchanged.** When adaptive thinking emits a
  thinking block, its `signature` is preserved so the next turn (same model)
  validates. The default `display` is "omitted", so the block often has empty text
  but still must be sent back intact — a unit test pins the signature preservation.
- **Retries are the SDK's job.** We raise `max_retries` (4) and let the SDK do
  exponential backoff on 429/5xx; a hand-rolled retry loop would only duplicate it.
- **Injectable client = testable streaming.** `AnthropicClient(client=...)` accepts
  a fake async-stream object, so the whole streaming + conversion path is unit-tested
  with no network; `from_config` is the real-key path and fails fast without a key.

## Task 9 — Built-in tools

- **Dependency injection via `ToolContext`, not globals.** Web search needs the
  Tavily key; rather than have the tool read `os.environ` (which the `.env` doesn't
  populate) or a module global, the registry injects a `ToolContext(config=...)` at
  discovery. Tools that need nothing ignore it. Adding `Tool.__init__(context=None)`
  was backward-compatible — every existing `EchoTool()` still constructs.
- **Blocking I/O runs in a thread, always.** Filesystem ops (`exists`, `read_bytes`,
  `glob`, `iterdir`) are synchronous and would stall the event loop while other
  tools run in parallel. Each tool delegates to a module-level sync helper via
  `asyncio.to_thread`. Ruff's `ASYNC240` caught the naive version — a good lint to
  keep on for an async codebase. Bonus: the sync helpers are directly testable.
- **Tools return clean error results for expected failures.** Missing file, not-a-
  directory, unconfigured web search — each returns `ToolResult(is_error=True)` with
  a message the model can act on, rather than raising and relying on the executor's
  generic wrapper. Unexpected errors still fall through to the executor.
- **The shell tool is deliberately un-sandboxed but gated.** It runs `pwsh
  -NoProfile -NonInteractive`; the safety boundary is the PermissionGate (ask +
  shell rules), not a restricted shell. It kills the process on timeout so a hung
  command can't wedge the loop, and caps output before the executor caps it again.
- **Network calls are isolated behind `_tavily_search` / `_fetch_html`.** Tests
  monkeypatch those two seams, so the tool logic (formatting, key handling, error
  paths) is fully covered with no network. `web_fetch` runs `trafilatura.extract`
  for real on sample HTML — a genuine integration check that boilerplate is stripped.
- **Real filesystem + shell tests, mocked web.** Filesystem/shell tests exercise the
  actual OS (shell guarded by `skipif(pwsh is None)`); only the web layer is mocked,
  because that's the only piece that needs the network or a paid key.

## Task 8 — REPL

- **The REPL is a thin event consumer.** It wires config→client→registry→gate→loop,
  then just renders the loop's events, prompts for approvals, and tracks totals. All
  the payoff of the thin-interface rule: `ConsoleRenderer` is ~60 lines and a web UI
  would be a different consumer of the same events, no loop changes.
- **Force UTF-8 stdio on Windows — a real crash, not cosmetics.** The first live run
  died with `UnicodeEncodeError` on `✓`: Windows stdout defaults to cp1252, which
  can't encode the model's Unicode (em-dashes, box glyphs, checkmarks). `main()`
  calls `stream.reconfigure(encoding="utf-8", errors="replace")` before any output.
  `errors="replace"` means even an unforeseen glyph degrades to `?` instead of
  crashing a turn.
- **Ctrl+C cancels the turn, not the session.** `run_turn` runs the loop as a task;
  on `KeyboardInterrupt` it cancels that task, drains it, and returns to the prompt.
  Awaiting a task does receive the interrupt while leaving the task cancellable —
  the standard asyncio pattern for "interrupt the work, keep the shell alive."
- **`PromptSession` is created in `run()`, not `__init__`.** prompt_toolkit opens the
  terminal on construction and throws `NoConsoleScreenBufferError` under pytest.
  Deferring it keeps `Repl` constructible headless, so the approver and a full turn
  are unit-tested against `FakeClient` with a `StringIO` console — no TTY, no network.
- **"Always allow" persists as narrowly as the tool allows.** For `run_shell` it
  writes a prefix rule for that command, for `write_file` it allowlists the parent
  dir, and only otherwise sets a blanket tool allow — so one approval never opens
  more than the user actually approved.
- **Approvals block on `input()` via `asyncio.to_thread`.** The loop already
  resolves permissions sequentially, so a threaded blocking prompt is safe and keeps
  approval UX simple; the main input line uses prompt_toolkit for history/editing.

## Task 10 — Persistence

- **The model is stateless; SQLite is the memory.** The whole conversation lives in
  the `messages` table and is reconstructed for every model call. This makes the
  "model is stateless" architecture rule concrete — nothing about a turn depends on
  process memory surviving.
- **Message content is stored as JSON, verbatim.** Content is a string (user text)
  or a list of blocks (assistant text/thinking/tool_use, or user tool_result).
  `json.dumps`/`loads` preserves the exact structure — critically, thinking blocks
  keep their `signature`, so a resumed session replays to the API unchanged. A test
  round-trips a signature to pin this.
- **Schema version via `PRAGMA user_version`, not a table.** SQLite has a built-in
  per-db integer; the migration runner applies ordered `(version, sql)` tuples when
  the db is behind. No ORM, no framework — the data model stays plain, visible SQL.
- **Save-per-turn is delete-then-reinsert.** At conversation scale, replacing all
  rows each turn is simpler and less bug-prone than tracking incremental appends
  (a turn appends several messages: assistant + tool_results + assistant…). Correct
  beats clever here.
- **A save failure must not kill the session.** `_persist` catches and logs; losing
  one turn's persistence is annoying, losing the live conversation is worse.
- **`--resume` picks the most-recently-updated session.** `latest_session_id`
  orders by `updated_at`, and every save touches it — so "resume" reliably means
  "the conversation I was just having," even across many stored sessions.

## Task 11 — Smoke evals

- **Agents are stochastic, so a single pass proves nothing.** The runner executes
  each scenario N=3 times and only passes if all N pass. A first validation run
  (N=1) is for wiring; the real gate is N=3. Cost is free, so there's no reason to
  skimp on repetitions.
- **Checks are a small declarative DSL, not code in YAML.** `file_exists`,
  `file_absent`, `file_matches`, `answer_matches`, `tool_called`, `tool_not_called`
  cover the three assertion families from the plan (a file was produced with the
  right content / the final answer says the right thing / the right tool ran). The
  scenario author writes YAML; the runner interprets it.
- **Each run is isolated in a temp workdir, then `chdir`'d into.** The scenario's
  setup files are written there and the agent's relative paths resolve there, so
  runs can't pollute the repo or each other. Verified: after a full run, the repo
  has no stray `summary.txt`/`secret.txt`.
- **The three scenarios map to the three things that matter.** Multi-step file work
  (tool composition), web research (grounding), and a *denied* tool (the safety
  path — proving a denial becomes model feedback the agent handles gracefully,
  not a crash). The denial eval uses an approver that says no to `write_file`.
- **Known edge, noted not fixed:** the gate resolves a relative write path against
  `config.root`, while the tool resolves it against the process cwd. In normal use
  (run from the project dir) these coincide; the eval's `chdir` makes them diverge
  harmlessly (the file lands in the workdir via cwd; the gate's allowlist reasoning
  just uses a different base). A future gate could take cwd into account.
- **Not a pytest test.** It hits the live API and costs money, so it's a standalone
  script (`uv run python tests/evals/runner.py`) with pass/fail + per-scenario cost,
  exiting non-zero on failure so it can gate a release later.

## Task 12 — Docs

- **Three docs, three audiences.** `README.md` is for a user/operator (setup, usage,
  the safety model); `docs/architecture.md` is the as-built map for a contributor
  (layers, the loop, data flow, module locations); `docs/decisions/0001` is an ADR
  capturing *why* the loop is hand-built, so the choice isn't silently re-litigated.
- **`PLAN.md` stays forward-looking; `architecture.md` is as-built.** Keeping them
  separate means the plan can describe phases 2–8 while the architecture doc only
  claims what actually exists — a reader never has to guess which is real.
- **The ADR records the seam, not just the decision.** The load-bearing detail is
  that "from scratch" was made reversible by the `LLMClient` interface and the tool
  registry — MCP or a higher-level SDK can slot in behind them later without a
  rewrite. An ADR that only said "we built it ourselves" would miss the point.

## Phase 1.1 — Safety hardening

A pass over the MVP's safety model before adding memory. Each item closes a way
the agent could exceed what a human actually approved.

- **A shell allowlist must model *chaining*, not just prefixes.** The original
  `git status → allow` rule matched `git status; rm -rf x` (longest-prefix match
  wins, and the whole string still starts with `git status`), silently allowing
  the tail. The fix mirrors the write allowlist's "can only tighten" rule: an
  `allow` is downgraded to `ask` whenever the command contains a shell
  metacharacter (`; | & ` `` ` `` `$( ${ > <` newline). Prefixes also now match
  at a **token boundary**, so `git statusfoo` can't inherit `git status`'s allow.
  A `deny` is unaffected — you can't metacharacter your way *out* of a denial.
- **The gate and the tool must resolve a path the same way, or the check is a
  lie.** The gate approved `root/notes/x` while the tool wrote `cwd/notes/x` — a
  classic check-here-act-there gap (harmless only because they usually coincide).
  Now a single `jarvis.paths.resolve_path(raw, root)` is the *one* resolver both
  call, always against `config.root`. `.resolve()` also collapses `..` and
  follows symlinks, so neither can be used to escape the write allowlist.
- **The secret denylist is a code floor, not a config setting.** `.env`, SSH/GPG
  keys, `.aws/credentials`, `.npmrc`, `*.pem` … are denied for read *and* write in
  `jarvis.paths.is_sensitive_path`, which policy can extend (`read_denylist`) but
  never disable. Reasons: (1) a foot-gun edit to `permissions.yaml` shouldn't be
  able to expose credentials; (2) the write side blocks a real attack —
  `write_file(~/.ssh/authorized_keys)` is persistence, not a file save. Committed
  templates (`.env.example`) are the one explicit exception, since the floor
  otherwise errs toward denying.
- **Network tools ask by default because egress is the exfiltration channel.** A
  `web_search`/`web_fetch` sends data *off* the machine; pairing "read anything"
  with "send anywhere" is the leak. Both now default to `ask`; "always" persists a
  tool-level allow. The approval prompt also prints a one-line summary of the
  *actual* call (the URL, the command, the query) — you consent to the action, not
  just the tool name.
- **"Always allow" for a write persists the *resolved* parent, and refuses to
  over-grant.** The old code stored `Path(raw).parent` — a bare relative fragment
  that wouldn't match the gate's resolved allowlist reasoning. Now it stores the
  absolute resolved parent, and `is_safe_to_persist_dir` refuses to persist a
  drive root, the home directory, or a sensitive dir — so one approval can't
  silently authorize writes across your whole home tree. The single approved write
  still proceeds; only the *broadening* is withheld.
- **"Bounded" reads means bounded *memory*, not just bounded output.** The
  executor already truncated tool *results* (context protection), but `read_file`
  did `path.read_bytes()` first — a 5 GB file was fully loaded before truncation.
  The real fix reads at most `cap+1` bytes from disk (`min(max_bytes, limit)` as a
  hard ceiling the model can't raise), so cost is one buffer, not the file size.
- **An intermediate abstract `Tool` subclass can't carry the shared helpers.**
  `Tool.__init_subclass__` enforces `name`/`description`/`Params`, and — because
  `ABCMeta` sets `__abstractmethods__` *after* `__init_subclass__` runs — the
  guard meant to skip abstract bases doesn't fire at class-creation time. A shared
  `_FsTool(Tool)` base therefore raised `TypeError` on import. The fix was
  module-level helpers (`_root`, `_limit`) instead of a base class — a reminder
  that metaclass hooks and `__init_subclass__` interleave in a non-obvious order.
- **Consequence: eval isolation moved from `chdir` to a root override.** Because
  tools now resolve against `config.root` rather than the CWD, the eval runner's
  `os.chdir(workdir)` no longer isolated file writes. It now runs each scenario
  with `config.root` copied to the temp workdir — isolation via the real mechanism
  instead of a process-global side effect (this also closes the "known edge" noted
  under Task 11).

# Phase 2 — Long-Term Memory

The full Phase 2 design is `docs/PLAN-2-memory.md`. These notes capture the
non-obvious *implementation* decisions per task.

## P2 Task 1 — Scaffold

- **`MemoryConfig` is the only Config sub-model with a default_factory.** Every
  other sub-config (`models`, `limits`, `paths`) is a required field on `Config`,
  but memory is a Phase-2 addition and several existing sites build `Config(...)`
  directly (tests, the web-tool fixture). Defaulting it means those callers don't
  have to learn about memory to keep compiling, while `load_config` still populates
  it from YAML. A small, deliberate inconsistency that buys backward compatibility.
- **Memory is a config *toggle*, not just a key check.** `memory.enabled` exists
  independently of whether `VOYAGE_API_KEY` is set, because there are two distinct
  reasons to run without memory — "I don't want it" (config) and "I can't (no key)"
  (degradation, later tasks). Conflating them into one signal would make "disable
  memory but keep the key around" impossible.
- **`voyageai` drags in a heavy transitive tree** (langchain-core, tokenizers,
  huggingface-hub, pillow). Accepted as-is: quality-first, cost/footprint is not a
  constraint, and it's the official SDK. Noting it so a future slim-down (calling
  the Voyage REST endpoint directly via httpx, which we already depend on) is a
  known option if the dependency surface ever becomes a problem.
- **Thresholds live in config from day one**, even before anything reads them.
  Cosine cutoffs are embedding-model-specific and will be tuned from real recall
  logs; hard-coding them would mean a code edit + redeploy to retune. They ship in
  `settings.yaml` with comments so the knobs are discoverable.

## P2 Task 2 — Schema v2 + MemoryStore

- **Store unit-normalized vectors so cosine collapses to a dot product.** Instead
  of computing `dot(a,b)/(‖a‖‖b‖)` per row at query time, normalize once at write.
  Then `search` is a single `matrix @ query` matmul over the live embeddings — the
  norms are all 1, so the dot product *is* the cosine. Cheap, and it forces the
  zero-vector guard (`norm > 0`) into exactly one place.
- **`float32` BLOB round-trip is `tobytes()` / `frombuffer(..., float32)`.** The
  test pins that a `[3,4,0]` input comes back as the unit `[0.6,0.8,0]` at float32
  precision — because a silent dtype or endianness mismatch would corrupt every
  similarity score with no error, just quietly wrong recall.
- **`search` filters by `embedding_model`, not just vector dimension.** Two models
  can share a dimension but live in incompatible vector spaces; comparing across
  them yields meaningless cosines. The column + the `WHERE embedding_model=?`
  filter mean switching embedders later degrades to "no matches from the old
  space" rather than "subtly wrong matches."
- **Three states, and only `DELETE` is forbidden.** `live` → recallable;
  `supersede` marks the loser `superseded` + records `superseded_by` (lineage);
  `forget` marks `forgotten`. Recall/search filter to `live`, but `get(id)` returns
  *any* status — so "what did I forget?" and "what did this replace?" always have
  answers. `forget` returns a bool (rowcount-based) so it's idempotent: forgetting
  an already-forgotten memory is a no-op, not an error.
- **The migration test simulates a real upgrade, not a fresh v2 db.** It hand-builds
  a v1 schema, sets `user_version=1`, inserts a session + message, *then* runs
  `migrate()` — proving the v1→v2 step preserves existing data and adds the new
  columns/table. A test that just opened a fresh (already-v2) db would never
  exercise the `ALTER TABLE`s on populated data.

## P2 Task 3 — Embeddings

- **The `Embedder` protocol mirrors `LLMClient` exactly.** Same trick that let the
  loop go from FakeClient to live with zero changes: the memory layer depends on an
  interface with `embed_documents` / `embed_query`, so `FakeEmbedder` backs every
  offline test and `VoyageEmbedder` drops in for real. Neither the store nor the
  service ever imports `voyageai`.
- **Two embed methods, not one, because Voyage has an `input_type`.** Documents and
  queries are embedded into slightly different regions of the space on purpose;
  calling `embed_documents` for stored memories and `embed_query` for lookups is
  free retrieval quality. The protocol encodes that asymmetry so a backend can't
  quietly ignore it, and the test asserts `VoyageEmbedder` passes the right
  `input_type` for each.
- **`FakeEmbedder` uses `hashlib`, not builtin `hash()`.** Python randomizes string
  hashing per process (`PYTHONHASHSEED`), so `hash("tabs")` differs between test
  runs — which would make similarity-threshold tests flaky. An md5-based bucket is
  stable forever. The bag-of-words construction gives the one property tests need:
  shared words ⇒ higher cosine.
- **`.model` is part of the protocol, not an implementation detail.** The store tags
  every vector with the model that produced it (to refuse mixing spaces), so the
  embedder must advertise its identity. Putting it on the protocol means the service
  reads `embedder.model` without caring which backend it is.
- **Empty-batch short-circuit avoids a pointless API round-trip.** `embed_documents([])`
  returns `[]` without calling Voyage — small, but it means callers don't have to
  guard against empty inputs, and the test pins that no call was made.
