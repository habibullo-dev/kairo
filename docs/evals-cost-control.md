# Eval cost control — when to use which mode

Kairo proves quality mostly **without burning real tokens**. Live API calls are reserved for
adapter fidelity and final checkpoint confidence. This is the cost ladder, cheapest first.

## The ladder (cheapest → most expensive)

| Tier | Command | Cost | API key? | Use it for |
|---|---|---|---|---|
| 1. Keyless unit tests | `uv run pytest -q` | $0 | no | Structure/logic/safety pins — the bulk of correctness. Always green before anything else. |
| 2. Replay eval (DEFAULT) | `uv run jarvis eval gate --suite core` | $0 | no | Re-proving eval scenarios from committed cassettes. Deterministic, keyless. Fails closed if a cassette is missing. |
| 3. Provider smoke bench | `uv run jarvis eval smoke --provider deepseek --live` | ≤ $3 (cap) | yes (that provider) | A 2-scenario liveness check that a provider's client + auth + parsing work. 1 run, cost-capped. |
| 4. Record cassettes | `uv run jarvis eval gate --suite core --record --max-cost-usd 5` | one-time, capped | yes | Fill/refresh the cassette cache after a scenario or prompt change, so tier 2 stays free. |
| 5. Full judged live gate | `uv run jarvis eval gate --profile live-chunked --live` | real $ | yes | **Phase-closeout only** (ADR-0005). The authoritative judged verdict before shipping a phase. |

**See the cost before you run:** `uv run jarvis eval plan --suite core [--live]` prints the
projected spend (replay = $0; live = estimated from the last live gate) and how many cassettes
are already cached. The Daily screen shows the same note under the eval chip.

## Modes (the `--live` / `--record` flags)

Every `gate` / `run` / `smoke` invocation runs in one of three modes:

- **replay** (default, no flag): each model call is served from a committed cassette. A **miss
  fails closed** — it never silently calls the API; it prints the exact `--record` command.
  Keyless and $0.
- **`--record`**: cassette hits are reused; misses make a real (capped) call and are recorded.
  This is the cheap way to fill the cache after a change — only the *new* calls cost money.
- **`--live`**: always calls the real API (and re-records), for fidelity. Use for the closeout
  gate and provider smoke checks.

`--max-cost-usd USD` is a hard cap on any live/record run: it aborts before the next call once
the cap is reached, and an unpriced model under a cap fails closed (an unmeasurable spend is not
allowed). Smoke defaults to a $3 cap.

## Cassettes

A cassette is a committed JSON file under `tests/evals/cassettes/` (smoke: `.../smoke/`) holding
one model **response** (assistant content + token usage), keyed by a hash of the full request
(provider + client config + system + messages + tools + max_tokens + tool_choice + temperature).
They are the same trust class as a scenario fixture — model output, no secret ever enters a
cassette, and the key hash is not reversible.

**Record once, replay free.** After adding or changing a scenario/prompt:

```powershell
# fill only the missing/changed cassettes (existing ones are reused), capped:
uv run jarvis eval gate --suite core --record --max-cost-usd 5
# commit the new cassettes so CI + teammates replay for free:
git add tests/evals/cassettes && git commit -m "evals: record cassettes for <change>"
```

Replay is deterministic because eval tools run over temp-dir fixtures, so each call's messages
reproduce given the prior cached responses. If a tool becomes non-deterministic, its downstream
cassettes miss and the run fails closed (a signal to re-record, never a silent live call).

## Rules

- The **default is keyless replay** — a bare `jarvis eval gate` costs $0 and needs no key.
- A missing cassette in replay mode is a **hard failure**, never an implicit live call.
- **Live is explicit** (`--live`/`--record`) and **capped** (`--max-cost-usd`).
- The **full judged live gate stays the phase-closeout ritual** (ADR-0005) — it is no longer the
  default invocation, only the deliberate one.
- Cassettes are committed; recording is a dedicated, reviewed commit like a baseline ratchet.
