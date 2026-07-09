# ADR-0023: Cost-Aware Auto Routing (Phase 15.6)

*Status: accepted (Phase 15.6, 2026-07-10). The interactive default becomes cost-aware AUTO routing
instead of a fixed premium model: a cheap Gemini 2.5 Flash-Lite classifier reads each message and a
pure policy layer picks the model — cheap tiers for simple work, trusted models for judgment/private
and deep work. It widens the `private_ok` provider set (Habib-approved) but changes NO other safety
boundary: the classifier is an optimization, never the security boundary. ⛔ A mandatory checkpoint
(after Task 7) gates on the evidence table before Phase 16.*

## Context

Phase 15.5 shipped a manual, Anthropic-only interactive model selector. Habib asked for a cost-aware
default: most daily work is cheap; only judgment-heavy, private, or deep work should reach premium
models. Two facts shaped the design:

- **The main chat carries private context** (memory, history, project state) on every turn, so any
  model it routes to receives private content.
- **Gemini/OpenAI are text-only in this codebase** (the OpenAI-compat adapter raises on tools), but
  the interactive loop always registers tools.

## Decision

- **Two routing modes, separate from the permission Mode.** `RoutingState` is AUTO | MANUAL —
  orthogonal to plan/approval/auto (which stays the permission axis, untouched). AUTO is the default
  daily experience; MANUAL pins a trusted, tool-capable Claude model.
- **`private_ok` widened to `{anthropic, gemini, openai}`** (was anthropic-only). `trusted_authority`
  stays anthropic-only, so `validate_route` still bars gemini/openai from planner/judge/utility, and
  the orchestration engine's private-bundle refusal simply admits the two new private_ok providers.
  `qwen/deepseek/zai` stay `private_ok=False` — non-sensitive workers only.
- **The classifier is Gemini 2.5 Flash-Lite, forced-JSON, fail-safe.** It returns
  intent/difficulty/sensitivity/category/needs_tools. It is a ROUTER, not an assistant (it ignores
  injected instructions). ANY failure or partial/unknown output coerces to the SAFE extreme
  (private/hard/needs_tools) ⇒ escalation to a trusted, tool-capable model — never a downgrade.
- **The pure policy layer enforces safety, not the classifier.** `resolve_route`:
  - Simple + tool-free → **Gemini 2.5 Flash** (cheapest; loop sends NO tools that turn).
  - Simple + needs a tool → **Claude Haiku 4.5** (cheap, tool-capable) — Gemini can't drive tools.
  - Judgment / private / important (personal/private, email/calendar/finance, hard, coding) →
    **Claude Sonnet 5**.
  - Deep / high-risk (expert) → **Claude Opus 4.8**; deep planning → **Fable 5**. Opus/Fable are
    never the simple-chat default.
  - **private_ok hard gate**: every Auto tier is private_ok by construction (catalog is the source of
    truth); a belt re-checks it. **Fail-closed availability**: an unavailable tier downgrades to a
    cheap tool-capable private_ok model, ultimately the trusted SAFE default (Sonnet) — never a cheap
    or non-private provider.
- **Per-turn client switching.** The loop gains an optional `router` + `client_selector`: when set
  (UI only), it applies the RouteDecision for the whole turn and uses the **ledgered client for the
  routed provider** (Anthropic vs Gemini). A text-only routed provider gets NO tools that turn. With
  NO router (REPL / sub-agents / evals) the loop is byte-identical (`self.client` + `models.main`).
- **Ledger `routing_mode` (migration v14).** Every `model_calls` row records how the model was chosen
  — `auto` | `manual` | NULL (no router). Attribution stays project/team/role/stage/mode/provider/
  model/cost.
- **UI: Auto is the default + recommended.** The composer leads with "Auto — recommended" ("uses
  cheap models first, escalates only when needed"), shows the live pick ("→ Sonnet 5"), keeps Manual
  Claude pins, and lists other providers DISABLED with honest reasons (text-only / not-allowed-for-
  private / unavailable). Manual per-model effort is disabled under Auto (Auto manages it).

## Consequences

- Cheap by default; premium only when the message warrants it. Gemini serves genuinely tool-free
  simple turns; Haiku covers cheap tool-using turns; Sonnet/Opus/Fable carry judgment/deep work.
- Non-private workers (Qwen/DeepSeek/GLM) are NEVER interactive-chat targets — they remain scoped
  delegation routes, where the orchestration context-policy block is the belt.
- Fully delivering "Gemini Flash for tool-using simple work" would need a Gemini function-calling
  adapter — deferred (a possible Phase 15.7); until then tool-using simple work uses Haiku.
- The classifier adds one cheap Flash-Lite call per turn; if Gemini is unavailable, AUTO degrades to
  the trusted SAFE default (Sonnet) for every turn (fail-closed), still correct, just not optimized.

## Alternatives considered

- **Haiku as the only cheap tier** (no Gemini for chat): simplest, but forgoes Gemini's cost edge on
  tool-free turns. Rejected in favor of the Gemini-tool-free + Haiku-tooled split (Habib's choice).
- **Classifier as the privacy boundary**: rejected — a cheap model must never be the security gate.
  Privacy is enforced in pure code (private_ok) + the existing context-policy block.

## Safety non-negotiables (all test-pinned)

1. `trusted_authority` stays anthropic-only; planner/judge/utility never move.
2. Private content never resolves to a non-`private_ok` provider (qwen/deepseek/zai).
3. Classifier failure/uncertainty escalates to a trusted, tool-capable model (fail-safe).
4. Unavailable/disabled/unpriced providers fail closed (no silent downgrade to cheap/non-private).
5. No router ⇒ REPL/sub-agents/evals byte-identical.
6. Gate, taint, context_policy, provider availability, pricing fail-closed, and the cost ledger are
   unchanged; the ledger gains `routing_mode` attribution only.
7. No secret crosses any GET (the routing surfaces are presence/model-name/reason only).
