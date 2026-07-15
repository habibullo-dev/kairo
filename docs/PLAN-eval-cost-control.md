# Eval Cost-Control Layer — plan

*(2026-07-08. Goal: prove Kairo quality mostly WITHOUT burning real tokens; reserve live calls
for adapter fidelity + final checkpoint confidence. Baseline: Phase 10C complete keyless (1364
passed / 1 skipped). NEVER commit `docs/PLAN.md`, `docs/PLAN-7-*`, `mcp_sample.json`,
`config/settings.yaml`, `config/permissions.yaml`, `design/`, or secrets/vault/data.)*

## Problem

Every eval scenario runs the real agent loop against the live Anthropic API (and, for provider
work, live worker providers). The judged gate costs real $ per run × 3 runs × N scenarios. We
want the default to be **free and keyless**, with live calls opt-in and hard-capped.

## Design — a cassette (VCR) layer at the LLMClient boundary

The eval builds all its model clients through `_default_client_factory(cfg)` (main/utility/loop)
and a separate `judge_client`. Wrapping each in a **CassetteClient** intercepts every
`create()` call — no scenario or loop change. The pattern mirrors `LedgeredClient` (a decorator
over `LLMClient`).

### Modes (default = replay)
- **replay** (DEFAULT): a cassette hit returns the cached `ModelResponse`; a **miss FAILS CLOSED**
  (`CassetteMissError`) — never a silent live call. Keyless, $0, deterministic.
- **live** (`--live`): call the real client, **record** the response to its cassette (refresh),
  and enforce the cost cap. A hit still records-through (refreshes) so `--live` always exercises
  the real path (that's its purpose: fidelity).
- **record** (`--record`, a convenience = live-but-reuse): hit → cached; miss → live + record +
  cap. Fills the cache cheaply for later replay without re-paying for existing cassettes.

### Cassette key (item 2)
`sha256` over canonical JSON of: `provider`, `model`, a **client signature** (`effort`,
`thinking`, `compat` — the settings that change output), `system`, `messages`, `tools`,
`max_tokens`, `tool_choice`, `temperature`. Replay is deterministic because eval tools run over
temp-dir fixtures, so each call's `messages` reproduce given the prior cached responses.

### Storage
`tests/evals/cassettes/<keyhash>.json` — the serialized `ModelResponse` (content_blocks /
stop_reason / usage / model) + metadata (provider, model, scenario, seq, recorded_at). Committed
to git so keyless CI/dev replay works with NO API key. Small (tiny eval scenarios).

### Cost cap (item 4)
`CostCap(max_usd, pricing)` — accumulates live-call cost (from `usage` × pricing); checked
BEFORE each live call (projected next-call worst case) and AFTER (actual) → `CostCapExceeded`
aborts the run. Default cap: gate live = configurable; smoke = **$3**.

## Tasks

- **E1 — cassette core** (`tests/evals/cassette.py`): `cassette_key`, `CassetteStore`,
  `CassetteClient` (replay/live/record; fail-closed miss), `CostCap` / `CostCapExceeded` /
  `CassetteMissError`. Keyless unit tests with a fake inner client.
- **E2 — runner wiring**: `run_once`/`run_scenario`/`run_chunk`/`gate` gain `mode` +
  `max_cost_usd`; wrap the factory output + judge client in `CassetteClient`. CLI: `--live`,
  `--record`, `--max-cost-usd`; **default replay**. Fail-closed on missing cassette prints the
  exact record command. Full judged live gate stays `gate --live` (closeout-only, item 6).
- **E3 — provider smoke bench** (`kira eval smoke`): tiny built-in smoke scenarios; `--provider`
  (deepseek/gemini/qwen/zai/anthropic), 1 run default, replay by default, `--live` +
  `--max-cost-usd 3` default. Cassettes per provider so a keyless `smoke` replays.
- **E4 — cost projection** (item 7): `project_eval_cost(scenarios, runs, mode, cassettes,
  pricing)` → `{projected_usd, basis}` (replay ⇒ $0; live ⇒ sum of cassette costs if present,
  else a max_tokens heuristic, flagged). CLI `eval plan` prints it; Lab/Daily + Studio read model
  surface "projected eval cost / last gate cost / replay = $0" before anyone runs.
- **E5 — docs** (`docs/evals-cost-control.md`): when to use keyless unit tests vs replay eval vs
  smoke bench vs full live gate; how to record/refresh cassettes; the cost ladder.

## Safety / non-negotiables
- Default is replay/keyless; live is explicit (`--live`) and capped (`--max-cost-usd`).
- A missing cassette in replay mode is a hard failure, never a silent live call.
- Cassettes store model OUTPUT only (assistant content/usage) — the same trust class as a
  scenario fixture; they are framed untrusted wherever a scenario already frames model output.
  No secret/key ever enters a cassette (requests hash system+messages; the STORED value is the
  response, and the key hash is not reversible).
- The full judged live gate remains the phase-closeout ritual (ADR-0005) — unchanged, just
  no longer the default invocation.
- Eval harness only (`tests/evals/`), plus a read-only cost-projection surface in the UI. No
  change to `PermissionGate`/taint/budget/service or model catalogs.
