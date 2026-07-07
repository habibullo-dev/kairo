# ADR-0013: A role→route registry, and a metadata-only cost ledger that fails closed

- **Status:** Accepted
- **Date:** 2026-07-07
- **Context phase:** Phase 10A (project workspaces)

## Context

Phase 10 introduces roles (planner/coder/reviewer/…) that may run on different models and,
for analysis roles, a second provider (OpenAI). It also needs to *account* for cost: before
Phase 10 there was no per-call persistence — only per-run aggregates and a non-queryable log
line. The orchestration studio (10B) will make many model calls per run, so a durable,
queryable, privacy-safe cost record is a prerequisite. Two failure modes to design against:
(a) a silent $0 for an unpriced model hiding real spend, and (b) prompts/bodies leaking into
cost rows.

## Decision

### 1. Roles → routes are code constants, resolved through override layers

`ROLES` and `DEFAULT_ROUTES` (a `ModelRoute` = provider + model + effort + text_only) are
frozen code constants — versioned with the code, no migration, no injection surface. A role
resolves through layers: `DEFAULT_ROUTES` ← `settings.yaml model_routes` ← per-project
`settings_json` ← per-run override, each a partial map overriding only the fields it names.
Validation rejects an unknown provider and a **text-only route on a tool-capable role** (a
write-capable executor must be able to drive tools).

### 2. OpenAI is text-only this phase

`OpenAIChatClient` implements the `LLMClient` protocol for tool-less calls only — it raises on
`tools`, maps usage fields **explicitly** (`prompt_tokens`→`input_tokens`, etc., never the
Anthropic shape that would read $0), and fails loud on empty content. Analysis roles
(synthesis/review/judge) can run on it; write-capable executors stay Anthropic. A full
tool-use shim is deferred.

### 3. The ClientFactory fails CLOSED and is the single wrap point

`ClientFactory.for_route` resolves a route to a cached client and raises `ConfigError` naming
the env var when the provider's key is missing — never a silent downgrade to another
provider. It caches Anthropic clients by `(effort, thinking)` (a text-only route ⇒ thinking
off, the `_utility_client` precedent) and one OpenAI client for all its models.

### 4. Pricing is versioned and provider-keyed; unknown ⇒ NULL, never $0

`config/pricing.yaml` is keyed by provider then model. Lookups never cross provider price
spaces: Anthropic tolerates dated-snapshot suffixes (longest-prefix); every other provider is
matched **exactly**, so an unlisted model is unpriced. A missing/malformed file falls back to
the code table (Anthropic only) — it never invents a price. `cost_of`'s legacy unknown→0.0
stays for existing callers; the ledger and all display paths use the `PricingTable` which
returns `None` for unknown, recorded as SQL `NULL`.

### 5. The ledger is a client wrapper writing metadata-only rows

`LedgeredClient` wraps any `LLMClient` and, after each `create`, writes one `model_calls` row
from a `cost_context` contextvar (project / session / role / orchestration_run / purpose /
trace). The row holds tokens, latency, model, purpose, scope, cost — **never a prompt or
body**. Attribution is set by each call site via `cost_scope(...)` (merging, so a nested scope
keeps project/trace and just changes purpose), and set *inside* each child coroutine so a
parallel `gather` can't share one role across agents. All six completion paths (turn,
subagent, compaction, reflection, memory_dedup, digest) are tapped by wrapping the main +
utility clients at the CLI composition sites.

### 6. Ledger degradation is visible (amendment A5)

A ledger *write* failure never breaks the model call — but it flips a `ledger_degraded`
status (surfaced on the Hub and the status strip) and is cleared on the next successful write.
Cost tracking can degrade, but never *silently* disappear.

## Consequences

- No silent $0: an unpriced model records `cost_usd NULL` + a `pricing_unknown` warning, and
  budgets/rollups surface the count of unpriced calls separately (pinned by a no-$0 test).
- Embeddings (Voyage) and voice (STT/TTS) are deliberately **out** of the completion ledger —
  documented, not accidental; voice keeps its own egress-byte accounting.
- The single wrap point (ClientFactory + the two CLI-composed clients) means 10B orchestration
  clients are ledgered for free; per-role/per-project routing threads through the same
  `config.model_copy(update=…)` pattern the sub-agent service already uses.
