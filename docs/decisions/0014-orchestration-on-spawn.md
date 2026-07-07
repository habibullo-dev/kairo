# ADR-0014: Orchestration is a host engine on Phase-6 spawn, not a second agent framework

- **Status:** Accepted (design); implementation in Phase 10B
- **Date:** 2026-07-07
- **Context phase:** Phase 10B (AI Orchestration Studio)

## Context

The Orchestration Studio runs a team of role-agents through a workflow (council →
synthesis → execution → review → verdict). The tempting mistake is to build a new agent
runtime. Phase 6 already has `SubAgentService.spawn` — a public, host-callable coroutine with
depth-1 enforcement, `ScopedRegistry` + `SubAgentGate` double-gating, an `agent_runs` audit
trail, and event forwarding — proven host-drivable by the eval runner. The decision is to
build the engine *on top of* that primitive, so every existing safety floor is inherited, not
re-implemented (and re-weakened).

## Decision (to be implemented in 10B)

### 1. A host engine drives `spawn()`; no new framework

`OrchestrationEngine` calls `SubAgentService.spawn` per role. `spawn` gains backward-compatible
keywords (`client` / `model` / `role` / `stage` / `orchestration_run_id` / `fresh_trace`); the
`spawn_agent` *tool* passes none of them, so model routing stays config-only and never becomes
model-controllable (pinned). The default `spawn` path stays byte-identical to Phase 6.

### 2. Council/review roles are read-only with NO egress

A hard `READ_ONLY_SPAWNABLE = {read_file, list_dir, glob_search, query_knowledge_base}` floor
for council/review roles — no shell (never read-only), no write, **no web egress**. A council
agent ingests the most untrusted, cross-source context; it must not be able to exfiltrate it.
A researcher role that needs the web is separate and never also holds cross-source context.

### 3. Exactly one writer, only in stage C, under the turn lock

At most one `write_capable` role per workflow, and only the Execution stage may hold it — it
acquires the shared turn lock (it is a writer; the digest's "work off-lock" pattern applies to
the read-only stages). Its tools still route through the existing `SubAgentGate` + human
approvals; there is no mode/orchestration bypass, and Plan-mode surfaces refuse to start an
execution-class workflow.

### 4. The engine trusts run RECORDS, never child report text

Stage transitions read `agent_runs` / `orchestration_runs` state, never the framed report
strings (which are untrusted, anti-forgery-headered). A forged "status: done" in a child's
output can't advance the engine. Run titles are sanitized (never raw user/email text), and
`model_calls`/`orchestration_runs` stay metadata + short-summary only — verbatim prompts live
only in the debug-only `agent_runs` store, never rendered on Studio/Costs surfaces (A2).

### 5. Budgets reserve worst-case before fan-out

A per-run hard cap checked only *between* stages lets a parallel council overspend N×. The
engine reserves worst-case (fan-out width × per-child token ceiling) before launching a
parallel stage and refuses the stage if the reservation exceeds remaining budget; unpriced role
models block the run unless an explicit, audited per-run override. Synthesis/verdict are
tool-less forced-schema calls (the judge-panel pattern).

## Consequences

- Every Phase-6 invariant (depth-1, double-gating, audit, orphan sweep) is inherited; the
  engine adds stages + budgets, not a parallel authority.
- Context is serialized into each child's prompt with untrusted framing (children are
  isolated — the only inbound channel is the prompt); a bodies-free manifest records what was
  selected.
- `cost_context` is set inside each child coroutine so parallel council spend attributes to the
  right role (ADR-0013 §5).
- The orchestration WS event types + `EVENT_SCHEMA_VERSION` bump land here (10B), when the
  versioned Event schema actually changes.
