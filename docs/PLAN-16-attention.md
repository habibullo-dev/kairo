# Phase 16 — Attention + Automation (Notification Center + Dreaming)

*Plan-of-record. Approved by Habib 2026-07-10. Basis: `docs/ROADMAP-12-16-execution.md` §Phase 16,
bound to the post-15.6 substrate (HEAD `7aabcfc`, migrations v14, mutation pin 44, suite 2010,
core gate 19/19 $0). Discipline: per-task commits with EXPLICIT paths (never `git add -A`); keyless
tests per task; full suite + `ruff check` + `uv run kira eval gate --suite core` ($0 replay) green
each task; trailer `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`. Numbering:
migration **v15**, ADR-**0024**, mutation-route closed set **44 → 45** (one new route). Never commit
`docs/PLAN.md` or `docs/PLAN-7-voice-consent-checkpoint.md`.*

## Two products

1. **One attention system.** A durable `attention_items` queue that UNIFIES everything wanting
   Habib's judgment — live Gate ASKs (ephemeral, in `ApprovalManager`), durable write-intents
   (Phase 12 `write_intents`), graph suggestions (Phase 15 `graph_suggestions`), dreaming proposals,
   and system alerts — with kind / priority / project / routing. The Gate screen EVOLVES into the
   Notification Center: one attention surface, never two competing lists. attention_items is the
   queue OVER the sources; it never duplicates their authority (approve/reject still hit the
   existing gated routes).
2. **Proposal-only dreaming.** Scheduled unattended jobs (morning briefing, nightly review,
   bottleneck detection, ROI/time-saved summary, self-improvement proposals) that run in a CAGED
   tool context and can ONLY produce artifacts + attention items — never an action.

## Model policy (Habib, 2026-07-10)

Dreaming summaries + proposal generation default to the **utility route (Haiku)** — private_ok,
cheap, good enough for recurring proposal-only work. **Escalate to Sonnet 5** for: weekly/monthly
deep review; security-sensitive findings; finance/client/company-critical summaries; low-confidence
or ambiguous proposal generation; any proposal affecting code architecture, major workflow changes,
or high-priority personal decisions. Model/pricing/provider uncertain ⇒ fail closed (no run).

## Safety non-negotiables (each a pinned test)

1. **Dreaming NEVER executes.** It emits only artifacts + attention items. Accepting any proposal is
   a human on an EXISTING gated route (task→`/api/tasks/create`; memory/graph→the suggestion approve
   route; self-improvement→text the human acts on). The center adds ZERO new authority.
2. **Tool cage by construction.** The dreaming registry is built from an enumerated `DREAMING_TOOLS`
   allowlist (read-only + internal proposal/artifact writers). No egress / connector-write / send /
   shell / spawn / schedule / delete tool is REACHABLE — adversarial eval tries each by name;
   `registry.names()` is exact-set pinned.
3. **Dreaming outputs are proposal / UNTRUSTED by default** (`trust_class = model_generated`).
4. **Dreaming outputs are NEVER auto-injected into future model context** (self-injection quarantine
   — the `graph_suggestions` precedent) and never FTS-indexed / retrievable / exported.
5. **Minimized egress.** No urgent Telegram/Kakao push includes email/task/body content — ONLY a
   minimized title + count + category (the digest-minimization contract). Routing-matrix test.
6. **Budget cap.** Per-night dreaming spend cap (config, default $1.50). Cap-hit halts the run
   mid-chunk and emits exactly ONE `alert` attention item. Unpriced ⇒ blocked.
7. **Cross-project isolation** in SQL; quiet-hours + per-project mute are NARROWING config only.
8. **UnattendedGate unchanged**; each dreaming job is chunked ≤ ~10 min (the 14-min ceiling) — an
   orchestrating schedule, never one long run.
9. The mutation closed set grows by EXACTLY one route: `POST /api/attention/{id}/resolve`
   (metadata-only done / dismiss / snooze). All existing pins intact: Gate / taint / modes /
   context_policy / pricing fail-closed / cost ledger.

## Tasks (10 — STOP at Checkpoint K after Task 9)

1. **This plan + migration v15 + AttentionStore + lifecycle.** `attention_items` (kind, source,
   source_ref, project_id, priority, state, trust_class, title, category, payload_json,
   evidence_json, dedupe_key UNIQUE, timestamps). Store modeled on `IntentStore`: plain SQL on the
   shared conn+lock, idempotent create by dedupe_key, a validated `ALLOWED_TRANSITIONS` state
   machine (open → done/dismissed/snoozed/expired; snoozed → open/…). Nothing renders yet.
2. **Source absorption read model** — one queue view over live approvals + write-intents + graph
   suggestions + attention rows (pointers, never duplicated authority) + `POST /api/attention/{id}/
   resolve` (pin 44→45).
3. **Notification Center screen** — the Gate tab evolves; approve/reject still hit the EXISTING
   gated routes; the center adds routing + priority, not authority.
4. **Routing rules** — config-shaped matrix (urgent → push, minimized; normal → digest; low →
   center-only) + notifier wiring + quiet hours + per-project mute. Matrix test incl. minimization.
5. **Dreaming cage** — `DreamingContext`: allowlisted registry builder + UnattendedGate + per-night
   budget cap + halt→alert. A no-op dreaming job proves the cage BEFORE any content builder exists.
6. **Content builders** — morning briefing, nightly review, bottleneck, ROI, self-improvement:
   deterministic collectors + one summarize each (digest pattern); model policy above; golden-file
   tests on fixtures.
7. **Job definitions + chunking** — each builder is one ≤10-min chunk; DEFINED but NOT scheduled;
   manual `kira dream run <job>` for attended testing only.
8. **Adversarial evals** — cage reachability per forbidden tool; injected reviewed-content cannot
   escalate a proposal into an action; budget-halt; routing minimization; cross-project isolation;
   self-injection quarantine.
9. **UX pass** — priority discipline (defaults bias to digest; notification fatigue is a product
   failure), quiet hours, per-project mute; screenshot DoD for the center. **→ ⛔ CHECKPOINT K.**

   **⛔ CHECKPOINT K — MANDATORY FULL STOP before any dreaming job is scheduled unattended.**
   Evidence: (i) cage reachability proof (no egress/write/send/schedule/delete reachable);
   (ii) injected-content-can't-escalate-a-proposal eval; (iii) budget-halt demo + single alert;
   (iv) routing minimization matrix (urgent = title/count/category only); (v) one-attention-surface
   evidence (Gate absorbed, not duplicated); (vi) attention lifecycle + cross-project isolation;
   (vii) the agreed week-long observation plan + abort criteria; (viii) full suite green.

10. *(after K sign-off ONLY)* ADR-0024 + `docs/verification-16.md` + README; schedule morning
    briefing + nightly review; begin the week-long live observation window (no other live changes in
    flight).

**Deferred (unchanged):** auto-applying proposals (indefinitely, until a consent-framed phase);
workflow auto-tuning; two-way Telegram/Kakao remote approval (candidate Phase 18).
