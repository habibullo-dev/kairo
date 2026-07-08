# ADR-0018: Provider-agnostic Context Reuse (prompt/context caching)

*Status: accepted (S7, 2026-07-08). Keyless substrate; no provider is caching live yet.*

## Context

Prompt/context caching cuts cost + latency for exactly the workloads Kairo leans on (orchestration
fan-outs, planner/judge/review agents, long project sessions). But every provider does it
differently — Anthropic explicit `cache_control` (5m/1h TTL), OpenAI automatic prefix caching +
`prompt_cache_key`, Gemini implicit caching (+ explicit `CachedContent`), DeepSeek automatic disk
prefix caching, Qwen/DashScope `cache_control` blocks — and an unverified provider (Z.ai) does who
knows what. Caching also touches data-flow safety: a cached prefix that contains private/project
content is a new place that content lives.

## Decision

A single **ContextReusePolicy** where **capability is data and behavior is derived** — the same
discipline as the provider/service catalogs. Not Anthropic-caching-with-a-coat-of-paint.

1. **Capability metadata on `ProviderSpec`** (8 fields; default fail-closed OFF): `supports_context_reuse`,
   `context_reuse_mode` (one of `off | automatic_prefix | explicit_breakpoint | explicit_resource |
   provider_default`), `supports_cache_key`, `supports_cache_ttl`, `reports_cached_tokens`,
   `cache_min_tokens`, `cache_ttl_options`, `cache_private_allowed`. `capability(provider)` resolves
   fail-closed: unknown / unsupported / unrecognized-mode ⇒ OFF. Z.ai = OFF until verified.
2. **Stable-first prompt layout** (`prompt_layout.assemble`): stable, non-sensitive framing leads
   (system contract + playbooks, tool schemas, team profiles, service catalog, stable project
   instructions); volatile per-turn content trails. Five named component hashes +
   composite `stable_prefix_hash` — a change to any stable section deterministically busts the
   composite so a stale cache is never reused. Helps even providers we cache nothing for.
3. **Policy → directive** (`plan`): per mode, the concrete control — a breakpoint (Anthropic/Qwen),
   a `prompt_cache_key` = the stable-prefix hash (OpenAI), or nothing (Gemini implicit /
   `explicit_resource` deferred / OFF). Emitters produce the exact provider control.
4. **Normalized cross-provider cost ledger** (migration v11 on `model_calls`, metadata-only):
   `cached_input_tokens`, `provider_cache_mode`, `provider_cache_hit_tokens`,
   `estimated_cache_savings_usd`, `stable_prefix_hash` (Anthropic's cache_write/read already
   existed). `normalize_cache_usage(provider, raw)` maps each provider's usage onto these; an
   absent field is **NULL, never a fabricated 0**. Cost Center surfaces hit tokens / savings /
   hit-rate by provider·model·project·team + top-benefit routes.

## Safety (non-negotiable, pinned)

- **Cache is NOT memory.** The cache layer does no persistence, no I/O, no retrieval — it only
  classifies + decides (pinned: no `aiosqlite`/`httpx`/`open`/`os.environ` in the modules). There
  is no prewarming path.
- Caching **never weakens** `context_policy`, project scoping, taint/egress, privacy routing,
  retention, or model authority — it is an orthogonal add-on that can only *narrow*.
- **Default: stable, non-sensitive prefix only.** A sensitive (private/project) stable prefix is
  cached ONLY when the provider permits private caching (`cache_private_allowed`, which implies
  `private_ok`) AND the route explicitly allows it. No provider caches a private prefix without
  route permission; a non-private provider never caches private content at all.

## Deferred (explicit)

- **Live client wiring.** The client's `create()` takes a plain `system` string today; threading a
  section-level breakpoint / setting the SDK cache key is the *enable-step* (rides on the emitters
  here), done when caching is turned on live — out of scope for this keyless substrate.
- **Gemini `explicit_resource` (`CachedContent`).** Deferred pending a privacy review (a
  provider-side cached resource of large/private docs is a retention surface); Gemini uses implicit
  caching (`provider_default`) only for now.
- **Z.ai** stays OFF until its cache behavior is verified against docs.

## Consequences

Every provider's cache usage lands in one normalized shape; the Cost Center shows savings; the
stable-first ordering + hashes help immediately (even keyless). Turning caching on for a provider
is a small enable-step (wire the emitter into its client + flip nothing else), gated by the
capability data and the private-content policy already in place.
