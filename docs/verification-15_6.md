# Phase 15.6 — Cost-Aware Routing: verification

*Companion to ADR-0023. The keyless evidence is below; the live ritual (real Gemini + a real
tokened session) is a manual step at the ⛔ mandatory checkpoint before Phase 16.*

## Keyless evidence (all green in CI-equivalent local runs)

| Claim | Where it's pinned |
|---|---|
| **Auto is the default daily selection** | `RoutingState()` defaults AUTO; `test_routing_ui.test_get_models_reflects_auto_default`; `interactive_models` returns `policy=auto`, `auto.recommended` |
| **Gemini Flash-Lite is the Auto router/classifier** | `build_ui_app` builds `Classifier(gemini-2.5-flash-lite)` when gemini is route_allowed; `test_routing` classifier tests |
| **Gemini Flash is the cheap tool-free simple worker** | `choose_tier` → SIMPLE (gemini-2.5-flash) when `needs_tools=False`; `test_routing_dispatch.test_auto_toolfree_simple_dispatches_to_gemini_with_tools_off` (asserts `tools==[]`) |
| **Haiku handles cheap tool-needing simple turns** | `choose_tier` → SIMPLE_TOOLED (Haiku) when `needs_tools`; `test_routing_dispatch.test_auto_simple_needing_tools_dispatches_to_haiku_with_tools` |
| **Sonnet 5 is escalation/final judgment, not every-message chat** | `choose_tier` → JUDGMENT for sensitive/email/calendar/finance/hard/coding; simple non-sensitive never reaches Sonnet |
| **Opus/Fable are not normal-chat defaults** | `test_routing.test_opus_fable_are_not_the_simple_default`; only expert→Opus, hard-planning→Fable |
| **Fail-closed: missing key / disabled / unpriced provider** | `ProviderRegistry.state` (unchanged); Gemini-unavailable → Haiku/Sonnet in `resolve_route`; classifier None → SAFE default; `test_routing.test_auto_router_unavailable_falls_back_to_safe_default` |
| **Private context blocked from Qwen/DeepSeek/Z.ai** | they are not Auto tiers + `private_ok=False`; `test_provider_safety.test_engine_refuses_private_bundle_to_cheap_provider`; `test_routing.test_private_content_never_lands_on_a_non_private_provider` |
| **Classifier failure/uncertainty → trusted private_ok model** | `FAILSAFE` (private/hard/needs_tools); `coerce_classification` safe defaults; `test_routing.test_auto_failsafe_*`, `test_routing_dispatch.test_auto_classifier_failure_escalates_to_sonnet` |
| **Ledger records provider/model/routing_mode** | migration v14 `model_calls.routing_mode`; `CostContext.mode`; `test_cost_ledger.test_records_routing_mode` |
| **UI selector shows enabled/disabled reasons consistently** | `test_routing_ui.test_external_reasons_distinguish_textonly_from_nonprivate` |
| **No secret leaks on any GET** | `test_routing_ui.test_no_secret_on_models_route` + the Phase-15.5 secret-absence sweep over all GETs |
| **No router ⇒ REPL/sub-agents/evals byte-identical** | `test_routing_dispatch.test_no_router_is_byte_identical`; the eval core gate (opus/sonnet) still 19/19 $0 |

Gates: full unit suite green; `jarvis eval gate --suite core` 19/19 $0 (replay, decoupled from the
daily model); `ruff check` clean. Migrations at v14. Mutation-route closed set unchanged.

## Live ritual (manual, at the checkpoint — needs a real GEMINI_API_KEY + a tokened session)

1. `providers.enabled` includes `gemini`; `jarvis --ui`. Composer shows **Auto — recommended** as the
   default; the effort control reads "Auto-managed".
2. Ask a tool-free simple thing ("explain X in one line") → the "→" caption shows **Gemini 2.5
   Flash**; the trace shows `route_selected tier=simple`, and the turn ran with NO tools.
3. Ask a cheap tool thing ("what's on my calendar tomorrow") → routes to **Haiku** (tools present).
4. Ask a private/judgment thing ("draft a reply to my boss") → routes to **Sonnet 5**.
5. Ask a deep thing ("plan the architecture for …") → **Opus 4.8** / **Fable 5** (planning).
6. Pin a manual model → composer leaves Auto; pin **Auto** again → back to cost-aware routing.
7. Costs screen: rows carry `routing_mode` (auto|manual) + the right provider/model; a Gemini turn is
   attributed to `gemini`.
8. Disable Gemini (drop the key) → Auto still works, degraded to Sonnet (fail-closed), reason shown.
9. Confirm Qwen/DeepSeek/GLM are never the main-chat model and appear disabled with "not allowed for
   private context".
