# ADR-0012: Run modes are two seams — Plan denies at the gate, Auto approves at the approver

- **Status:** Accepted
- **Date:** 2026-07-07
- **Context phase:** Phase 10A (project workspaces)

## Context

Phase 10 adds visible run modes: **Plan** (read-only analysis), **Approval** (today's
behavior — the default), and **Auto** (auto-approve a low-risk allowlist). The hazard is that
a naive "mode wrapper around the gate" would sit *upstream* of the Phase 9 egress-taint rule,
and Auto would then auto-approve a tainted-egress call the taint rule had deliberately demoted
to a non-persistable ASK — silently reopening the exfiltration pipe Phase 9 closed. Modes must
add UX, never a new bypass of the PermissionGate or taint substrate.

## Decision

### 1. Plan and Auto live at DIFFERENT seams, both inside the loop

`Decision.persistable` (the taint demotion flag) is computed in `AgentLoop._handle_tools`
*after* `gate.check()`. So the two mode behaviors are placed relative to that:

- **Plan** denies at the gate seam — before the approver. After the raw gate decision + taint
  demotion, anything not in `PLAN_SAFE` becomes DENY. A plan-denied tool never prompts and
  never runs.
- **Auto** approves at the approver seam — it evaluates the **post-taint** decision. It only
  resolves an ASK to ALLOW when `decision.persistable` is still true, so a tainted-egress
  demotion (`persistable=False`) *always* reaches the human, even in Auto.

Both are pure predicates (`plan_blocks`, `auto_approves`) co-located with the taint transform,
so the ordering is provable and unit-tested as a matrix — not two wrapper objects reading a
live mode.

### 2. Plan mode is an ALLOWLIST (`PLAN_SAFE`), not a denylist

`PLAN_SAFE` is an explicit frozenset of read-only, non-egress, no-world-change tools
(`read_file`, `list_dir`, `glob_search`, `query_knowledge_base`, `lint_knowledge_base`,
`recall`, and the connector *reads*). A denylist ("deny side-effecting tools") would fail
*open* for a future tool nobody classified; the allowlist fails *closed* — a new tool is
denied in Plan until someone deliberately adds it, and a pinned test forces that decision.

### 3. Auto can never approve run_shell / write_file — config cannot widen it

`auto_allow_tools` is opt-in and empty by default (Auto adds no standing authority until a
human lists tools). `run_shell` and `write_file` are in `AUTO_NEVER` and are refused even if a
user lists them; `NEVER_PERSIST` and the SubAgentGate hard-denies are likewise excluded.

### 4. Mode is snapshotted per turn (permissive side only)

A mid-turn flip *into* Auto must not retroactively auto-approve an in-flight turn, but a flip
to a *stricter* mode should tighten immediately. So the loop freezes "did this turn start in
Auto" at `run_turn` start, and Auto applies only when `started_auto AND currently Auto` — a
flip into Auto (started_auto false) doesn't apply, and a flip out of Auto (currently not Auto)
stops it. Plan is read live per iteration, so tightening is immediate.

### 5. Modes are interactive-only; Debug is not a mode

The mode enum is `plan | approval | auto`. Debug stays a UI presentation flag — it must never
change what is permitted. The BackgroundRunner keeps its `UnattendedGate` (no mode provider ⇒
Approval semantics), so Auto can never leak into an unattended job; voice sessions are pinned
to Approval. Every decision line carries the mode; an Auto approval is recorded with
`resolution="auto_approved"` (visible in Trace, never hidden), and `mode_changed` /
`mode_auto_approved` are audited events.

## Consequences

- The Phase 9 exfil guard survives Auto mode — the load-bearing pre-mortem finding, pinned by
  a test (`auto never approves a non-persistable tainted-egress decision`).
- Adding a tool without classifying it into `PLAN_SAFE` fails the pin, forcing a deliberate
  read-only/side-effecting decision.
- No prior contract weakens: `UnattendedGate`, `VoiceApprover`, `NEVER_PERSIST`, and the
  SubAgentGate hard-denies are all untouched; mode composes on top.
