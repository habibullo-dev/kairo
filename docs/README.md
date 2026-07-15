# Kira documentation

This index separates current operating guidance from historical design evidence. When a historical
record disagrees with current code or tests, the current source of truth wins.

## Current guidance

- [Kira User Guide](KIRA-USER-GUIDE.md) — setup and daily use.
- [Architecture](architecture.md) — current system boundaries and compatibility behavior.
- [macOS migration](migration-macos.md) — moving an existing workplace safely.
- [Remote Operator](remote-operator.md) — Telegram operator setup and controls.
- [Eval cost control](evals-cost-control.md) — replay, recording, live gates, and spend limits.
- [Brand assets](assets/KIRA-BRAND-ASSETS.md) — shipped logo and preloader provenance.

## Historical records

The following paths preserve the names, commands, paths, route counts, schema versions, test counts,
and line references that were accurate for the snapshot they describe:

- `PLAN*.md` and `ROADMAP*.md`;
- `decisions/` and `verification-*.md`;
- `evals-baseline*.md` and `learning-notes.md`;
- `fable-*/` audit, design, and proposal records;
- `phase-11-implementation-playbook.md` and `KIRA_10X_PRODUCT_PLATFORM_PLAN.md`.

These documents are evidence, not current Kira setup or implementation instructions. Rewriting
their bodies would falsify old commands and baselines, so ambiguous entry points carry a historical
banner instead.

## Compatibility identifiers

Former executable, database, log, lock, browser-storage, and graph identifiers remain only where
Kira must read or safely migrate existing user state. Their presence is not current product copy.
New current documentation should use Kira names and canonical `kira` commands.
