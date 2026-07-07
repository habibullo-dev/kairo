# Kairo Phase 10C — Direct Provider Integrations (Qwen · DeepSeek · GLM/Z.ai · Gemini)

*(Planned 2026-07-08 by Fable; to be committed as `docs/PLAN-10C-providers.md` in Task 1.
Baseline: Phase 10B fully closed — live verification green, chunked gate PASS 42 / Safety
CLEAN 23/23, baselines ratcheted (`6e2c870`), config-load bug fixed (`f086cc6`); suite at
1263 passed / 1 skipped, ruff clean, migrations v8. NEVER commit `docs/PLAN.md` or
`docs/PLAN-7-voice-consent-checkpoint.md`. `design/` is a future UI/UX reference — DO NOT
touch, modify, or commit it in this phase.)*

## 0. Placement decision (planning question 1)

**A small Phase 10C, before Phase 11.** Not folded into Phase 11:

- It is pure backend, keyless-testable, and a natural extension of the Phase 10A
  ModelRegistry/ClientFactory — the same fail-closed patterns, no UI beyond availability
  chips the Studio read model already half-implements (`readmodels.py` already builds a
  per-provider `present` map).
- Phase 11 is a large UI/data phase (search/artifacts/project UX) with its own checkpoint
  discipline; mixing tool-calling/provider risk into it dilutes both. The provider *settings
  screen* stays in Phase 13 per the approved roadmap — 10C ships the backend states it will
  render.
- It compounds immediately: every later phase's orchestration gets cheap scalable workers.

Effort: **Medium** (~8 tasks). Two known follow-ups are handled: the **Semgrep default-config
fix is folded in as a small task** (T6); the **Playwright driver stays a Phase 11
prerequisite** (untouched here).

## 1. What already exists (do not rebuild)

- `models/roles.py` — ROLES, `TOOL_CAPABLE_ROLES = {coder}`, `PROVIDERS = {anthropic, openai}`,
  `ModelRoute(provider, model, effort, text_only)`, all-Anthropic `DEFAULT_ROUTES`.
- `models/registry.py` — layered resolution (code defaults ← `config.models.routes` ←
  project `settings_json["model_routes"]` ← per-run), `validate_route` (known provider;
  text-only rejected on tool-capable roles).
- `models/factory.py` — `ClientFactory.for_route`, **fail-closed** on missing key
  (`ConfigError` naming the env var; never a silent provider fallback), per-key client cache.
- `models/openai_client.py` — TEXT-ONLY OpenAI chat adapter; raises `UnsupportedToolUseError`
  if tools are passed; explicit usage-field mapping (never the Anthropic shape); empty
  responses fail loud.
- `core/anthropic_client.py` — the streaming + tool-use client the AgentLoop trusts;
  content blocks round-trip verbatim.
- `config/pricing.yaml` v2 — keyed provider→model; **non-Anthropic models match EXACTLY,
  unpriced fails closed** (NULL cost + warning, never $0); orchestration estimates **block**
  unpriced routes (`treat_unpriced_as_blocking`).
- `observability/ledger.py` — LedgeredClient wraps ANY provider uniformly; `model_calls`
  already carries purpose/agent_role/model/**provider**/team/stage.
- Engine `_spawn_member` — per-member `registry.route(...)` → `factory.for_route(...)`.

10C is therefore mostly **catalog + validation + two client extensions + pricing + read
models** — not a new subsystem.

## 2. Architecture

### 2.1 Client strategy: Anthropic-compat first (planning question 3)

DeepSeek, Z.ai (GLM), and Alibaba (Qwen) all publish **Anthropic-Messages-compatible
endpoints** (built for Claude Code interop). Kairo's AgentLoop round-trips Anthropic content
blocks verbatim, and `AnthropicClient` already implements streaming, tool use, and retries
against exactly that wire format. So:

- **Tool-capable workers (DeepSeek, GLM, Qwen)** — extend `AnthropicClient` with
  `base_url` + a `CompatCapabilities` degradation profile: **thinking off, no
  `cache_control`, no `output_config`/effort, no cache-multiplier pricing, no dated-snapshot
  prefix matching**. One battle-tested protocol path; near-zero new parsing code.
- **Gemini** — no Anthropic-compat endpoint. It rides the existing **text-only**
  `OpenAIChatClient`, extended with `base_url` (Gemini's OpenAI-compatible endpoint).
  Gemini routes are `text_only: true` this phase — the registry already rejects text-only
  on tool-capable roles, which matches its intended use (research/context synthesis, drafts).
- **Deferred:** a full OpenAI function-calling ↔ Anthropic tool-block translation layer
  (only needed if a compat endpoint proves unfaithful in live verification, or to make
  Gemini tool-capable later); Gemini native SDK + multimodal (image/video/doc understanding);
  Workspace-private Gemini flows (needs the explicit privacy opt-in design, post-9B);
  automatic pricing sync; local/self-hosted models (Ollama) as a future catalog row.

Default base URLs ship in the provider catalog and are config-overridable
(`providers.base_urls`). **Exact URLs and model ids are verified against live provider docs
at T3/T5 and pinned then** — candidates: DeepSeek `https://api.deepseek.com/anthropic`
(deepseek-chat, deepseek-reasoner); Z.ai `https://api.z.ai/api/anthropic` (current GLM
flagship + air tier); Qwen DashScope Anthropic-compat endpoint (qwen3-coder family), with
DashScope's OpenAI-compat `…/compatible-mode/v1` as the text-only fallback if the
Anthropic-compat path proves unfaithful; Gemini
`https://generativelanguage.googleapis.com/v1beta/openai/` (current flash + pro tiers).
Rule: **if a compat endpoint fails live fidelity checks, the provider ships text-only or
not at all — never a degraded silent fallback.**

### 2.2 Provider catalog: `models/providers.py` (mirror of SERVICE_CATALOG)

```python
@dataclass(frozen=True)
class ProviderSpec:
    name: str                 # "deepseek" | "qwen" | "zai" | "gemini" | "anthropic" | "openai"
    api_style: str            # "anthropic" | "anthropic_compat" | "openai_compat"
    key_service: str          # Config.require() name -> env var
    default_base_url: str | None
    tool_capable: bool        # anthropic_compat: True; openai_compat: False (this phase)
    private_ok: bool          # may receive PRIVATE-provenance context (anthropic only)
    trusted_authority: bool   # may hold final-authority roles (anthropic only)
    notes: str
```

`PROVIDERS` (roles.py) derives from the catalog keys. The catalog is the safety model;
enforcement derives from it — never hand-set per call site (the ADR-0015 lesson applied to
models).

**Availability = `providers.enabled` flag ON ∧ key present ∧ ≥1 priced model in
pricing.yaml.** Anything else ⇒ routes to that provider fail validation with a named reason,
the Studio shows disabled / missing-key / unpriced, and nothing registers. `enabled: []` by
default — byte-identical behavior until a human lists a provider (the services precedent).

### 2.3 Authority tiers (routing principles, planning question 4)

Code constants in roles.py, enforced in `validate_route` (no config escape hatch this phase):

- `TRUSTED_AUTHORITY_PROVIDERS = {"anthropic"}`.
- `FINAL_AUTHORITY_ROLES = {"planner", "judge"}` — planner is the head
  planner/synthesizer/final verdict (engine stages already use the head client); judge is
  the eval judge. A route putting a non-trusted provider on these roles raises `RouteError`
  at resolution — settings, project, and per-run layers alike.
- `PRIVATE_CONTEXT_ROLES = {"utility"}` — utility processes raw conversation content
  (memory extraction, compaction, digest summarize), which is private by nature; it stays on
  trusted providers this phase. Cheap-model summarization happens in orchestration worker
  roles over provenance-checked bundles instead.
- Worker/assistant roles (`coder`, `reviewer`, `security`, `ux`, `qa`, `researcher`,
  `docs`) may route to any AVAILABLE provider — subject to 2.4 — with the engine's existing
  floors unchanged: one writer, stage scoping, Gate/taint, budgets. Review-stage members on
  cheap models are *assistants*; the verdict is always the head's. **Studio appearance:**
  roster cards keep their per-member model chips and gain a provider badge + authority tag
  (`worker` vs `head`); `/api/studio` extends its provider `present` map to the full
  availability states. Per-project routing = the existing `settings_json["model_routes"]`
  layer — that's how one project gives its research team Qwen workers while another stays
  all-Anthropic.

### 2.4 Provider context policy (planning question 5 — B1 extended to models)

A model IS a context sink, so the check is harder than the service one (block, not
tool-drop):

- `ProviderSpec.private_ok = False` for every non-Anthropic provider. At the engine's
  member-composition seam (where B1 already runs `check_context_policy` for services): if a
  member's context bundle contains PRIVATE-provenance material and its route resolves to a
  `private_ok=False` provider, the run is **refused before fan-out** with a clear reason
  (`provider_refused_private_context`) — never a silent reroute.
- Per-provider nuance is deliberately flat this phase: Qwen/DeepSeek/GLM/Gemini are all
  `private_ok=False`, `project_non_private`-equivalent (repo code + non-private briefs OK).
  The tempting "Gemini may see Workspace data since Google already holds it" shortcut is
  REJECTED for 10C: Google-the-connector-custodian and Gemini-the-API-processor are
  different data flows. A `providers.allow_private` opt-in is a documented future design,
  gated on its own review.
- Interactive main loop, voice, memory, digest: untouched — they run `models.main`/utility
  routes, which stay Anthropic (2.3).

### 2.5 Pricing & cost attribution (planning question 6)

- `pricing.yaml` gains `deepseek:`, `qwen:`, `zai:`, `gemini:` sections under `models:`,
  **EXACT-match semantics** (already the non-Anthropic rule) — an unlisted model is unpriced
  ⇒ ledger records NULL (never $0) ⇒ orchestration estimate **blocks** the run
  (`treat_unpriced_as_blocking`, already shipped). Real numbers are filled at implementation
  from provider price pages, `effective` date bumped.
- Compat-endpoint usage extraction: anthropic_compat responses report Anthropic-shaped
  usage; cache fields may be absent/zero (fine — no cache_control is sent). The OpenAI-compat
  path keeps its explicit field mapping. **A provider whose responses omit usable usage
  fails loud** (empty-usage guard), because unmetered spend is untracked spend.
- Attribution: LedgeredClient already stamps provider/model/team/role/stage/project/mode —
  no schema change, **no migration** (v8 stands). Costs screen groupings work unchanged.
- Drift mitigations: per-run/monthly budget caps still bound absolute spend; a monthly
  pricing-review ritual is documented in the ADR; `effective` dates make staleness visible.

### 2.6 Env vars & config (planning question 7)

```bash
# .env — keys ONLY here (never settings.yaml, never code)
DEEPSEEK_API_KEY=...
DASHSCOPE_API_KEY=...     # Qwen (Alibaba DashScope)
ZAI_API_KEY=...           # GLM / Z.ai
GEMINI_API_KEY=...        # distinct from GOOGLE_CLIENT_ID/SECRET (Phase 9 connectors)!
```

`Secrets` gains the four fields; `_REQUIRED_KEYS` gains `deepseek`, `qwen`, `zai`, `gemini`
so `Config.require()` and the factory fail-closed paths name the right env var.

```yaml
# settings.yaml
providers:
  enabled: []               # fail-closed opt-in list, e.g. [deepseek, qwen, zai, gemini]
  base_urls: {}             # optional per-provider endpoint override
models:
  routes:                   # existing layer — cheap workers are just route overrides, e.g.:
    # researcher: {provider: qwen,     model: <pinned-at-T5>}
    # qa:         {provider: deepseek, model: deepseek-chat}
```

**Config-load regression guard:** `load_config()` must pass `providers=` through
`Config(...)` — with a YAML-roundtrip test, because the f086cc6 bug (services/budgets blocks
silently dropped) must never recur for the new block.

## 3. Task list (implementation order — planning question 2: pins BEFORE clients)

- **T1 — Plan + catalog + config plumbing (no clients).** Commit this doc. Add
  `models/providers.py` (ProviderSpec + PROVIDER_CATALOG incl. anthropic/openai rows);
  derive `PROVIDERS`; `ProvidersConfig` (enabled/base_urls) wired through `load_config`
  (**with the roundtrip regression test**); Secrets + `_REQUIRED_KEYS` entries; availability
  resolution helper (flag ∧ key ∧ priced). Tests: catalog invariants (every row fully
  classified; only anthropic is trusted_authority/private_ok), availability matrix, config
  roundtrip.
- **T2 — Authority + privacy pins (before any new client exists).**
  `TRUSTED_AUTHORITY_PROVIDERS` / `FINAL_AUTHORITY_ROLES` / `PRIVATE_CONTEXT_ROLES` in
  roles.py; `validate_route` enforces authority + availability; engine member seam refuses
  PRIVATE bundles to `private_ok=False` providers (B1-style, blocking). Adversarial-style
  unit pins: planner/judge/utility→deepseek raises at every override layer; per-run route
  injection cannot bypass; refusal reason surfaces in the run record.
- **T3 — Anthropic-compat client path.** `AnthropicClient` gains `base_url` + a
  capability-degradation profile (thinking off, no cache_control, no effort/output_config,
  exact model ids); factory builds per-provider compat clients from the catalog
  (cache key includes provider), fail-closed `require()`. Usage-empty guard. Tests with
  injected fake SDK clients: params NOT sent for compat providers, usage mapped, tool blocks
  round-trip, missing key ⇒ ConfigError, never a fallback client.
- **T4 — OpenAI-compat text-only path for Gemini.** `OpenAIChatClient` gains `base_url`;
  Gemini catalog row (`openai_compat`, `tool_capable=False`); registry keeps rejecting
  text-only routes on `coder`. Tests mirror the existing OpenAI ones + base_url wiring.
- **T5 — Pricing + Studio/read-model surfaces.** pricing.yaml rows for the four providers
  (real prices, `effective` bump, model ids pinned from live docs); `/api/studio` provider
  states (available/disabled/missing-key/unpriced — presence booleans ONLY); roster provider
  badge + authority tag; estimate demo math for a mixed-provider team. Secret sweep extended
  over the new read models. **No new mutation routes** (pin unchanged at 25).
- **⛔ CHECKPOINT P — mandatory stop before any live key is used.** Evidence, each with a
  named test: (i) `providers.enabled: []` ⇒ byte-identical routing to today (all-Anthropic
  defaults untouched); (ii) authority pins hold at every layer; (iii) private-bundle
  refusal fires; (iv) fail-closed matrix (flag/key/pricing) with correct UI reasons;
  (v) no secret text on any new surface; (vi) full suite + ruff green. Report, then continue.
- **T6 — Semgrep default fix (folded follow-up).** `semgrep_config` default `auto` → `p/ci`
  (a named registry pack works with `--metrics=off`; `auto` hard-errors — verified live
  during the 10B closeout). settings.yaml comment updated; local-rules-dir stays the
  fully-offline option; config test updated; `docs/verification-10B.md` note appended.
- **T7 — Adversarial pins + gate rerun prep.** `test_provider_safety.py`: the routing
  matrix end-to-end (cheap provider can never acquire planner/judge/utility; unpriced blocks
  fan-out; missing key blocks, never falls back; disabled provider invisible; PRIVATE bundle
  refusal; per-run override injection inert). No new live eval scenarios (routing is
  keyless-pinnable; avoids suite churn) — the chunked gate reruns in live verification
  because loop-adjacent code changed.
- **T8 — Docs.** ADR-0016 (provider integration: compat-first, authority tiers, provider
  privacy, pricing fail-closed); README provider section; `docs/verification-10C.md`
  (checklist below); learning-notes entries.

## 4. Non-negotiables (all test-pinned)

1. Fable/Opus remain head planner, architect, synthesizer, final reviewer, judge, and the
   high-risk decision layer: `planner`/`judge` routes MUST resolve to
   `TRUSTED_AUTHORITY_PROVIDERS` — no config/project/run layer can override; engine
   synthesis/verdict stay on the head client.
2. `utility` (private conversation content) stays on trusted providers this phase.
3. A PRIVATE-provenance bundle is never sent to a `private_ok=False` provider — refused
   before fan-out, visibly.
4. Availability is fail-closed at three layers (registry validation, factory require,
   pricing/estimate block); a missing key/flag/price NEVER downgrades to another
   provider/model silently.
5. Keys live in `.env` only; UI/API/logs/traces show presence booleans, never key text;
   secret sweep extended.
6. Ledger attribution (project/team/role/stage/mode/provider/model) on every call;
   unpriced ⇒ NULL cost ⇒ estimate blocks; no migration needed (v8 stands).
7. Existing contracts untouched: Gate, taint/egress, modes, context_policy, output_trust,
   budget reservation, turn lock, one-writer floor, service catalog, eval ritual, project
   isolation, mutation-route closed set, ADR-0002…0015.
8. `providers.enabled: []` default ⇒ byte-identical to pre-10C behavior.
9. `design/` untouched; `docs/PLAN.md` / `docs/PLAN-7-*` / `mcp_sample.json` /
   `config/settings.yaml` / `config/permissions.yaml` never committed.
10. Text-only routes stay rejected on tool-capable roles; a compat endpoint that fails
    fidelity checks ships text-only or not at all.

## 5. Test plan

- **Catalog invariants:** every ProviderSpec fully classified; exactly one
  `trusted_authority`/`private_ok` provider (anthropic); api_style ∈ the closed set;
  tool_capable ⇔ anthropic/anthropic_compat.
- **Routing matrix:** authority pins per role × per override layer; availability
  (flag/key/pricing 2×2×2) ⇒ correct RouteError/UI state; text-only × tool-capable.
- **Clients (keyless, injected fakes):** compat degradation profile (no thinking/cache/
  effort params on the wire), tool-block round-trip, usage mapping + empty-usage fail-loud,
  base_url wiring, retry passthrough, ConfigError on missing key with the right env-var name.
- **Engine:** PRIVATE-bundle refusal; mixed-provider team estimate math; unpriced member
  blocks fan-out; run records carry provider.
- **Config:** providers block YAML roundtrip (the f086cc6 regression class); env precedence.
- **Surfaces:** secret sweep over `/api/studio` + costs; mutation-route pin unchanged.
- **Eval:** no new scenarios; the chunked gate reruns at live verification (baseline
  ratchet NOT expected — investigate any regression, never re-ratchet to absorb one).

## 6. Live verification (`docs/verification-10C.md`; requires the user's keys)

Per provider, one at a time (add key to `.env`, add to `providers.enabled` locally — never
commit settings.yaml; revert after):

1. `/api/studio` shows the provider available; the other three show missing-key/disabled
   with reasons; no key text anywhere.
2. **Qwen:** research-team run with the researcher routed to Qwen — council output framed,
   synthesis stays Fable; ledger rows carry provider=qwen with non-NULL cost.
3. **DeepSeek:** backend-team `implement` on a scratch project with coder→deepseek
   (tool-capable via anthropic-compat) — tools drive correctly, one-writer floor + Gate
   approvals unchanged, head verdict is Fable.
4. **GLM:** long-horizon sub-agent worker (e.g. refactor_proposal council) — iterations
   bounded, report ingested as untrusted, verdict Fable.
5. **Gemini:** text-only synthesis/research role — confirm a tool-capable route to Gemini
   is REJECTED by validation.
6. **Fail-closed demos:** remove a pricing row ⇒ estimate blocks with `unpriced`; unset a
   key ⇒ ConfigError naming the env var; set `planner: {provider: deepseek…}` in a project
   override ⇒ RouteError surfaces in the Studio read model.
7. **Cost check:** Costs screen by-model/by-provider groupings match SQL sums; budget caps
   still stop a runaway fan-out.
8. **Chunked eval gate** (the 10B-verified commands): `eval run --suite core`,
   `--suite adversarial`, then `aggregate --report`. Green required; no ratchet expected.
9. Semgrep T6 check: fresh default config runs a real scan with `--metrics=off`.

## 7. Risks / tradeoffs

- **Compat-endpoint fidelity (top technical risk).** Anthropic-compat implementations vary
  (streaming event shapes, stop reasons, usage fields, tool-call edge cases). Mitigations:
  capability-degradation profile sends only the conservative core; fail-loud guards on
  empty content/usage; live fidelity checks per provider before enabling; the rule that a
  flaky provider ships text-only or not at all. Fallback (deferred): the OpenAI
  function-calling translation layer.
- **Provider privacy (top safety risk).** These are foreign-jurisdiction processors;
  prompts may be retained/trained on. Mitigations: `private_ok=False` + engine refusal;
  worker roles only see provenance-checked bundles; utility/main stay Anthropic; documented
  in ADR-0016 so the posture is a decision, not an accident. Residual risk: repo code IS
  sent to cheap providers when a user routes a coder/researcher there — that's the feature;
  the ADR states it plainly and per-project routing keeps sensitive projects all-Anthropic.
- **Pricing drift.** Manual pricing.yaml can go stale ⇒ wrong attribution (never
  unbounded spend — budgets cap absolutes; unpriced blocks). Mitigations: `effective`
  dates, monthly review ritual, exact-match fail-closed.
- **Authority creep.** The pressure to let a cheap model "just approve this once" is the
  slippery slope; the no-escape-hatch code constant + adversarial pins make weakening it a
  deliberate, reviewable code change.
- **Quality variance.** Cheap workers may produce worse drafts — acceptable by design: the
  head reviews everything, revise loops are bounded, and per-role `max_cost_usd`/budgets
  bound the waste. ROI surfaces (T17/10B) make the tradeoff measurable per team.
- **Dependency surface.** No new SDKs: anthropic SDK reused for compat; `openai` SDK is
  already a dependency path for the existing adapter. Gemini native SDK deferred keeps it
  that way.
