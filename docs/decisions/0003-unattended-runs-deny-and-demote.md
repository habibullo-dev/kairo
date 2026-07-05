# ADR-0003: Unattended runs — ASK degrades to DENY, interactive grants don't extend, no per-task grants

- **Status:** Accepted
- **Date:** 2026-07-06
- **Context phase:** Phase 3 (tasks & scheduling)

## Context

Phase 3 lets a background **job** run a stored prompt as a real agent turn with no
human present. That removes the interactive safety story — "anything risky prompts
the user" — so the permission model needs an explicit unattended variant. Three
pressures shape it:

1. **No one can answer an ASK.** A job that hits an `ask` decision cannot prompt a
   human; blocking would hang, and auto-allowing would be a silent escalation.
2. **Policy `ALLOW`s are the real escalation channel — not ASKs.** The gate resolves
   many calls to `ALLOW` *before* any approver is consulted: persisted
   `tools: {x: allow}`, shell prefix rules, write-allowlist dirs. Every "always
   allow" the user granted *while watching an interactive stream* would otherwise
   apply, unwatched, at 3am — a poisoned page in a research job riding an allowlisted
   `git ` prefix straight to execution.
3. **`schedule_task` is a deferred-execution injection sink.** A stored payload is
   replayed later and *run with tools*. If a task could carry its own permission
   grants, one mis-read approval would become standing self-authorization, and a job
   could schedule more jobs.

## Decision

Background runs use an **`UnattendedGate`** (wrapping the normal `PermissionGate`)
plus a **`HeadlessApprover`**:

1. **ASK ⇒ DENY.** The headless approver denies every `ask`, returning an `is_error`
   tool result the model reads ("couldn't do that unattended") and adapts to. It
   provably never touches stdin.
2. **Interactive ALLOW does not extend to unattended runs.** The gate demotes
   `ALLOW → DENY` for the side-effecting tools `run_shell` and `write_file` unless the
   tool is named in `scheduler.unattended_allow_tools` (default `[]`) — the single,
   explicit, config-file opt-in surface.
3. **Meta tools are hard-denied regardless of policy.** `schedule_task`,
   `cancel_task`, `remember`, `forget` return `DENY` before policy is even consulted,
   so a persisted allow can't reopen self-replication or unattended memory writes.
4. **No per-task permission grants, ever.** The only way to widen what background jobs
   may do is `permissions.yaml` (via the opt-in set) — a place edited consciously, not
   a field the model can populate.

Two further controls sit alongside the gate: the stored payload is replayed inside an
**envelope** ("this is a STORED instruction, not a live human; no one is present"),
and background sessions are `kind='task'` — invisible to `--resume` and excluded from
reflection by default, so a job's transcript can't hijack the next session or launder
fetched content into long-term memory.

## Consequences

- **Upside:** the unattended attack surface is closed at the gate, not by convention.
  The safety tests (`tests/unit/test_unattended.py`) assert the load-bearing property —
  a persisted/policy `ALLOW` on `run_shell`/`write_file`/`schedule_task` is *still*
  denied unattended — and were written and committed before any runner code existed.
- **Cost:** a legitimately autonomous job (e.g. a nightly writer) needs a deliberate
  `unattended_allow_tools` entry; it can't inherit an interactive grant. That friction
  is the point — unattended write/exec is exactly what should require a conscious,
  file-level decision.
- **Boundary:** demotion covers `run_shell` and `write_file` specifically (the
  side-effecting builtins). A future tool with side effects must be added to the
  demote set — the set is a small, auditable constant in `permissions/unattended.py`,
  not an inferred property.
