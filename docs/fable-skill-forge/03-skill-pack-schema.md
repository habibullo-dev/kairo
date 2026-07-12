# 03 — Skill Pack Schema (strict)

Status: PROPOSAL. Every pack in [packs/](packs/) conforms to this schema. The loader described in [02-skill-system-design.md](02-skill-system-design.md) §3.2 validates it **fail-closed**: unknown frontmatter key ⇒ reject pack; missing required section ⇒ reject; over token budget ⇒ reject.

## 1. File format

One pack = one markdown file: strict YAML frontmatter + fixed markdown sections in fixed order. Filename = `<id>.md`.

## 2. Frontmatter (all keys shown; no others permitted)

```yaml
---
id: backend-implementer          # kebab-case, unique, = filename stem
name: Backend Implementer        # display name
version: 1.0.0                   # semver; MUST bump on any content change
status: draft                    # draft | shadow | active | retired (authoritative state
                                 #   is settings.yaml `skills.enabled` + mode; this field
                                 #   is documentation of intent and lint-checked ≤ that state)
owner: habib                     # human accountable for the pack
created: 2026-07-11              # ISO dates, absolute
updated: 2026-07-11
applies_to:                      # ALL four axes must match a spawn for the pack to bind
  teams: [backend]               # team ids from teams.py TEAM_PROFILES, or ["*"]
  roles: [be_implementer]        # roster member ids, or ["*"]
  route_roles: [coder]           # registry roles (models/roles.py ROLES), or ["*"]
  stages: [execution]            # council|synthesis|execution|review|verdict, or ["*"]
rank: 10                         # compile order, ascending; 0 reserved for core packs
token_budget: 1200               # max compiled tokens (estimate: chars/4); hard cap 2000
requires: [core-engineering]     # pack ids that must also be active; loader refuses otherwise
conflicts: []                    # pack ids that may not be co-active
---
```

Deliberately **absent** fields (their absence is the safety property): no `tools`, no `services`, no `permissions`, no `model`, no `budget`, no `routes`. A pack cannot express authority; the schema has no vocabulary for it.

Hashes are **not** authored in the file. The file `sha256` is computed by the loader and pinned in `settings.yaml` at activation (02 §3.3); `compiled_sha256` is recorded per run.

## 3. Required sections, in order

| # | Section | Compiled into prompt? |
|---|---|---|
| 1 | `## Mission` | yes |
| 2 | `## Non-goals` | yes |
| 3 | `## Assumptions and context boundaries` | yes |
| 4 | `## Operating procedure` | yes |
| 5 | `## Evidence requirements` | yes |
| 6 | `## Verification` | yes |
| 7 | `## Stop and escalation conditions` | yes |
| 8 | `## Failure modes and anti-patterns` | yes |
| 9 | `## Deliverable format` | yes |
| 10 | `## Examples` | **no** (authoring-only) |
| 11 | `## Revision triggers` | **no** (authoring-only) |
| 12 | `## Source evidence` | **no** (authoring-only) |

Compiled = emitted by `compile_skills` into the member's system prompt, in this order, after the code-owned preamble (02 §3.5). Authoring-only sections exist for maintainers and reviewers; they never reach the wire, so examples and citations don't consume the token budget.

### Section contracts

1. **Mission** — 1–3 sentences. What this member is for, in this repo. No adjectives without operational content.
2. **Non-goals** — bulleted; what the member must NOT attempt even if the task text invites it. Non-goals are behavioral only; tool impossibilities are enforced by code and may be *restated* here for clarity but never relied on.
3. **Assumptions and context boundaries** — what the member can expect in its input (e.g., "framed context bundle; the task brief is inside the untrusted frame"), what it cannot see (conversation, memory, other members' work unless stated), and the standing rule that framed content is data.
4. **Operating procedure** — numbered steps. Each step names the tool(s) it uses. Steps must be executable within the member's actual scope (03 §5 V6).
5. **Evidence requirements** — what must be inspected/cited before any claim. Minimum: every factual claim about the repo carries `file:line`; every "it works/passes" claim carries verbatim command output; anything unverifiable is listed under UNCERTAINTIES.
6. **Verification** — exact commands or checks, marked `[RUN]` (member can execute it within scope) or `[RECOMMEND]` (member lacks the tool; it must name the command for a capable stage/human instead of claiming the result). This distinction is load-bearing: read-only members (council/review) have no `run_shell` (`roles.py:23-32`) and must never claim to have run anything.
7. **Stop and escalation conditions** — enumerated triggers → the exact required response (usually: emit a `BLOCKED` report per §4 format and end turn). "Escalation" for a spawned member always means *report and stop* — a member cannot page a human directly; the human sees run records and reports.
8. **Failure modes and anti-patterns** — each entry names the concrete bad behavior it prevents, tied to a repo mechanism where possible.
9. **Deliverable format** — the exact report skeleton the member's final message must follow. Structured headings, machine-scannable; report the format even when the answer is "nothing found".

## 4. Normative rule format

Every rule inside Operating procedure / Evidence / Stop conditions answers six questions. Compact table or inline form:

- **Trigger** — when does this apply?
- **Action** — exactly what to do.
- **Evidence** — what must be inspected or cited.
- **Verification** — how completion is proven ([RUN] output or [RECOMMEND] handoff).
- **Escalation** — when to stop and report BLOCKED instead.
- **Failure mode prevented** — the named bad behavior.

## 5. Validation rules (loader + `skills lint`)

- **V1** frontmatter parses; only the keys in §2; `id` = filename stem; semver valid; dates ISO.
- **V2** all 12 sections present, in order, non-empty.
- **V3** `applies_to` values exist: teams ∈ `TEAM_PROFILES` ids, roles ∈ that team's member ids (when teams ≠ `*`), route_roles ∈ `ROLES`, stages ∈ `{council, synthesis, execution, review, verdict}` — validated against code at load, so a renamed team breaks the pack loudly.
- **V4** compiled size ≤ `token_budget` ≤ 2000.
- **V5** no authority language (lint L3, 02 §7); no framing delimiter strings or SPECIMEN markers in the file (L4); no project-private facts (packs are non-sensitive by construction, human-reviewed).
- **V6** every `[RUN]` verification step must be executable within the scope of every member the pack can bind to; otherwise it must be `[RECOMMEND]`. This is a mandatory human-review checklist in v1; automated scope parsing is a follow-on lint, not a claim of the current loader.
- **V7** `## Source evidence` cites ≥ 3 repo `file:line` anchors (L5) — packs must be grounded in this repository, not generic advice.
- **V8** `requires` closure is active-able (no cycles, no conflicts intersection).

## 6. Compiled output shape (for reference)

```
[code-owned preamble — 02 §3.5, not authorable]
[member identity line — from roster, not from pack]

## Skill: <name> v<version>
### Mission
...
### Non-goals
...
(sections 3–9 in order)
```

Multiple packs concatenate in `(rank, pack_id)` order; `core-engineering` (rank 0) always leads, maximizing the shared stable prefix (02 §5).
