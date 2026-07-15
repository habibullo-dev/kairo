# Fable Skill Forge — staged proposal

> **Historical design record.** This directory preserves the proposal, draft packs, counts, and
> source anchors from its recorded snapshot. It is evidence, not current Kira skill configuration;
> see the [documentation index](../README.md).

Fable's read-only architecture + skill-authoring pass, 2026-07-11, at HEAD `84e0988`. The packs in this directory remain drafts. The runtime seam and P0 fixes were implemented separately with skills disabled; promotion still requires explicit human review, a hash pin, and a committed configuration change.

| Artifact | Contents |
|---|---|
| [01-current-state-audit.md](01-current-state-audit.md) | Team/role/workflow inventory, actual runtime prompt & tool behavior, encoded invariants, gaps, P0/P1 risks, doc-vs-code conflicts — every claim `file:line`-anchored |
| [02-skill-system-design.md](02-skill-system-design.md) | Versioned skill-pack runtime: binding, compilation, injection seam, hashing/activation/rollback/audit, caching interaction, guard tests, P0 platform fixes |
| [03-skill-pack-schema.md](03-skill-pack-schema.md) | Strict pack schema: frontmatter, 12 fixed sections, compiled/authoring split, validation rules V1–V8 |
| [packs/](packs/) | Pilot packs: `core-engineering`, `backend-implementer`, `architect-reviewer`, `qa-eval`, `security-review` (all `status: draft`) |
| [04-evaluation-and-rollout.md](04-evaluation-and-rollout.md) | Baselines, A/B rubric, regression + adversarial additions, cost limits, shadow→checkpoint→pilot rollout, promotion/rollback criteria |

Recommended reading order: 01 → 02 → 03 → packs → 04.
Recommended first pilot: **Backend Implementer + Architect Reviewer** (with `core-engineering`), attended, after the Stage-2 checkpoint (04 §7).

Hard rule restated: packs are text-only process guidance. Fable may draft them, but only explicit human review plus a Git commit may promote a draft into `config/skills/packs/`. Tool scope, permissions, provider routing, budgets, and authority remain code-derived; no runtime model output, retrieval result, KB item, connector payload, or pack can create authority or self-activate (ADR-0004 discipline).
