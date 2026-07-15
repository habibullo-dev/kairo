# Eval cost control — choose the safest mode

Kira proves most quality without network calls. Live providers are reserved for adapter checks,
cassette refreshes, and deliberate phase-closeout gates. Start with the cheapest useful tier and
move upward only when the lower tier cannot answer the question.

## Before you run

`kira eval plan` is read-only: it makes no API calls and does not acquire Kira's exclusive writer
lock, so it may run while the workspace is open.

Every other eval command acquires the reset-sensitive writer lock, including `gate`, `run`, and
`smoke` even in keyless replay mode, offline `aggregate`, and the live `cache-ab` and `skills-ab`
probes. Stop the Kira UI, Kira terminal/REPL, and any other Kira writer before running one of those
commands.

## Cost ladder

| Tier | Command | Incremental API spend | API key? | Use it for |
|---|---|---:|---|---|
| 1. Unit tests | `uv run pytest -q` | $0 | no | Structure, logic, and safety checks. Keep this green first. |
| 2. Eval plan | `uv run kira eval plan --suite core` | $0 | no | Scope, cassette inventory, and known spend facts. It is not a forecast. |
| 3. Replay gate | `uv run kira eval gate --suite core` | $0 | no | Deterministic, keyless replay from committed cassettes. Misses fail closed. |
| 4. Provider smoke | `uv run kira eval smoke --provider deepseek --live --max-cost-usd 3` | unknown in advance | yes | Two tiny requests that check provider auth, client construction, and parsing. |
| 5. Record misses | `uv run kira eval gate --suite core --record --max-cost-usd 5` | unknown in advance | yes | Reuse hits and fill missing cassettes after a scenario or prompt change. |
| 6. Judged live gate | `uv run kira eval gate --profile live-chunked --live --max-cost-usd BUDGET` | unknown in advance | yes | Deliberate phase closeout under an owner-selected LLM stop threshold. |

Replace `BUDGET` with a positive finite USD value chosen before the run. It is a stop threshold,
not a promise that the provider bill will remain below that number.

## What `plan` tells you

Choose the mode you intend to run:

```powershell
uv run kira eval plan --suite core
uv run kira eval plan --suite core --record --max-cost-usd 5
uv run kira eval plan --suite all --live --max-cost-usd 10
```

The plan prints:

- the selected suite, run count, mode, and scenario count;
- repository-wide cassette files grouped as LLM, embedding, web, smoke, and other;
- the last recorded gate's modeled cost, suite, and run count as historical context only;
- `$0` incremental API spend for replay, or `unknown before provider calls` for record/live;
- the supplied metered-LLM stop threshold and its exclusions for record/live.

Cassette inventory is not selected-suite coverage. Gate history does not record replay, record,
or live mode, so its last modeled cost is not a spend forecast. A replay plan ignores any supplied
`--max-cost-usd` value because replay never calls a provider.

## Modes and spend control

`gate`, `run`, and `smoke` support three mutually exclusive modes. `plan` only describes one of
those modes; it does not execute it. `cache-ab` and `skills-ab` are live-only, while `aggregate`
is network-offline.

- **Replay** (default): cassette-wrapped LLM, embedding, and web calls use committed values. A
  miss raises an error; it never silently falls through to a provider.
- **Record** (`--record`): existing LLM, embedding, and web hits are reused. Only misses call a
  provider and are recorded.
- **Live** (`--live`): hits are bypassed and providers are called. A priced LLM response that keeps
  cumulative spend at or below the threshold is recorded. If charging a completed response pushes
  spend above the threshold, the charge raises before `store.put`, so that crossing response is not
  persisted. An unpriced resolved response likewise raises before persistence.

Cost authorization is command-specific:

- `gate` and `run` require an explicit positive finite `--max-cost-usd` in record/live mode.
- `smoke` defaults to `3` in record/live mode; pass it explicitly in reviewed commands.
- `cache-ab` and `skills-ab` require both `--live` and a positive finite threshold.

The threshold is shared by the metered LLM clients created in one CLI process. Kira checks already
completed LLM spend before starting the next call, then prices a response after it returns. A call
that starts below the threshold can cross it; Kira then raises before persisting that response or
starting another metered call. Concurrent calls already past the check can also finish. The
threshold excludes Voyage embedding and Tavily/web charges and is not a provider-side billing
ceiling.

An unpriced resolved model can be detected only after its response arrives, so that completed call
cannot be prevented. Each CLI process starts a new threshold counter. Resuming a chunked gate in a
new invocation therefore starts a new counter; operators must track cumulative spend across
invocations separately.

## Gate, chunk, and artifact behavior

The default artifact root is `data/evals`; a custom `paths.data_dir` changes it to
`<configured data_dir>/evals` everywhere.

- A completed gate writes `<configured data_dir>/evals/<timestamp>-<rev>/` and appends one line to
  `<configured data_dir>/evals/history.jsonl`.
- `run` stages one suite and writes no gate or history entry.
- `aggregate` merges completed chunks into one gate and one history line.
- The default chunk stage is `<configured data_dir>/evals/_chunked-<rev>`.
- At the same revision, chunk resume skips completed scenarios and chunks. A new revision gets a
  new default stage.
- `--profile live-chunked` does not accept `--suite`, `--scenario`, or `--only` narrowing.
- `--compare REV` uses the newest local history entry whose revision begins with `REV`.
- `--propose-baselines` prints YAML suggestions; it never edits `baselines.yaml`.
- Every completed gate saves `report.md`; `--report` also prints it to the terminal.

The full judged live profile may need multiple invocations. Record the threshold and actual
provider charges for every invocation rather than treating the per-process counter as a cumulative
budget.

## Fable cache A/B probe

Use this measurement-only probe when deciding whether Fable prompt caching deserves follow-up:

```powershell
uv run kira eval cache-ab --live --max-cost-usd 5 --runs 3
```

Both arms share one metered-LLM spend stop threshold. The report is written to
`<configured data_dir>/evals/cache-ab/<timestamp>-<rev>/report.json`. The probe does not edit
`config/settings.yaml`, change routing, append gate history, alter committed cassettes, or enable
caching in production.

Both arms must pass the existing deterministic checks. The off arm must report zero cache tokens;
the on arm must show a write followed by a read. Otherwise the probe returns `NOT_ELIGIBLE`
instead of implying a saving. `PASS` is evidence for human review, not authorization to change a
production flag.

## Fable skill-pack A/B probe

Use this measurement-only probe to compare the configured shadow packs with no-skill behavior:

```powershell
uv run kira eval skills-ab --live --max-cost-usd 10 --runs 3
```

Both arms and both probes share one metered-LLM spend stop threshold. The report is written to
`<configured data_dir>/evals/skills-ab/<timestamp>-<rev>/report.json`. The active arm uses a
temporary re-hashed copy of configured packs, so the probe does not change on-disk activation or
`config/settings.yaml` and does not append gate history.

The report contains deterministic rubric scores, modeled LLM costs, arm order, and pinned pack
manifests—not prompts, compiled pack text, or child reports. `PASS` means the active arm scored
higher on these probes with every configured pack covered; activation still requires separate
human review. Any other outcome keeps production in shadow mode.

## Cassettes and data handling

Committed cassette data lives under `tests/evals/cassettes/`:

- direct JSON files hold LLM responses and token usage;
- `embeddings/` and `web/` hold cached external values;
- `smoke/` holds provider-smoke LLM responses.

The cassette key is a one-way hash of the request determinant, but that hash does not sanitize the
stored response or external value. Model output and web results can contain sensitive text. Never
record secrets or private production data, inspect cassette diffs before committing, and treat
committed cassette bodies as repository-visible test data.

After an approved scenario or prompt change:

```powershell
# Reuse hits and fill only misses under a metered-LLM stop threshold.
uv run kira eval gate --suite core --record --max-cost-usd 5

# Review every body before staging repository-visible fixtures.
git diff -- tests/evals/cassettes
git add tests/evals/cassettes
```

Replay remains deterministic because eval tools use controlled fixtures and a frozen eval clock.
If a request changes, its downstream cassette misses and the run fails closed, signaling that an
explicit reviewed refresh is required.

## Historical keyless proof — 2026-07-08

On the 2026-07-08 snapshot, the core gate replayed 19/19 scenarios with deliberately invalid
Anthropic, Voyage, and Tavily credentials: **GATE PASS, 19/19**, with `$0` incremental API spend.
That proved every then-current LLM, embedding, and web request came from a cassette because any
network fallback would have failed authentication.

This is dated snapshot evidence, not a claim that today's request hashes were freshly verified.
The final Kira identity/prompt cutover changes LLM request hashes; re-record under a finite
threshold, rerun the invalid-key replay proof, and refresh this section as one reviewed milestone.

| External call type | Replay wrapper | Keyless on the dated snapshot? |
|---|---|---|
| LLM main loop, sub-agents, judge, and unattended jobs | `CassetteClient` | yes |
| Voyage knowledge and memory embeddings | `CassetteEmbedder` | yes |
| Tavily/web research and ingest | `wrap_web_tool` | yes |
| Scheduler timing | frozen eval and seed clocks | yes |

Replay incremental API spend is `$0`. Record/live spend is unknown before provider calls, can
cross the LLM threshold, and may also include embedding or web charges outside that threshold.
