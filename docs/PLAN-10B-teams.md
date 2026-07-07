# Jarvis Phase 10B (revised) — Orchestration Studio with Team Tool Intelligence

*(To be committed as `docs/PLAN-10B-teams.md` in Task 10. Supersedes the 10B half of
`docs/PLAN-10-workspaces.md`; the 10A half is shipped and untouched. Baseline: 10A complete
at `91bf812` (Tasks 1–9.5 + ADRs 0011–0014), 1139 tests green, ruff clean, migrations at v7,
Checkpoint C approved. Pre-condition: the pre-10B chunked eval gate aggregates GREEN — do not
start Task 10 on a red baseline. NEVER commit `docs/PLAN.md` or
`docs/PLAN-7-voice-consent-checkpoint.md`.)*

## Context — what this revision changes

The approved 10B built roles → workflows → engine → Studio. This revision adds **Team Tool
Intelligence**: the Studio is not just "roles on models" but **project-specific teams**
(Research, Frontend/UX, Backend/Data, Security, QA/Eval, Product/PM, Ops/Cost, Custom), where
each team member has the *right tools and services* — classified, feature-flagged, priced,
project-scoped, mode-aware, and gated exactly like everything else in Kairo.

The design rule that keeps this from becoming a giant integration phase: **the framework ships
now; services ship as catalog entries.** The Team Tool Intelligence Matrix classifies ~30
candidate services, but only three cheap, local, keyless-testable adapters are *enabled* in
10B (Semgrep, Gitleaks, Playwright-localhost). Everything else is a `SERVICE_CATALOG` row
with a documented adapter strategy behind a feature flag that defaults OFF. A service
appearing in the matrix does NOT mean it is enabled.

**Approval amendments (2026-07-07, binding):**
- **B1 — `context_policy` on every ServiceSpec** (what context a service may RECEIVE):
  `public_only | project_non_private | repo_code_only | local_only |
  private_allowed_with_gate | never_private`. External research tools (Firecrawl/Tavily/Exa/
  Jina/SearXNG) are `public_only` — they must never receive private project memory, Gmail
  content, Drive content, or secrets unless a future explicit policy/gate allows it. Enforced
  in the engine's context assembly (a bundle containing private-sourced material is refused
  for a `public_only`/`repo_code_only` service's role), not just documented.
- **B2 — `output_trust` on every ServiceSpec** (how a service's OUTPUT is classified):
  `trusted_local_scan | untrusted_external_content | untrusted_model_generated |
  security_finding_untrusted | derived_summary`. Everything except `trusted_local_scan` is
  wrapped in untrusted framing before any model sees it — and scanner findings are
  `security_finding_untrusted` (framed too: a hostile repo can plant instructions inside
  code that a finding quotes).
- **B3 — Playwright-localhost is inspect/QA-first**: screenshots, DOM inspection,
  accessibility checks, visual diff against localhost ONLY. NO arbitrary clicking, form
  submission, or generic browser actions — interaction is a separately planned/gated future
  step. It must not become a generic Browser MCP.
- **B4 — Scanners respect the sensitive-path floors**: Semgrep/Gitleaks scan selected project
  repo roots but exclude `.env`, `data/connectors/`, token stores, and every Kairo
  sensitive-path pattern (the adapter passes exclusions derived from `paths.py`, and output
  is filtered against the floor as a second belt). Gitleaks findings stay redacted to
  file:line + rule id — never a matched secret value.
- **B5 — NotebookLM Enterprise and generic Browser MCP stay avoid/deferred** — not integrated
  in 10B under any flag.
- **B6 — Checkpoint D is mandatory** with the expanded evidence list (see Task 15) before any
  of the three adapters is enabled.

Everything inherits 10A/Phase-6/Phase-9 substrate unchanged: `SubAgentService.spawn` (ADR-0014
— no second agent framework), PermissionGate + taint/egress, modes (PLAN_SAFE allowlist,
Auto at the approver), project scoping in SQL, ModelRegistry/ClientFactory fail-closed,
LedgeredClient + budgets.

## 1. Revised architecture (new pieces in bold)

```
src/jarvis/
├── orchestration/                 # 10B core (as approved, now team-aware)
│   ├── roles.py                   #   RosterRole + READ_ONLY_SPAWNABLE floor (no shell/write/web)
│   ├── teams.py                   # NEW — TeamProfile code constants (8 teams): members
│   │                              #   (route role, title, tools, services, capability, output,
│   │                              #   max_cost), team budget, default workflows, icon/color.
│   │                              #   Project overrides in settings_json["teams"], validated
│   │                              #   against the same invariants (never silently widened).
│   ├── workflows.py               #   10 templates + team_default_workflows mapping
│   ├── context.py                 #   ContextSelector → framed ContextBundle + bodies-free manifest
│   ├── engine.py                  #   stages A–E on spawn(); scopes = role.tools ∩ stage floor,
│   │                              #   services resolved through the ServiceRegistry per
│   │                              #   (project, team, role, mode, stage)
│   └── store.py                   #   OrchestrationStore over orchestration_runs
├── services/                      # NEW — Team Tool Intelligence
│   ├── registry.py                #   ServiceRegistry: SERVICE_CATALOG (code constants carrying
│   │                              #   the matrix classification) + feature flags + credential
│   │                              #   presence + pricing presence → availability. Fail closed.
│   ├── catalog.py                 #   the SERVICE_CATALOG rows (ServiceSpec dataclass)
│   ├── semgrep.py                 #   NEW adapter: hardened CLI (fixed argv, --offline/local
│   │                              #   rules, no shell) → semgrep_scan tool (RO, non-egress;
│   │                              #   B4: excludes Kairo sensitive paths, output floor-filtered)
│   ├── gitleaks.py                #   NEW adapter: hardened CLI → gitleaks_scan tool (RO,
│   │                              #   non-egress; B4: sensitive-path exclusions; findings
│   │                              #   redacted to file:line + rule id, NEVER the secret value)
│   └── playwright_local.py        #   NEW adapter: localhost-ONLY, INSPECT-ONLY (B3):
│                                  #   screenshot / DOM inspect / a11y check / visual diff.
│                                  #   NO click/type/submit — interaction is a future gated
│                                  #   step. Allowlist + verb set enforced in the adapter.
├── persistence/migrations.py      # v8 (additive, plain SQL): model_calls += team, stage;
│                                  #   new service_calls table (metadata-only service ledger)
├── observability/ledger.py        #   CostContext += team, stage; ServiceLedger (service_calls)
├── config.py                      #   ServicesConfig: enabled: [] (global opt-in flags);
│                                  #   per-project narrowing via settings_json["services"]
├── ui/                            #   studio.js (workflow | team | custom selection; service
│                                  #   availability chips: available / disabled / missing
│                                  #   credentials / deferred), Hub services panel (presence only)
config/pricing.yaml                #   schema v2: adds services: {name: {unit, usd_per_unit}}
docs/PLAN-10B-teams.md, decisions/0015-team-tool-intelligence.md
```

## 2. Team Profiles (code constants; roles reference the existing ModelRegistry routes)

Every member: `(title, route_role, tools ⊆ floors, services ⊆ catalog, capability, output,
max_cost_usd)`. Capability ∈ read_only | review_only | write_capable. **≤1 write_capable
member per team, and it is only ever activated in the Execution stage.** Fable (`planner`
route → `claude-fable-5`) remains the head synthesizer/final reviewer on every workflow —
synthesis/verdict are engine stages, not team members.

| Team | Members (route) | Services (now) | Services (later, flagged) | Default workflows |
|---|---|---|---|---|
| **Research** | Lead Researcher (researcher, +web), Analyst (utility), Archivist (docs) | tavily web_search, web_fetch, KB query/ingest, MarkItDown/Docling (already native) | Firecrawl, Exa, Jina Reader, SearXNG, Obsidian-MCP | research, council_review |
| **Frontend/UX** | UX Lead (ux), Implementer (coder, WRITER), Visual QA (qa) | playwright-localhost + screenshot/visual diff | Figma MCP, Browserbase/Stagehand, OpenAI image gen, Browser MCP | ux_critique, implement, review_diff |
| **Backend/Data** | Architect (reviewer), Implementer (coder, WRITER), Data Analyst (utility) | filesystem/git-read (native), RepoReader | GitHub MCP, Docker MCP, Supabase/Neon MCP, sqlite-RO tool | implement, review_diff, refactor_proposal |
| **Security** | Security Lead (security), Scanner (utility), Red-team Analyst (security) | semgrep_scan, gitleaks_scan, OWASP-LLM-Top10 (KB doc), adversarial-eval *results* (read) | CodeQL, Promptfoo red-team | security_review, review_diff |
| **QA/Eval** | QA Lead (qa), Eval Reader (utility), UI Tester (qa) | eval history/baselines READ (lab read model), playwright-localhost | Promptfoo, LangSmith (avoid for now), BrowserStack | debug_eval, review_diff |
| **Product/PM** | PM Lead (docs), Spec Writer (docs), Researcher (researcher) | drive_search/drive_fetch (Phase 9, read), KB/vault write (write_wiki_page, ASK) | GitHub issues (gh RO), Linear/Atlassian | plan_feature, release_notes |
| **Ops/Cost** | Ops Analyst (utility), Release Notes (docs) | cost ledger + pricing + ROI read models | Docker, GitHub Actions | release_notes, debug_eval |
| **Custom** | user-defined in settings_json["teams"] | any *enabled* service | — | any |

Pins: eval-gate **running** stays a terminal ritual (ADR-0005) — the QA team reads
`history.jsonl`/baselines, never runs gates. Gmail stays drafts-only; Drive/Calendar writes
stay Phase 9B. The Research lead may hold web egress **or** cross-source private context,
never both in one member (ADR-0014 §2).

## 3. Team Tool Intelligence Matrix

Legend — Class: RO=read-only, EG=egress, WR=write-capable, DG=dangerous. Modes: C=Council,
R=Review, E=Execution (stage availability ceiling; PermissionGate/taint still apply within).
Adapter: native | mcp | cli | browser | ritual (human-only). Priority: **now** / later / avoid.

Every row additionally carries (in `catalog.py`, defaulted by class and overridable per spec):
**context_policy** (B1) — all hosted EG research/web tools = `public_only`; local scanners =
`repo_code_only`; converters/local tools = `local_only`; Phase 9 connector reads =
`private_allowed_with_gate` (they ARE the private source); and **output_trust** (B2) — hosted
fetch/search = `untrusted_external_content`; scanners = `security_finding_untrusted`; image/LLM
generators = `untrusted_model_generated`; local deterministic reads = `trusted_local_scan`.

### Research
| Tool | Use | Host | Creds | Cost | Sens | Class | C/R/E | Gate | Taint | Adapter | Priority |
|---|---|---|---|---|---|---|---|---|---|---|---|
| Tavily (web_search) | web search | hosted | key (have) | metered | low | EG | E only¹ | ASK | egress-demotable | native (done) | **now** |
| web_fetch | page fetch | hosted | – | free | low | EG | E only¹ | ASK | egress-demotable | native (done) | **now** |
| MarkItDown | doc→md convert | local | – | free | med | RO | C/R/E | ALLOW | – | native (done) | **now** |
| Docling | pdf→md convert | local | – | free | med | RO | C/R/E | ALLOW | – | native (done) | **now** |
| Firecrawl | site crawl→md | hosted | key | metered/credit | med (URLs leak) | EG | E only¹ | ASK | egress | native | later |
| Exa | semantic search | hosted | key | metered | low | EG | E only¹ | ASK | egress | native | later |
| Jina Reader | page→md | hosted | key/free | free tier drift | med | EG | E only¹ | ASK | egress | native | later (thin value over web_fetch) |
| SearXNG | meta-search | local Docker (proxies out) | – | free | low | EG² | E only¹ | ASK | egress | cli/native | later |
| Obsidian REST/MCP | vault r/w | local | local token | free | high | RO/WR | R (RO) / E (WR) | ASK | – | mcp | later (native wiki covers now) |
| NotebookLM Ent. | corpus QA | hosted ent. | org auth | opaque | high | EG | – | – | – | – | **avoid** (future only, per user) |

¹ Egress tools are excluded from Council/Review by the READ_ONLY floor; a *researcher-role
Execution-stage* (or the dedicated `research` workflow's lead, which then carries **no**
cross-source private context) is where web tools live. ² Local install, but it proxies
queries to public engines ⇒ still egress.

### Frontend / UX
| Tool | Use | Host | Creds | Cost | Sens | Class | C/R/E | Gate | Taint | Adapter | Priority |
|---|---|---|---|---|---|---|---|---|---|---|---|
| Playwright (localhost, inspect-only) | screenshot / DOM / a11y / visual QA of local dev app | local | – | free | med | RO³ | R/E | ASK | non-egress (allowlist) | native | **now** |
| Screenshot/visual diff | pixel diff vs baseline | local | – | free | low | RO | C/R/E | ALLOW | – | native (with above) | **now** |
| Playwright (general web) | browse any site | local | – | free | high | EG+DG | E only | ASK | egress | native | later |
| Figma MCP | read designs | hosted | OAuth | free/seat | med | RO+EG | R/E | ASK | egress | mcp | later |
| OpenAI image gen | mockups/assets | hosted | key (have) | metered | low (prompts leak) | EG | E | ASK | egress | native | later |
| Browserbase/Stagehand | cloud browser | hosted | key | metered | high | EG+DG | E | ASK | egress | native | later |
| Browser MCP | generic browser | local/hosted | varies | varies | high | EG+DG | – | – | – | mcp | avoid until MCP layer exists |

³ Localhost-only AND inspect-only (B3), both enforced **in the adapter**: URL allowlist
(`127.0.0.1/localhost` + the project's configured dev ports) makes it non-egress *by
construction*, and the exposed verbs are exactly {navigate, screenshot, dom_inspect,
a11y_check, visual_diff} — no click/type/submit/eval. Arbitrary interaction is a separately
planned, separately gated future step; this must not become a generic Browser MCP.

### Backend / Data
| Tool | Use | Host | Creds | Cost | Sens | Class | C/R/E | Gate | Taint | Adapter | Priority |
|---|---|---|---|---|---|---|---|---|---|---|---|
| Filesystem/Git read | code context | local | – | free | med | RO | C/R/E | ALLOW | – | native (done) | **now** |
| write_file/run_shell | implementation | local | – | free | high | WR/DG | E only | ASK (NEVER_GRANTABLE) | – | native (done) | **now** |
| RepoReader | branch/commits/dirty | local | – | free | low | RO | C/R/E | ALLOW | – | native (done) | **now** |
| sqlite read-only tool | inspect local DBs | local | – | free | high | RO | R/E | ASK | – | native | later |
| GitHub MCP / gh CLI | issues/PRs | hosted | PAT | free | med | RO first; WR=DG | R (RO) / E | ASK | egress | cli (RO) → mcp | later |
| Docker MCP Toolkit | containers | local daemon | – | free | high | DG | E only | ASK | – | mcp/cli | later |
| Supabase/Neon MCP | cloud DBs | hosted | key | metered | **high** | EG+WR+DG | E only | ASK | egress | mcp | later |

### Security
| Tool | Use | Host | Creds | Cost | Sens | Class | C/R/E | Gate | Taint | Adapter | Priority |
|---|---|---|---|---|---|---|---|---|---|---|---|
| Semgrep | SAST scan | local CLI | – | free (local rules) | med | RO⁴ | C/R/E | ALLOW | – | cli (hardened argv, --offline) | **now** |
| Gitleaks | secret scan | local CLI | – | free | **high**⁵ | RO⁴ | C/R/E | ALLOW | – | cli (hardened argv) | **now** |
| OWASP LLM Top 10 | review checklist | local KB doc | – | free | low | RO | C/R/E | ALLOW | – | ritual (ingest once) | **now** |
| Adversarial eval results | posture evidence | local files | – | free | low | RO | C/R/E | ALLOW | – | native (lab read model) | **now** |
| CodeQL | deep SAST | local, heavy | license constraints | free-ish | med | RO | R | ASK | – | cli | later |
| Promptfoo red-team | LLM attack suites | local CLI, calls LLMs | provider keys | LLM spend | med | EG (LLM calls) | E | ASK | egress | cli | later |

⁴ CLI adapters run a fixed binary with fixed argv (the hardened-RepoReader pattern: no shell,
no model-supplied flags, pinned cwd, timeout) — classified RO because the adapter can only
read + report. They are **added to READ_ONLY_SPAWNABLE** so Security council/review members
can hold them. ⁵ Gitleaks output is redacted in the adapter: file:line + rule id + entropy,
**never the matched secret value** — a finding must not become the leak.

### QA / Eval
| Tool | Use | Host | Creds | Cost | Sens | Class | C/R/E | Gate | Taint | Adapter | Priority |
|---|---|---|---|---|---|---|---|---|---|---|---|
| Eval history/baselines (read) | freshness/regressions | local | – | free | low | RO | C/R/E | ALLOW | – | native (lab) | **now** |
| Eval gate (run) | – | – | – | LLM spend | – | – | – | – | – | **ritual only** (ADR-0005) | **now** (as ritual) |
| Playwright-localhost | UI assertions | local | – | free | med | RO-ish³ | R/E | ASK | – | native (shared) | **now** |
| Promptfoo | prompt regression | local, calls LLMs | keys | LLM spend | med | EG | E | ASK | egress | cli | later |
| LangSmith | tracing SaaS | hosted | key | seat/metered | **high** (sends traces off-box) | EG | – | – | – | – | **avoid** for now (content egress) |
| BrowserStack | cross-browser | hosted | key | seat | med | EG+DG | E | ASK | egress | native | later |

### Product / PM
| Tool | Use | Host | Creds | Cost | Sens | Class | C/R/E | Gate | Taint | Adapter | Priority |
|---|---|---|---|---|---|---|---|---|---|---|---|
| drive_search/fetch | read docs | hosted | OAuth (done) | free | high | RO (reads_private) | C/R/E | ALLOW (taints turn) | taints | native (done) | **now** |
| write_wiki_page / vault | specs, PRDs | local | – | free | med | WR | E | ASK | – | native (done) | **now** |
| Drive/Docs WRITE | author docs | hosted | OAuth scopes | free | high | EG+WR | – | – | – | – | **deferred to Phase 9B** (pinned) |
| GitHub issues (gh RO) | backlog context | hosted | PAT | free | med | RO+EG | R/E | ASK | egress | cli | later |
| Linear / Atlassian | tickets | hosted | OAuth | seat | med | EG+WR | E | ASK | egress | mcp | later |

### Ops / Cost
| Tool | Use | Host | Creds | Cost | Sens | Class | C/R/E | Gate | Taint | Adapter | Priority |
|---|---|---|---|---|---|---|---|---|---|---|---|
| Cost ledger/budget read models | spend analysis | local | – | free | low | RO | C/R/E | ALLOW | – | native (done) | **now** |
| pricing.yaml | pricing review | local | – | free | low | RO | C/R/E | ALLOW | – | native (done) | **now** |
| ROI read models | value analysis | local | – | free | low | RO | C/R/E | ALLOW | – | native (Task 17) | **now** |
| Docker | build/run | local daemon | – | free | high | DG | E only | ASK | – | cli | later |
| GitHub Actions | CI status | hosted | PAT | free | med | RO+EG | R/E | ASK | egress | cli | later |

## 4. How the matrix maps to enforcement (mechanical, not per-tool code)

Each `ServiceSpec` in `catalog.py` carries: `name, teams, kind(adapter), hosted, credential_env
(tuple), pricing (fixed_zero | metered:unit | unknown), sensitivity, egress: bool, write: bool,
dangerous: bool, stages: frozenset({council,review,execution}), permission_default,
context_policy (B1), output_trust (B2), priority`.
Then the enforcement is derived, never hand-tuned per call site:

1. **Availability** = feature flag ON (`services.enabled`) ∧ credentials present ∧ (pricing
   known ∨ not metered). Any miss ⇒ the tool never registers (`is_available`) — the model
   never sees it; the UI shows *why* (disabled / missing credentials / unpriced / deferred).
2. **Gate** = `permission_default` from the spec; egress specs set `Tool.egress=True` ⇒ the
   Phase 9 taint demotion applies automatically; private-read specs set `reads_private`.
3. **Stages** = the engine intersects role tools with the stage floor: council/review ⊆
   `READ_ONLY_SPAWNABLE` (which grows ONLY by the semgrep/gitleaks RO-CLI additions — pinned),
   write/dangerous ⇒ Execution only, under the turn lock, via SubAgentGate + human approvals.
4. **Modes** = new service tools are NOT in `PLAN_SAFE` (allowlist ⇒ auto-denied in Plan until
   deliberately classified) and NOT auto-approvable (opt-in `auto_allow_tools`; AUTO_NEVER
   still excludes shell/write).
5. **Project scope** = service tools read `ToolContext.project` (KB precedent); per-project
   narrowing via `settings_json["services"]` ⊆ globally enabled; `service_calls` rows carry
   project_id/team/role/stage.
6. **Cost** = every service invocation writes a metadata-only `service_calls` row (service,
   operation, units, est_cost_usd — NULL if unpriced ⇒ blocking under
   `treat_unpriced_as_blocking`); LLM spend keeps flowing to `model_calls`, now with
   team+stage columns. Local free tools record a *known* $0 (fixed_zero), which is not the
   fail-closed NULL.
7. **Context policy (B1)** = the engine's context assembly checks the bundle's provenance
   against each role's services: a role holding a `public_only`/`repo_code_only` service is
   refused a bundle containing private-sourced material (memory/Gmail/Drive/secrets-adjacent
   paths); `local_only` services never appear on roles whose stage receives external content.
   The manifest records the provenance classes selected, so the check is auditable.
8. **Output trust (B2)** = the adapter wraps its result per the spec before it re-enters any
   prompt: only `trusted_local_scan` is unframed; external content, model-generated output,
   and security findings are all delimiter-framed untrusted (findings can quote hostile code).

## 5. Revised task order (10B = Tasks 10–19)

Discipline unchanged: every task green (`ruff check` + `uv run pytest`), keyless, committed
separately with explicit paths, learning-note bullets.

10. **Plan doc + spawn() extension + migration v8 + cost context**: commit this doc as
    `docs/PLAN-10B-teams.md`. `spawn()` gains `client/model/role/stage/orchestration_run_id/
    project_id/fresh_trace` (tool schema pinned unchanged; default path byte-identical). v8
    (plain SQL, additive): `model_calls += team, stage`; new `service_calls` table; version
    pins 7→8. `CostContext += team, stage`; `ServiceLedger` writer (A5 degradation shared).
11. **ServiceRegistry + catalog + flags + availability UI**: `services/` package, `ServiceSpec`
    + `SERVICE_CATALOG` (the full matrix as code — including later/avoid rows, so the UI can
    show "deferred"), `ServicesConfig.enabled: []`, per-project narrowing, availability
    resolution (flag ∧ creds ∧ pricing), pricing.yaml v2 `services:` section, Hub/Studio
    availability read model (available/disabled/missing-credentials/unpriced/deferred — never
    a key value). *No new adapters yet.*
12. **TeamProfiles + roster/workflow constants + ContextSelector**: `teams.py` (8 teams above),
    `roles.py` floors, `workflows.py` (10 templates + team defaults), `context.py` (framed
    bundle + bodies-free manifest + project-ownership validation). Invariant pins: ≤1 writer
    per team/template; council/review ⊆ READ_ONLY floor; member services ⊆ enabled catalog;
    settings overrides validated, never widened; members can never hold spawn_agent.
13. **OrchestrationEngine + store**: stages A–E on spawn(); team-aware scoping (role.tools ∩
    stage floor, services resolved per project/team/role/mode/stage); off-lock with the
    stage-C lock window; cancellation; orphan sweep; revise cap; engine trusts run records.
14. **Budgets + estimates + confirmation**: worst-case reservation before fan-out; between-
    stage soft/hard; per-role AND per-team caps; two-step confirm; unpriced roles/services
    block; estimates include flat per-op service costs.
15. **Orchestration API + WS v2 + Studio screen**: run/cancel mutations (pin 23→25),
    read models (summaries/manifests only), `EVENT_SCHEMA_VERSION → 2` with the five
    orchestration event types (now including team on agent updates), `studio.js`: selection =
    **workflow template | team profile | custom run**, roster cards w/ per-member model/effort/
    tools/services chips, availability states, stage timeline, side-by-side outputs, synthesis/
    verdict, Gate links, live team-attributed cost ticker, promote buttons.

    **⛔ CHECKPOINT D — MANDATORY stop (B6) before any adapter is enabled.** The complete
    framework with ZERO new external services enabled. Evidence, per bullet with named tests:
    (i) service catalog fail-closed — disabled / deferred / unpriced / missing-credential
    services do not register anywhere (the tool never exists); (ii) service tools inherit
    egress / reads_private / write / dangerous policy from their ServiceSpec (derived, not
    hand-set); (iii) team/role/stage/mode restrictions enforced by the engine (council member
    cannot hold an egress or write service; Plan denies service tools; Auto never approves
    them by default); (iv) context_policy enforced — a public_only/repo_code_only role is
    refused a private-sourced bundle (B1); (v) output_trust framing applied per spec (B2);
    (vi) service_calls ledger records project/team/role/stage/service, unpriced ⇒ NULL;
    (vii) no service surface (UI/API/trace/cost) leaks a secret — sweep extended over the
    services panel + service_calls read models; (viii) engine invariants (one writer, floors,
    forged-report inert) + budget reservation math; full suite green. Report, then continue.

16. **First enabled services (the "now" set)**: `semgrep.py` + `gitleaks.py` (hardened-argv
    CLI adapters, offline rules; **B4**: exclusion args derived from the `paths.py` sensitive
    floor — `.env`, `data/connectors/`, token stores — plus output filtered against
    `is_sensitive_path` as a second belt; gitleaks findings redacted to file:line + rule id,
    never a matched value) + `playwright_local.py` (**B3**: localhost allowlist + inspect-only
    verb set {navigate, screenshot, dom_inspect, a11y_check, visual_diff}; no click/type/
    submit). Feature-flagged; keyless tests via fake subprocess runners + a local test HTTP
    server; READ_ONLY_SPAWNABLE grows by exactly {semgrep_scan, gitleaks_scan} (pinned);
    scanner outputs framed `security_finding_untrusted` (B2); OWASP checklist ingested as a
    KB doc (ritual).
17. **ROI + team cost surfaces**: per-run cost breakdown by team/role/model/stage/service;
    ROI (baseline_minutes × hourly_rate − actual); Costs screen gains team + service groupings.
18. **Adversarial evals + safety pins**: the 15 non-negotiables executable — council member
    attempting web/write/service-egress (denied by floor); injected context instructing a
    scanner to exfiltrate (no egress tool exists in scope); cross-project service access
    (403/absent); flags-off ⇒ absent; unpriced service blocks; taint: drive_read then any
    egress service ⇒ demoted ASK; budget hard-stop halts a fan-out; report forgery inert;
    depth-1 stands; Auto/Plan matrices over service tools.
19. **Docs + live verification**: ADR-0015 (team tool intelligence: catalog/flags/derived
    enforcement), README/architecture Phase 10B sections, honest Phase 9 live-status kept
    (A6); live checklist below; if green, ratchet new eval baselines in a dedicated commit.

## 6. Now vs deferred (explicit)

**Enabled in 10B** ("now"): everything already native (filesystem/git-read, KB+converters,
tavily/web_fetch, drive-read, vault write, ledger/lab/ROI read models) + Semgrep + Gitleaks +
Playwright-localhost (+ visual diff) + OWASP checklist KB doc.

**Cataloged, feature-flagged, NOT built** ("later"): Firecrawl, Exa, Jina, SearXNG, Obsidian
MCP, Figma MCP, OpenAI image gen, Browserbase/Stagehand, general-web Playwright, GitHub
(gh RO first), Docker, Supabase/Neon, sqlite-RO, CodeQL, Promptfoo, BrowserStack, Linear/
Atlassian, GitHub Actions. Each has an adapter strategy in the catalog; enabling any is a
small follow-up (adapter + tests + flag), not a redesign. **No MCP client is built in 10B** —
mcp-kind entries stay deferred and the Hub stays honest.

**Avoid**: NotebookLM Enterprise (org auth, opaque pricing, high sensitivity — future only);
LangSmith (ships trace *content* off-box — revisit only with explicit consent + redaction);
generic Browser MCP (until an MCP layer exists).

## 7. Safety non-negotiables (all pinned by tests)

1. Teams are orchestration groups, not swarms: members are depth-1 spawn children; only the
   host engine coordinates; members can never hold `spawn_agent`.
2. Fable (planner route) remains the sole synthesizer/final verdict; engine stages, not
   team members, hold that authority.
3. Council/review ⊆ READ_ONLY_SPAWNABLE (no shell, no write, **no egress**); it grows in 10B
   by exactly {semgrep_scan, gitleaks_scan} — enumerated pin.
4. Exactly one write_capable member per team/template, active only in stage C, under the turn
   lock, through SubAgentGate + human approvals; write/dangerous services are Execution-only.
5. Service access is scoped by project ∧ team ∧ role ∧ mode ∧ stage — resolved by the
   registry/engine, never by the model; per-project service sets can only narrow.
6. External web/research services are egress: `Tool.egress=True` ⇒ Phase 9 taint demotion
   applies; private context cannot reach them post-private-read without the human.
7. No mode bypass: new service tools are outside PLAN_SAFE (fail closed in Plan) and outside
   Auto's allowlist by default; AUTO_NEVER and VoiceApprover/unattended paths untouched.
8. Missing credentials or unknown metered pricing ⇒ the service tool does not exist
   (unregistered), and orchestration refuses a roster that references it — fail closed with a
   visible reason, never a downgrade.
9. Ledgers (`model_calls`, `service_calls`) are metadata-only — never prompts, bodies,
   secrets, or matched secret values (gitleaks redaction pinned).
10. No key values in UI/logs/traces — availability is presence-booleans; the secret sweep
    extends over the services panel + service_calls surfaces.
11. Every enabled service ships keyless tests + a live/manual verification note.
12. All Phase ≤10A contracts intact: ADR-0002…0014, drafts-only Gmail, eval ritual, never-
    DELETE, closed mutation-route pin, one attention surface.
13. **context_policy (B1)**: external research/web services are `public_only` — private
    project memory, Gmail/Drive content, and secrets can never be assembled into their
    context; enforced in the engine's bundle check, pinned adversarially.
14. **output_trust (B2)**: every non-`trusted_local_scan` service output is framed untrusted
    before a model sees it — including scanner findings (they quote hostile code).
15. **Scanners respect the sensitive floors (B4)**; **Playwright stays inspect-only (B3)**;
    NotebookLM Enterprise and generic Browser MCP remain avoid/deferred (B5) — no flag
    enables them in 10B.

## 8. Tests / evals (beyond per-task units)

- Catalog invariants: every SERVICE_CATALOG row fully classified (all fields non-default-able);
  every "now" service has an adapter + tests; every mcp-kind row is priority later/avoid.
- Availability matrix: flag off / creds missing / unpriced ⇒ unregistered + correct UI state.
- Floor pins: READ_ONLY_SPAWNABLE exact-set; PLAN_SAFE unchanged unless deliberately edited.
- Ledger: service_calls rows attributed (project/team/role/stage/service); unpriced ⇒ NULL;
  A5 degradation shared with model_calls.
- Engine: scripted FakeClient runs for accept/reject/revise-cap/cancel/budget_stopped with
  team-attributed spend; lock held only in stage C; forged reports inert.
- Adversarial (Task 18) as listed above; eval scenarios reuse the Phase 9 injection pattern
  with service-flavored payloads.

## 9. Live verification (Task 19)

1. Enable `services.enabled: [semgrep, gitleaks, playwright_local]`; `jarvis` UI: Hub/Studio
   show the three available, others disabled/missing-creds/deferred with reasons; no key text.
2. Security team `security_review` on this repo: council (semgrep+gitleaks findings framed,
   OWASP checklist context) → synthesis by Fable → verdict; findings show file:line, never a
   secret value; run record + team-attributed costs in the ledger.
3. Frontend team `ux_critique` against the local workstation (playwright-localhost screenshots
   + DOM/a11y inspection + visual diff); confirm a non-localhost URL is refused by the adapter
   AND that no interaction verb (click/type/submit) exists to invoke (B3).
3b. Security proof (B4): run semgrep/gitleaks over this repo with a canary planted in a
   sensitive path (`.env`-style file + `data/connectors/`) — the canary appears in NO finding,
   and gitleaks findings show file:line + rule id only.
4. Taint demo: drive_read then a web egress attempt in one turn ⇒ demoted ASK (unchanged).
5. Budget demo: tiny per-run cap ⇒ reservation refuses the council fan-out with a clear reason.
6. Costs screen: by-team/by-service groupings match SQL sums; chunked eval gate re-run →
   aggregate → baseline ratchet commit if green.

## 10. Handoff instructions for Opus 4.8

- Execute Tasks 10–19 in order; MANDATORY stop at Checkpoint D (after 15, before any adapter
  is enabled) with the eight-bullet evidence list; per-task commits with explicit paths ending
  `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`.
- Amendments B1–B6 are binding: context_policy + output_trust on every ServiceSpec (derived
  enforcement, adversarially pinned); Playwright inspect-only; scanner sensitive-floor
  exclusions + redaction; NotebookLM/Browser-MCP stay unintegrated.
- Never commit `docs/PLAN.md` or `docs/PLAN-7-voice-consent-checkpoint.md`. Never weaken
  PermissionGate/taint/mode/project boundaries — modes compose at the documented seams only.
- Build ON `SubAgentService.spawn` (ADR-0014); no second agent framework, no MCP client.
- The catalog ships classifications for tools you do NOT implement — resist implementing
  them; "later" rows are catalog entries + UI states only.
- Reuse pins & patterns: hardened-argv CLI (RepoReader), is_available registration
  (connectors), untrusted framing, judge forced-schema panel, `_fire_digest` off-lock,
  `config.model_copy` per-child overrides, FK-enforced project ids (create projects in test
  fixtures), autouse `_close()` db fixtures.
- Chunked eval rule stands: background eval runs die ~14 min — stage per-suite, aggregate once.
- If a baseline needs adjustment, dedicated commit + explanation; a red gate blocks progress.
