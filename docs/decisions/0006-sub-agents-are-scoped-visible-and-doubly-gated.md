# ADR-0006: Sub-agents are scoped, visible, and doubly-gated

- **Status:** Accepted
- **Date:** 2026-07-07
- **Context phase:** Phase 6 (multi-agent orchestration)

## Context

Phase 6 lets the primary agent **delegate**: spawn a scoped sub-agent with an isolated
context, run it, and synthesize its report. A sub-agent is just another `AgentLoop`
turn — the thesis holds — so the novelty is entirely in the *constraints*: delegation
multiplies the surface area of everything the safety model exists to bound (tool
authority, prompt-injection sinks, unattended action, context laundering). A pre-mortem
found five ways the obvious design would betray a contract the earlier phases established;
this ADR records the decisions that close them, and the rationale for the ones that were
*decided but not built*. The distinctive risk: **a delegated action that no one can see
is worse than one that's denied** — so visibility and auditability are treated as safety
properties, not conveniences.

## Decision

### 1. Depth 1, enforced three independent ways

A sub-agent can never spawn another. This is guaranteed by three overlapping mechanisms,
each failing closed, so no single missed check re-opens recursion:

1. `spawn_agent` is absent from every child's `ScopedRegistry` (a child's registry is a
   filtered view; `spawn_agent` is not in the spawnable set).
2. `SubAgentGate` hard-denies `spawn_agent` (with the meta tools) before any policy.
3. `SubAgentService.spawn` refuses re-entry via a contextvar set for the duration of a
   child run.

There is no autonomous swarm and no hidden agent: every agent that runs was individually
approved by a human at a prompt.

### 2. The double gate — approve the contract, then gate every call anyway

Delegation passes through **two** gates:

- **Gate one, the spawn:** `spawn_agent` defaults to ASK and joins `_NEVER_PERSIST`
  (with `schedule_task`/`cancel_task`) — a stray "always" keystroke must never open a
  delegated-execution channel. The approval prompt shows the **full, untruncated task
  prompt plus the tool scope**; a long prompt pages behind a `v` (view) option, but y/N
  is only offered once the full text is available. The human consents to the exact task
  and the exact authority.
- **Gate two, every child tool call:** `SubAgentGate` wraps whichever gate the parent
  used and can only ever *narrow* its decisions — hard denies → scope check → delegate to
  the inner gate (every floor survives: sensitive-path denial, write-allowlist
  escalation, KB denylist, shell-metacharacter escalation) → run-scoped grant. A child's
  ASK is forwarded to the human like any other; the interactive safety story holds
  verbatim for delegated actions.

### 3. Run-scoped grants are pattern-scoped and never persisted

"a-for-this-run" at a child's prompt does not grant the whole tool — it grants a
*pattern* derived from the approved call, shown to the human, living only in that child
run's gate instance (never written to `permissions.yaml`, gone when the run ends):

| tool | "a" grants (this run only) |
|---|---|
| `web_fetch` | the approved URL's exact host |
| `read_file` / `list_dir` / `glob_search` | the approved path's resolved directory prefix |
| `web_search` / `query_knowledge_base` | tool-level (the query varies; the backend is the fixed surface) |
| `run_shell` / `write_file` | **never** — each shell command and file write is approved individually |

The load-bearing case: a research child working one docs site prompts once, but a
poisoned page that redirects it to `attacker.example` prompts again. A blanket tool-level
grant for `web_fetch` would defeat exactly the injection defense delegation most needs.

### 4. No unattended spawning, and no *safety* fan-out cap

- **`spawn_agent` is in the unattended `HARD_DENY` set.** A background job cannot spawn
  sub-agents in this phase, full stop. ADR-0003's whole design is "no human present ⇒
  deny everything that would ask"; delegation multiplies that surface, and a job that
  fans out into N children is the definition of an unsupervised swarm. **Unattended
  delegation is deferred, not built** — the future-work preconditions are: per-child and
  aggregate token budgets, a cap on total children per job, and quarantined review of any
  child side effects (mirroring the KB `unreviewed` posture). This is the same
  "decided, not built" discipline as the auto-injection verdict in ADR-0005.
- **There is no safety-motivated fan-out cap.** Every spawn is individually
  human-approved (ASK, never "always"), so the human *is* the rate limiter — a hidden
  numeric cap would just be a second, worse approval mechanism. There **is** a
  runaway/UX guard, `sub_agents.max_spawn_calls_per_turn` (default 8), enforced per
  parent trace id: it bounds how many children run in one turn so a model gone sideways
  can't bury the terminal, and `sub_agents.max_parallel` (default 4) is a concurrency
  semaphore. Both are resource/UX bounds, **not** safety bounds — the distinction is
  deliberate and recorded so neither is mistaken for the thing that keeps delegation safe.

### 5. Nothing about a child is hidden

- **Events.** Every child event is forwarded to the parent's sink inside a
  `SubAgentEvent` — including its `ToolDecision` *attempts*, the load-bearing signal for
  adversarial evals. The REPL renders compact tagged child-activity lines (no child text
  streaming); a `SubAgentCompleted` carries the child's usage/cost so delegated spend is
  summed into the session total and the eval token ceiling — child tokens are never
  invisible spend.
- **Sessions.** Each child transcript persists as a `kind='subagent'` session.
  `latest_session_id()` selects only `'interactive'` (a resume can never land in a child
  transcript), and reflection can never touch a subagent session — see §6.
- **Audit.** An `agent_runs` row (never DELETEd — the ADR-0005 retention constraints
  extend to it; `parent`/`child_session_id` are FK `ON DELETE SET NULL`) is opened
  `running` *before* the child executes, so a crash leaves an orphan the startup sweep
  marks `aborted`. Each row records **both** trace ids — parent and child — so one log
  query reconstructs the full delegation causality chain. The parent id is captured
  before the child runs and the child id after; this only stays uncontaminated because
  `asyncio.gather` runs each tool in a *copied* context, so a child's `bind_trace()`
  never leaks into the parent turn.

### 6. The report is data, and the reflection firewall is structural

- **Report framing.** A child's final text returns to the parent wrapped in
  untrusted-content delimiters, with a header composed **from the run record, never from
  child text** (a child that read a poisoned page can't forge its own "0 denied, ok"
  status line). The report is a fresh injection channel back into the parent — which
  never saw the page — so it gets the same treatment as `web_fetch` output.
- **Reflection firewall, made structural.** Delegation opened two paths by which a child
  could launder into long-term memory: (a) a child report is a tool_result, and
  reflection's `_strip_tool_results` removes all tool_result bodies before the extractor
  sees them; (b) a subagent *session* could be reflected. Path (b) was previously guarded
  by a boolean `include_task_sessions` whose `True` arm removed the kind filter *entirely*
  — a footgun that would have swept subagent sessions in the day anyone widened job
  reflection. Phase 6 replaced it with an explicit `kinds` parameter intersected with a
  `REFLECTABLE_KINDS` ceiling (`{interactive, task}`) that omits `subagent`, so **no
  caller — however buggy — can reflect a subagent session.** Both paths are pinned by
  tests.

### 7. Kairo naming: adopt subsystem names in docs, defer the code/product rename

The Kairo subsystem names are adopted at the **documentation level** (architecture.md,
README): Kairo **Core** (`core/`), **Command** (`cli/`), **Gate** (`permissions/`),
**Vault** (`knowledge/` + `memory/`), **Trace** (`observability/` + audit), **Lab**
(`tests/evals/`), and **Orchestrator** (`agents/` — this phase). "Hub" is **reserved for
the future connectors/MCP layer** (a hub is where external spokes attach) and must not be
reused for the multi-agent subsystem — the naming collision is resolved here so it isn't
relitigated.

A `jarvis`→`kairo` **code/product rename is deliberately not done in this phase.** It
would touch every import, the entry point, `data/jarvis.db`, the log-file prefix, memory
contents, and — critically — the system-prompt identity ("You are Jarvis"), which changes
*agent behavior* and therefore invalidates the live-eval baseline's comparability exactly
when `--compare` is needed to prove delegation regressed nothing. The rename is a
dedicated standalone milestone for later: one mechanical commit sequence (package → entry
point → identity prompt → data/log paths with back-compat reads → docs), followed by a
fresh live baseline that re-anchors `baselines.yaml` under the new identity.

## Consequences

- Delegation is available interactively, visibly, and under two gates; the safety
  contracts of ADR-0002/0003/0004/0005 are untouched (verified: the existing 24 eval
  scenarios PASS→PASS across the Phase 6 baseline; see `docs/evals-baseline-phase6.md`).
- A few more approval prompts during delegation (each child ASK forwards), traded for
  zero weakening of the interactive safety story. Revisit only with eval evidence that
  prompting friction breaks real delegation flows.
- Children run on the main model by default (`sub_agents.model` unset → `models.main`) —
  quality-first, no cheap-child tier; model routing is config, not something a prompt
  injection can downgrade.
- Unattended delegation and any auto-scaling of the fleet remain future work with
  recorded preconditions.

## Alternatives considered

- **Spawn-time blanket grants** (approving the spawn pre-authorizes the scoped tools'
  ASKs): rejected — it collapses the double gate into one and lets a child act on
  injected content without a prompt. Per-call ASK with a pattern-scoped run grant keeps
  the ergonomics without the hole.
- **Giving children memory access** (auto-recall or `recall` in scope): rejected —
  context isolation and least privilege beat convenience; the parent packs the context it
  approves. Revisit if delegation-quality evals trace grounding failures to missing
  personal context.
- **A numeric safety fan-out cap:** rejected as a *safety* control (the human approving
  each spawn is the real limiter); kept only as a UX/runaway guard, explicitly labeled.
- **Unattended-style demotion inside interactive children** (deny side-effecting ALLOWs):
  rejected — the human is present and watching the tagged stream; policy ALLOWs that the
  user set and whose scope the user approved should apply.
