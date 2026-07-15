# Fable Skill Forge — historical proposal

> **Historical design record.** This directory preserves the proposal, draft packs, counts, and
> source anchors from its recorded snapshot. It is evidence, not current Kira skill configuration;
> see the [documentation index](../README.md).

Fable's read-only architecture + skill-authoring pass, 2026-07-11, at HEAD `84e0988`. Since that
snapshot, `core-engineering`, `backend-implementer`, and `architect-reviewer` were reviewed and moved
to the hash-pinned runtime catalog in `config/skills/packs/`, where they currently run in shadow
mode. Their stale proposal copies were removed from this directory; `qa-eval` and `security-review`
remain archived proposal drafts and require a full evidence re-audit before any promotion.

| Artifact | Contents |
|---|---|
| [01-current-state-audit.md](01-current-state-audit.md) | Team/role/workflow inventory, actual runtime prompt & tool behavior, encoded invariants, gaps, P0/P1 risks, doc-vs-code conflicts — every claim `file:line`-anchored |
| [02-skill-system-design.md](02-skill-system-design.md) | Versioned skill-pack runtime: binding, compilation, injection seam, hashing/activation/rollback/audit, caching interaction, guard tests, P0 platform fixes |
| [03-skill-pack-schema.md](03-skill-pack-schema.md) | Strict pack schema: frontmatter, 12 fixed sections, compiled/authoring split, validation rules V1–V8 |
| [packs/](packs/) | Archived proposal drafts: `qa-eval` and `security-review` (not current instructions); reviewed runtime packs live only under `config/skills/packs/` |
| [04-evaluation-and-rollout.md](04-evaluation-and-rollout.md) | Baselines, A/B rubric, regression + adversarial additions, cost limits, shadow→checkpoint→pilot rollout, promotion/rollback criteria |

Recommended reading order: 01 → 02 → 03 → packs → 04.
The original first-pilot recommendation was **Backend Implementer + Architect Reviewer** with
`core-engineering`; current activation status is defined only by `config/settings.yaml` and the
runtime catalog.

Hard rule restated: packs are text-only process guidance. Fable may draft them, but only explicit human review plus a Git commit may promote a draft into `config/skills/packs/`. Tool scope, permissions, provider routing, budgets, and authority remain code-derived; no runtime model output, retrieval result, KB item, connector payload, or pack can create authority or self-activate (ADR-0004 discipline).
