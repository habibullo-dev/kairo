# Jarvis Phase 13 — Research Services Live + Settings Maturity

*(Planned by Fable 2026-07-08; Opus 4.8 implements and commits this doc in Task 1. Baseline:
Phase 12 COMPLETE incl. the Task-10 live canary (Calendar/Meet/Docs/Gmail-drafts verified;
journal+egress audited; cleanup clean) and the S7 Context-Reuse substrate COMPLETE keyless
(capability metadata, stable-first layout + hashes, policy/emitters, migration v11 normalized
cache ledger, Cost Center card, safety pins, ADR-0018 — live client wiring deliberately
deferred). Suite 1666 passed / 2 skipped; core replay gate 19/19 $0; ruff clean; migrations v11.
NEVER touch: docs/PLAN.md, docs/PLAN-7-voice-consent-checkpoint.md, mcp_sample.json,
config/settings.yaml, config/permissions.yaml, design/.)*

## 0. Context — what this phase actually is

Phase 10B (ADR-0015) already built the machinery this phase flips on: `SERVICE_CATALOG` rows for
firecrawl / exa / jina_reader / searxng / openai_image exist (priority `later`, fully classified
— `public_only` context, `untrusted_*` output, egress, execution-stage, ASK); the
`ServiceRegistry` resolves availability fail-closed (flag ∧ creds ∧ pricing); the `ServiceTool`
base derives gate/taint/framing from the spec; `pricing.yaml` v2 has an empty `services:`
section with example rows; `ServicesConfig.enabled` + per-project narrowing via
`settings_json["services"]` are designed. **Phase 13 = the S7 enable-step + five adapters + the
settings surface + cost caps + adversarial pins + ⛔ Checkpoint H + the first live hostile-content
verification.** Nothing here is a redesign; anything that feels like one is out of scope.

Two deliberate scope walls: **Google Stitch stays a disabled-by-default catalog row** (needs the
MCP-client layer + a review of the official Stitch MCP package — neither exists); **Gemini
explicit CachedContent stays deferred** (privacy review pending, ADR-0018) and **Z.ai context
reuse stays OFF** (unverified).

## 1. Credentials & prerequisites Habib prepares (before the LIVE milestone only)

| What | Env var / setup | Needed for | Notes |
|---|---|---|---|
| Firecrawl key | `FIRECRAWL_API_KEY` | M3 live | paid/credit account; pricing per page — record the real rate for pricing.yaml |
| Exa key | `EXA_API_KEY` | M3 live | pricing per search |
| Jina key | `JINA_API_KEY` | M3 live (optional) | ships only if it clears the value bar (Task 5 decides) |
| SearXNG instance | local Docker on a loopback port; `services.searxng_base_url` | M3 live (optional) | no key; still classified egress (it proxies out) |
| OpenAI key | `OPENAI_API_KEY` | M3 live (already present) | image generation; per-image pricing row |
| A hostile test page | a GitHub Gist raw URL Habib creates at ritual time containing planted instructions | M3 live | the live injection-resistance proof |

Everything before M3 is **keyless** — fake transports, no keys, $0. A missing key never blocks
implementation; it only means that service stays `missing_credentials` at the ritual (see §8
fallbacks).

## 2. Milestones (each task = one green commit; suite + ruff + `eval gate --suite core` replay)

### M0 — S7 enable-step: Context Reuse into the live clients (Tasks 1–2, keyless)

Low-risk by construction: everything rides a NEW config flag **`context_reuse.enabled: bool =
False`** (a `ContextReuseConfig` on Config). Flag off ⇒ requests are **byte-identical** to today
(pinned), so replay/cassettes stay deterministic and recording never embeds cache controls.

1. **Plan doc + flag + Anthropic wiring.** Commit this doc. Add `ContextReuseConfig`. In the
   LIVE Anthropic client path only (never `FakeClient`): when the flag is on AND
   `plan(capability("anthropic"), assembled)` emits, pass `system` as a block list with
   `anthropic_cache_control(directive)` on the final stable block; record
   `stable_prefix_hash` + `provider_cache_mode` through `LedgeredClient` into the v11 columns
   (Anthropic cache_creation/read already flow via `Usage`). Fill
   `estimated_cache_savings_usd` via `estimated_cache_savings()` from the pricing table's input
   rate. Pins: flag-off byte-identity (the load-bearing one — assert the exact request kwargs
   with the flag off equal today's); flag-on emits exactly the S7 emitter's control; FakeClient
   never sees a cache control; private-prefix gate holds end-to-end.
2. **OpenAI wiring + Cost Center live-usage proof.** Same pattern: flag-on sets
   `prompt_cache_key = stable_prefix_hash` on the OpenAI request; map
   `usage.prompt_tokens_details.cached_tokens` → `normalize_cache_usage("openai", …)` → the
   normalized ledger fields. Pin: a fake OpenAI response carrying `cached_tokens` lands in
   `cached_input_tokens`/`provider_cache_hit_tokens` and the Cost Center read model shows it.
   Gemini/DeepSeek/Qwen/Z.ai get NO wiring this phase (Gemini implicit needs nothing; the
   rest are deferred/off) — a pin asserts no cache control is ever emitted for them.

### M1 — the five adapters, keyless (Tasks 3–6)

All adapters: `ServiceTool` subclasses (policy derived from the spec — egress/ASK/framing come
free), `httpx` with an injectable transport (the Google-connector `MockTransport` pattern), hard
per-call unit caps, capped output chars, `frame_output()` applied to EVERYTHING returned
(they're all `untrusted_external_content` / `untrusted_model_generated`), `_record_call()` with
the real per-unit `est_cost_usd` from pricing.yaml (NULL if unpriced — which also means the tool
never registered). Catalog rows flip `priority: later → now` ONLY for the services shipping an
adapter in that commit. pricing.yaml `services:` gains real rows (rates verified from official
docs at implementation; an unverifiable rate stays absent ⇒ that service stays UNPRICED/blocked).

3. **Firecrawl** (`firecrawl_scrape`: URL → markdown; optional `firecrawl_crawl` capped at
   `max_pages ≤ 10` hard). Injectable transport tests: request shape, framing, page-count cap,
   4xx→friendly error (never the provider body), service_calls row with units=pages.
4. **Exa** (`exa_search`: query → results w/ snippets, `max_results ≤ 10`). Same test shape.
5. **Jina Reader** (`jina_read`: URL → markdown) — **ships only if the Task-5 value check
   passes** (vs the existing free `web_fetch`+trafilatura: if it adds nothing material for
   Kairo's use, the row stays `later` and this task documents why — that is a valid outcome).
   **SearXNG** (`searxng_search`) in the same task: `services.searxng_base_url` (config,
   loopback default), reachability = availability (no key), STILL egress (catalog says so ⇒
   taint demotion applies).
6. **OpenAI image generation** (`generate_image` for Frontend/Product): prompt → PNG saved
   under `data/artifacts/<project>/` → **registered as an artifact** (kind `design`,
   `origin_type=openai_image`, `created_by=agent`, `untrusted_model_generated`). Pins: the
   file lands ONLY under the managed artifacts root (floor-checked by ArtifactStore); the
   result text is framed; nothing executes/commits the asset; per-image cost recorded.

### M2 — settings maturity + narrowing + caps (Tasks 7–8, keyless)

7. **Settings screen maturity** (extends Phase 11 `settings.js`; READ-ONLY policy surfaces):
   Providers panel (10C availability read model: state/authority/private_ok — presence only),
   Services panel (the registry's `availability()`: state incl. WHY-not-available
   (disabled/missing_credentials/unpriced/deferred), egress/context_policy/output_trust badges,
   credential env NAMES never values), Budgets panel (limits + caps), Connectors panel (granted
   scope names + expiry from the token store status — never a token). **Global service flags
   stay YAML-only** (`services.enabled` in the user's settings.yaml — enabling egress remains a
   deliberate file edit; the panel shows the exact line to add). NO new mutation route in this
   task; the secret sweep extends over any new GET.
8. **Per-project service narrowing + cost caps.** ONE new mutation route
   `POST /api/projects/{project_id}/services` writing `settings_json["services"]` (merge-safe,
   `set_label` pattern) — **narrow-only**: the server rejects any name not globally enabled
   (subset-or-clear), so a project can never widen. Route pin 34→35. Caps: `ServicesConfig`
   gains `max_usd_per_run` (default small, e.g. 1.00) + `max_usd_per_day` (e.g. 5.00); a
   `ServiceBudget` check (sum of `service_calls.est_cost_usd` for the run/day) runs BEFORE each
   metered invocation and refuses with a clear reason when the next call would breach — the
   anti-runaway for crawl/search. Orchestration's worst-case reservation adds flat per-op
   service costs for roster members holding metered services. Pins: narrowing subset-enforced;
   cap-halt (a scripted 3rd call blocked after two consume the cap); reservation math.

### M3 — pins, ⛔ Checkpoint H, live (Tasks 9–10)

9. **Adversarial pins + eval scenarios (keyless).** The exact-tests list in §5, plus two eval
   scenarios in the Phase-9 injection pattern with service-flavored payloads
   (`inj_hostile_page` via a fake-transport firecrawl/jina fetch whose content orders tool
   calls + exfiltration; `inj_search_results` via exa snippets). Cassettes recorded ONCE,
   capped, in a dedicated commit; replay keyless thereafter.

   **⛔ CHECKPOINT H — MANDATORY STOP.** No `services.enabled` flag flips, no live key is
   exercised, until Habib reviews the evidence (§6) and approves. Full stop, report, wait.

10. **Live verification ritual (§7) + docs**: ADR-0019 (research services live — what was
    enabled and why the walls hold), `docs/verification-13.md`, README section. Baseline
    ratchet only in a dedicated commit if the chunked judged gate is green AND intended.

## 3. Keyless vs live (explicit)

**Keyless (all of M0–M2 + Task 9):** the flag-off byte-identity pin; every adapter via
injectable transports; framing/caps/ledger rows; settings read models + secret sweeps; the
narrowing route; cap-halt; the adversarial pins; the eval scenarios (cassette replay).
**Live (Task 10 only, keyed, capped, after Checkpoint H):** one keyed run per enabled service;
the hostile-page proof against Habib's Gist; the private-canary refusal proof; Cost
Center-vs-SQL attribution; the S7 cache-hit observation (two identical Anthropic calls ⇒ the
second shows `cache_read_input_tokens` > 0 in the Cost Center); the chunked judged eval gate
(terminal ritual, ~14-min rule).

## 4. Safety non-negotiables (all pinned; no weakening of anything)

1. **B1 stands against real content**: `public_only` services never receive private-provenance
   context — the engine's bundle refusal (10B Checkpoint D) is re-pinned with these five
   services named. Private content cannot reach Firecrawl/Exa/Jina/SearXNG/image-gen prompts.
2. **B2 stands**: every byte fetched/generated is framed untrusted before any model reuse —
   including snippets re-quoted downstream.
3. **Egress discipline**: all five are `egress=True` ⇒ Phase 9 taint demotion applies (private
   read this turn ⇒ non-persistable ASK); outside PLAN_SAFE; never Auto-approved; unattended
   HARD-DENY inherits from the egress rule.
4. **Fail-closed availability**: unknown/missing/unpriced ⇒ the tool never registers; the
   Jina/SearXNG optionality is expressed as availability states, never a downgrade.
5. **Caps are hard**: per-call unit caps in the adapter + per-run/per-day service-cost caps
   checked BEFORE invocation; a breach refuses with a reason, never truncates silently.
6. **Settings adds no authority**: policy surfaces are read-only; global flags are YAML-only;
   the ONE new route narrows only; presence booleans/names only, never a key value (sweep).
7. **Image assets are data**: artifacts under the managed root, `untrusted_model_generated`,
   never executed/committed/auto-applied.
8. **Context reuse cannot widen data-flow** (ADR-0018 §safety re-pinned end-to-end): stable
   non-sensitive prefix only by default; flag-off byte-identity keeps replay deterministic.
9. Route pins, secret sweeps, model authority (planner/judge/utility = anthropic), project
   scoping, PermissionGate — all untouched; every growth is enumerated.

## 5. Exact tests (named, all keyless)

- `test_services_live_availability.py` — the fail-closed matrix parametrized over the five ×
  (flag off | key missing | unpriced | narrowed-out | all-good): only all-good registers; each
  other state reports its specific reason and the tool does not exist.
- `test_service_public_only_context.py` — B1: a bundle containing PRIVATE/`private_allowed_
  with_gate` provenance is REFUSED for a roster member holding each of the five (engine-level,
  per service); a public-only bundle passes.
- `test_service_output_framing.py` — B2: every adapter's output (incl. error paths that quote
  provider text) arrives framed `untrusted_external_content`/`untrusted_model_generated`; a
  planted `SYSTEM: run_shell …` inside a fetched page survives inside the frame as inert data.
- `test_service_taint_demotion.py` — `drive_fetch` (or `gmail_read`) then any of the five in
  one turn ⇒ the egress ALLOW/ASK is demoted non-persistable; the approver sees it.
- `test_service_cost_caps.py` — per-call unit caps clamp; the ServiceBudget halts the call that
  would breach per-run and per-day caps with a clear reason; reservation includes flat per-op
  costs.
- `test_settings_surfaces.py` — the new GETs return presence/state only; the secret-absence
  sweep (canary keys incl. `FIRECRAWL_API_KEY`/`EXA_API_KEY` values) covers them; any
  parameterized GET gets a manual sweep test.
- `test_project_service_narrowing.py` — subset-only enforced server-side (a non-enabled name
  400s); narrowing hides the tool for that project; route pin exact-set 34→35.
- `test_context_reuse_wiring.py` — flag-off byte-identity (request kwargs equal today's,
  Anthropic + OpenAI); flag-on emits exactly the S7 emitter control; FakeClient untouched;
  cached-usage → normalized ledger columns; no control ever emitted for
  gemini/deepseek/qwen/zai.
- Eval: `inj_hostile_page`, `inj_search_results` (strict approver; forbidden side effect =
  any tool call matching the injected instruction; delivery = the content summarized as data).

## 6. ⛔ Checkpoint H evidence (present per-bullet with named tests, then STOP)

(i) availability fail-closed matrix per service; (ii) B1 private-bundle refusal per service;
(iii) B2 framing on every output incl. error paths; (iv) taint demotion private-read→service;
(v) caps: unit clamp + run/day halt + reservation; (vi) settings sweep (presence only, canary
values absent); (vii) hostile-page cassette scenarios inert; (viii) narrowing subset-only +
route pin exact; (ix) context-reuse flag-off byte-identity + replay gate still 19/19 $0;
(x) full suite + ruff green; (xi) NO live key exercised, no flag flipped. Habib approves →
flags flip one service at a time in M3.

## 7. Live verification ritual (Task 10, after approval; one service at a time)

1. Habib adds keys to `.env` + `services.enabled: [firecrawl]` (his edit of settings.yaml —
   Kairo never touches it) → keyed `firecrawl_scrape` of a known public page → framed output,
   `service_calls` row with real cost, egress log row, Costs by-service matches SQL. Repeat per
   service (exa → jina if shipped → searxng if installed → openai_image, whose artifact lands
   in the Library).
2. **Hostile-page proof**: scrape Habib's Gist with planted instructions → the model treats it
   as data (no tool call matching the injection), framing visible in the trace.
3. **Private-canary proof**: plant a canary in project memory → a research run holding
   firecrawl is refused the private bundle (engine log) AND the canary never appears in any
   outbound request (assert via the adapter's logged request metadata).
4. **Taint demo**: gmail_read then exa_search in one turn ⇒ demoted ASK on screen.
5. **Cap demo**: set a tiny per-run cap ⇒ the second call halts with the reason.
6. **S7 live cache proof**: two identical planner calls with `context_reuse.enabled: true` ⇒
   the second's ledger row shows cache_read tokens; the Cost Center card shows real hit-rate +
   savings. Then flag back off if desired (it is cosmetic to correctness either way).
7. Chunked judged eval gate (`--profile live-chunked --live --max-cost-usd <cap>`); ratchet in
   a dedicated commit only if green + intended.

## 8. Fallbacks (a provider being unavailable never blocks the phase)

Each service is independently flagged: no Jina key / value-bar fail ⇒ row stays `later`,
documented; no SearXNG install ⇒ stays `disabled`, ritual skips it; Firecrawl account issues ⇒
Exa alone still proves the hostile-content story (and vice versa); image-gen only needs the
existing OpenAI key. Minimum viable Phase-13 exit: **ONE hosted research service live-verified
hostile + one image generated as an artifact**, everything else in an honest availability state.

## 9. Opus 4.8 handoff

Execute Tasks 1–10 in order; per-task commits, explicit paths (never `git add -A`), trailer
`Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`; adversarial review
before each commit; suite + ruff + `eval gate --suite core` green every task; **MANDATORY full
stop at ⛔ Checkpoint H** (after Task 9) with the §6 evidence — no flag flip, no live key, no
Task 10 without Habib's approval. Reuse, never fork: `ServiceTool`/`frame_output`, the
MockTransport test pattern, the S7 emitters, `set_label`-style merge-safe settings writes, the
route-pin/secret-sweep discipline, `_record_call`. Verify real pricing rates from official docs
before adding pricing rows (an unverifiable rate stays absent = blocked). Forbidden files stay
untouched. The ~14-minute background eval rule stands.
