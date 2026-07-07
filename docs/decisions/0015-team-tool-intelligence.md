# ADR-0015: Team Tool Intelligence — a classified service catalog with derived enforcement

- **Status:** Accepted; implemented in Phase 10B (Tasks 11–18)
- **Date:** 2026-07-08
- **Context phase:** Phase 10B (Orchestration Studio — Team Tool Intelligence)

## Context

The Orchestration Studio (ADR-0014) runs *project teams* — Research, Frontend, Backend,
Security, QA, PM, Ops, Custom — where each member needs the right *tools and services*
(scanners, browsers, web search, converters, …). The failure mode to avoid is a giant
integration phase that bolts a dozen SaaS clients on with per-service safety code, each a place
to get the gate/taint/scope wrong. The decision is to make service enablement a **data +
derivation** exercise, not integration glue.

## Decision

### 1. The catalog is the safety model; enforcement is derived, never hand-set

Every candidate service is a `ServiceSpec` row in `SERVICE_CATALOG` (`services/catalog.py`) —
the Team Tool Intelligence Matrix as code. A row carries `egress` / `write` / `dangerous`,
`stages` (⊆ council/review/execution), `permission_default`, `credential_env`, `pricing`
(`fixed_zero | metered | unknown`), `context_policy` (B1), `output_trust` (B2), and `priority`
(`now | later | avoid`). A row is **documentation, not an enablement** — `later`/`avoid` rows
exist so the UI can render them "deferred".

The adapter tool (`ServiceTool`) DERIVES its gate/taint ClassVars from its spec in
`__init_subclass__`: `egress`, `write`, `dangerous`, `reads_private`
(= `context_policy is private_allowed_with_gate`), and `permission_default` all come from the
catalog row. There is no per-tool gate decision to get wrong.

### 2. Availability is fail-closed

A service is `AVAILABLE` (its tool may register, a roster may reference it) only when it is a
`now` service AND globally flag-enabled (`config.services.enabled`) AND (per-project) not
narrowed out AND its credentials are present AND (metered ⇒ it has a pricing entry). Anything
else is a specific non-available state the UI shows and the tool never registers — the model
never sees it. `services.enabled` is `[]` by default: nothing is live until a human lists it.

### 3. Three enablement points, all in the engine or the adapter

- **Stage + floor** — `OrchestrationEngine._member_scope` grants a member a service's tool only
  when the service is stage-appropriate AND within the member's floor. A read-only
  (council/review) member never receives a non-`READ_ONLY_SPAWNABLE` tool: the floor grows by
  EXACTLY `{semgrep_scan, gitleaks_scan}` (hardened read-only scanners), and no more. An
  egress/write/dangerous service is execution-stage authority, held only by the single writer.
- **context_policy (B1)** — the engine runs `check_context_policy` before granting a service
  and DROPS a service whose policy forbids the member's context bundle (a repo_code_only scanner
  is refused a PRIVATE-sourced bundle). The adapter re-enforces at its call-site (a scanner
  refuses a target that escapes the project root or lands on the sensitive floor).
- **output_trust (B2)** — the adapter frames its result before it re-enters any prompt: only a
  `trusted_local_scan` is unframed; scanner findings are `security_finding_untrusted` (a finding
  quotes code a hostile repo could have authored), so they are delimiter-framed untrusted.

### 4. The first three adapters are local, free, and hardened

Semgrep and Gitleaks are hardened-argv CLI wrappers (fixed argv, `shell=False`, pinned cwd =
the project root, hard timeout, scrubbed env, offline flags) — the RepoReader pattern. B4: the
sensitive-path floor (derived from `paths.py`) is passed as `--exclude` globs AND every
finding's path is re-checked with `is_sensitive_path` (a second belt); Gitleaks findings are
reduced to `file:line + rule id` ONLY, so the matched secret value is *structurally* absent.
Playwright-localhost is localhost-only + inspect-only (B3): the URL host must be loopback (the
non-egress guarantee) and the verb set is exactly `{navigate, screenshot, dom_inspect,
a11y_check, visual_diff}` — no click/type/submit/eval; it is execution-stage only, ASK-gated.

### 5. Cost + ledger

Every service invocation writes a metadata-only `service_calls` row (project / team / role /
stage / service / operation / units / est_cost), attributed from the `cost_context` the engine
set for the child. A `fixed_zero` local tool records a known `0.0`; an unpriced metered service
records NULL (fail-closed) — never a fabricated `0.0`. The worst-case reservation (Task 14)
blocks a run whose route or metered service is unpriced.

## Consequences

- Enabling a `later` service is a small follow-up (adapter + tests + flag), never a redesign;
  the safety model already classifies it.
- No MCP client and no second agent framework are introduced (ADR-0014 holds). `mcp`-kind rows
  stay deferred and the Hub stays honest about them.
- The `repo_code_only` / `local_only` policies were widened to tolerate `PROJECT_NON_PRIVATE`
  (a scan member sees its own non-private task brief); the guarantees that matter —
  `public_only` never receives private content, and nothing but the private source receives
  `PRIVATE` — are unchanged.
- Playwright is execution-stage only, so a QA team (no writer) cannot use it in 10B; a QA
  execution path is a documented follow-up. The conservative choice preserved the read-only
  floor rather than widening it for a convenience.

## Alternatives rejected

- **Per-service safety code.** Rejected: every service becomes a place to mis-set the gate.
  Deriving from the catalog makes the classification the single source of truth.
- **Playwright in the read-only floor.** It is inspect-only and non-egress, so it would be
  *safe* there — but the plan pins the council/review floor to grow by exactly the two scanners,
  and keeping Playwright execution-only means the floor is never widened, even structurally.
- **Enabling metered/external services now.** Deferred behind flags — the framework ships; the
  external adapters do not (amendment B5).
