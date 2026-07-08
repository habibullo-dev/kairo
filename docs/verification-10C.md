# Phase 10C — live / local verification (direct providers)

Phase 10C adds Qwen / DeepSeek / GLM(Z.ai) / Gemini as scalable **worker** models behind
fail-closed flags. This checklist separates **what is verified keylessly** (Tasks 1–7, in the
test suite) from **what requires your provider keys** — per amendment A6, the second set is NOT
claimed done until you run it. Commands are **Windows PowerShell**, from the repo root.

Providers to live-test are limited to the keys currently available: **DeepSeek** and **Gemini**;
**Qwen** only if its pricing is filled (else it stays fail-closed/blocked); **Z.ai skipped** —
its provider console was unavailable at ship time (its missing-key/unavailable path is part of
the fail-closed proof below, not a blocker).

## 1. Keyless implementation — VERIFIED (no keys, in the suite)

- Full suite green: `uv run pytest -q` → **1321 passed, 1 skipped**; `ruff check` clean;
  migrations at v8 (10C added NO migration).
- Byte-identical default (`providers.enabled: []`): all ten roles resolve to their unchanged
  anthropic defaults (`test_provider_safety.py::test_opt_in_providers_disabled_by_default…`,
  `test_default_authority_routes_resolve_to_anthropic`).
- Authority pins at every layer: planner/judge/utility → anthropic only, settings/project/run
  (`test_every_worker_provider_rejected_on_every_authority_role_and_layer` — 36 cases);
  head + judge stay anthropic with cheap workers configured.
- Fail-closed availability (flag ∧ key ∧ priced): disabled / missing-key / unpriced routes raise
  with the reason; core providers stay routable (`test_route_to_*_fails_closed`).
- Provider privacy: a PRIVATE bundle bound for a `private_ok=False` provider is refused before
  fan-out (`test_engine_refuses_private_bundle_to_cheap_provider`).
- Compat client fidelity: degradation profile omits effort/thinking on the wire; tool blocks
  round-trip; usage mapped; empty/zero-usage fail loud; bearer vs x-api-key auth
  (`test_provider_clients.py`).
- No secret text on any new surface (`test_providers_status_states_and_secret_sweep`).
- Pricing: deepseek/zai/gemini priced (exact-match), qwen intentionally unpriced.

## 2. Z.ai (GLM) — fail-closed missing-key / unavailable proof (no live access)

Z.ai's console was unavailable, so there is no key. This is a *proof*, not a gap: Z.ai stays in
the catalog and is provably inert without a key.

```powershell
# No ZAI_API_KEY set. Studio must show zai as missing_credentials/disabled, and any route to it
# must fail closed with the reason. Keyless — run anytime:
uv run pytest tests/unit/test_provider_safety.py -k zai -q
```

Expect: `zai` state cycles `disabled → missing_credentials → unpriced → available` exactly as its
flag/key/pricing change; a worker route to `zai` without a key raises `RouteError` naming
`missing_credentials`; `zai` is worker-only (never trusted/private). When the Z.ai console is
back, add `ZAI_API_KEY` to `.env`, add `zai` to `providers.enabled`, and run the DeepSeek-style
worker check below.

## 3. Providers to LIVE-TEST (requires YOUR keys)

### 3.0 Add keys + enable locally (local-only; never commit)

```powershell
# .env — keys ONLY here (never settings.yaml, never code)
#   DEEPSEEK_API_KEY=...
#   GEMINI_API_KEY=...            # distinct from GOOGLE_CLIENT_ID/SECRET
#   DASHSCOPE_API_KEY=...         # Qwen — ONLY if you also fill Qwen pricing (else leave it out)
```

Add a `providers:` block to `config/settings.yaml` (a LOCAL edit — revert before any commit;
`config/settings.yaml` is never committed):

```yaml
providers:
  enabled: [deepseek, gemini]     # add qwen ONLY if you filled its pricing (§3.4)
  # base_urls: {}                 # optional per-provider endpoint override
```

Revert when done: `git checkout -- config/settings.yaml`.

### 3.1 Studio shows the right states

`uv run jarvis --ui` → Studio. `deepseek` + `gemini` show **available**; `zai` shows
**missing_credentials** (or disabled); `qwen` shows **unpriced** (unless you filled pricing);
external rows deferred. No key text anywhere (presence booleans + env-var names only).

### 3.2 DeepSeek — a real tool-capable worker (anthropic-compat)

On a **scratch** project, run a Backend-team `implement` with the coder routed to DeepSeek (a
per-project `settings_json["model_routes"]` override, or a settings route
`coder: {provider: deepseek, model: deepseek-v4-pro}`). Confirm: the worker drives tools
(the compat path round-trips tool blocks); the one-writer floor + Gate approvals are unchanged;
the **head synthesis/verdict stays Fable** (planner is anthropic-pinned); `model_calls` rows carry
`provider=deepseek` with a **non-NULL** cost; the Costs screen shows by-provider spend.

### 3.3 Gemini — text-only research/synthesis

Route a worker analysis role (e.g. `researcher: {provider: gemini, model: gemini-2.5-flash}`) and
run a research workflow. Confirm the council output is framed and synthesis stays Fable. Then
confirm a **tool-capable** route to Gemini is REJECTED:

```powershell
uv run python -c "from jarvis.models.registry import ModelRegistry; ModelRegistry({'coder':{'provider':'gemini','model':'gemini-2.5-flash'}}).route('coder')"
# expect: RouteError — 'coder' must drive tools; provider 'gemini' is text-only
```

### 3.4 Qwen — only if you fill pricing (else it stays fail-closed)

Qwen ships UNPRICED (no official price was verifiable). To use it, add real DashScope numbers to
`config/pricing.yaml` under `qwen:` and confirm the exact endpoint for your plan
(`providers.base_urls.qwen` if it differs from the pinned coding-intl endpoint). Until then,
leave it out — a Qwen route is correctly blocked as `unpriced`.

### 3.5 Fail-closed demos (each should refuse with a clear reason)

- Remove a pricing row for a model you routed → the estimate blocks the run (`unpriced`).
- Unset a key that a route needs → `ConfigError` naming the exact env var (no fallback).
- Put `planner: {provider: deepseek, …}` in a project override → `RouteError` (authority pin)
  surfaces in the Studio model-routes read model.

### 3.6 Cost attribution

Costs screen by-provider / by-model groupings match SQL sums; per-run/monthly budget caps still
stop a runaway fan-out.

### 3.7 Chunked eval gate (loop-adjacent code changed)

```powershell
uv run jarvis eval run --suite core --stage data/evals/_chunked-10c
uv run jarvis eval run --suite adversarial --stage data/evals/_chunked-10c
uv run jarvis eval aggregate --stage data/evals/_chunked-10c --report
```

Green required. **No baseline ratchet is expected** — 10C adds no eval scenarios and should not
move existing baselines. Investigate any regression; never re-ratchet to absorb one.

## 4. Providers PENDING (external account / pricing / provider issue)

- **Z.ai (GLM)** — built + cataloged; **live verification pending** because the provider console
  was unavailable (no key). Fail-closed proof is done (§2).
- **Qwen** — **pending pricing**: usable only once official DashScope pricing is filled in
  `pricing.yaml`; until then it is correctly `unpriced`/blocked.
- **Tool-capable Gemini + Gemini multimodal** — deferred to a future phase (needs a function-
  calling ↔ tool-block translation layer + fidelity tests).
