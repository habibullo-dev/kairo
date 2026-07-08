# ADR-0016: Direct provider integration — compat-first clients, authority tiers, fail-closed

- **Status:** Accepted; implemented in Phase 10C (Tasks 1–7; live verification pending)
- **Date:** 2026-07-08
- **Context phase:** Phase 10C (Direct Provider Integrations — Qwen / DeepSeek / GLM / Gemini)

## Context

The Orchestration Studio (ADR-0014/0015) runs project teams of worker roles. Everything ran on
Anthropic. The goal of 10C is to add **cheap, scalable worker models** — Qwen, DeepSeek, GLM
(Z.ai), Gemini — for drafting, research, inspection, and summarization, WITHOUT weakening any
safety contract and WITHOUT letting a cheap model become the deciding authority. The failure mode
to avoid is a pile of bespoke provider SDKs, each a place to mis-wire auth, cost, or trust.

## Decision

### 1. The provider catalog is the safety model; enforcement is derived (ADR-0015 applied to models)

Every provider is a `ProviderSpec` row in `PROVIDER_CATALOG` (`models/providers.py`): `api_style`,
`credential_env`, `tool_capable`, `private_ok`, `trusted_authority`, `core`, `default_base_url`,
`auth_style`, `default_models`. `PROVIDERS` and `TRUSTED_AUTHORITY_PROVIDERS` derive from it. The
client factory and the route registry read the catalog — there is no per-provider trust decision
to get wrong.

### 2. Compat-first clients — no new SDK, no bespoke protocol

DeepSeek, Qwen (DashScope), and GLM (Z.ai) publish **Anthropic-Messages-compatible** endpoints.
The loop already round-trips Anthropic content blocks verbatim, so those three reuse
`AnthropicClient` with `base_url` + `compat=True`: a **capability-degradation profile** that sends
only the conservative core — no `output_config`/effort, no adaptive `thinking` (compat endpoints
reject/ignore them); no `cache_control` is sent anywhere in the codebase, so none to strip.
`auth_style` selects the header per provider — `bearer`/`auth_token` for Z.ai, `x-api-key` for
DeepSeek/Qwen. A **fail-loud guard** rejects an empty-content or zero-usage compat response
(untracked spend is a defect, never a silent $0). **Gemini** has no Anthropic-compat endpoint, so
it rides the existing **text-only** `OpenAIChatClient` + `base_url` — text-only this phase; tool/
function calling and multimodal are deferred to a future phase after fidelity tests. A compat
endpoint that fails live fidelity ships text-only or not at all — never a silent degradation.

### 3. Authority tiers — Fable/Opus stay the deciding layer (no routing escape hatch)

`FINAL_AUTHORITY_ROLES = {planner, judge}` and `PRIVATE_CONTEXT_ROLES = {utility}` can ONLY resolve
to a `trusted_authority` provider (`TRUSTED_AUTHORITY_PROVIDERS = {anthropic}` this phase). Enforced
in the pure `validate_route` at EVERY override layer (settings / project / run) — a cheap worker
can never become the head synthesizer, the final reviewer, the eval judge, or the processor of raw
private conversation content. The engine's synthesis/verdict already run on the head (planner)
route, so this keeps the deciding model trusted by construction. Giving another provider final
authority is a **separate, explicit design change** (a code edit to the trusted set, reviewable),
never a routing option. A `TOOL_CAPABLE` role additionally requires a `tool_capable` provider, so
`coder → gemini` is rejected at validation, not left to fail at the client.

### 4. Availability is fail-closed at three layers

A provider is routable only when it is `(core OR in providers.enabled) AND its key is present AND
it has ≥1 priced model`. Core providers (anthropic/openai) are always routable (key enforced at
the factory); opt-in providers (deepseek/qwen/zai/gemini) must clear the full bar. A miss is a
specific state the Studio renders (`disabled` / `missing_credentials` / `unpriced`) and route
resolution raises `RouteError` with the reason — never a silent downgrade to another provider.
`providers.enabled` is `[]` by default: byte-identical to pre-10C (all ten roles resolve to their
unchanged anthropic defaults).

### 5. Provider privacy — a model is a context sink (B1 extended)

Every non-Anthropic provider is `private_ok=False`. The engine refuses a run whose context bundle
carries PRIVATE provenance when any member's route resolves to a `private_ok=False` provider —
`ProviderContextError`, raised BEFORE any run row opens (like the two-step confirm: no new status,
no migration), never a silent reroute. The tempting "Gemini may see Workspace data since Google
already holds it" shortcut is REJECTED: connector-custodian and API-processor are different data
flows; a `providers.allow_private` opt-in is a future design gated on its own review.

### 6. Cost — priced or blocked

`pricing.yaml` prices deepseek/zai/gemini (verified from official docs 2026-07-08) with EXACT-match
semantics (an unlisted model is unpriced ⇒ ledger records NULL ⇒ the worst-case reservation blocks
the run). Qwen ships **UNPRICED on purpose** — no official price was verifiable — so it is
fail-closed/blocked until real DashScope numbers are filled. Attribution
(project/team/role/stage/mode/provider/model) needs no schema change; migrations stay at v8.

## Consequences

- Enabling a worker provider is `.env` key + `providers.enabled` + (for the UI) a local flag —
  no redesign. The safety model already classifies it.
- Repo code IS sent to a cheap provider when a user routes a coder/researcher there — that is the
  feature, stated plainly; per-project routing keeps a sensitive project all-Anthropic.
- Z.ai (GLM) is fully built and cataloged but its live console was unavailable at ship time, so
  its live verification is pending; its missing-key/unavailable path is part of the fail-closed
  proof (`test_provider_safety.py`).
- Pricing can drift; `effective` dates + budget caps + exact-match-fail-closed bound the risk.

## Alternatives rejected

- **A bespoke SDK per provider.** Rejected: each becomes a place to mis-wire auth/cost/trust.
  Compat-first reuses one battle-tested protocol path.
- **A config flag to let GPT/another provider be final authority.** Rejected for 10C: final
  authority is a code-level trust decision, not a routing knob.
- **Tool-capable Gemini now.** Deferred: needs an OpenAI-function-calling ↔ Anthropic-tool-block
  translation layer + fidelity tests. Gemini is text-only (analysis/research) this phase.
- **Enabling providers by default.** Rejected: `providers.enabled: []` fail-closed default keeps
  behavior byte-identical until a human opts in.
