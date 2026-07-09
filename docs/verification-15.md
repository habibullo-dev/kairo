# Phase 15 — verification (Memory Graph + Knowledge Topology)

*Prepared 2026-07-09. Phase 15 has two verification layers: (A) a **keyless suite + screenshot DoD**
that is GREEN now and is the per-task gate, and (B) a **live ritual** (below) that exercises the real
DB, a real suggest→approve cycle, unified search, the Obsidian write, embedding spend under cap, and
the adversarial suggestion live — to be run in an interactive session with keys. The graph is
read/reason-only, so (A) covers every safety wall; (B) confirms the end-to-end experience and records
the two positive-path core cassettes. No `config/*.yaml`, `.env`, connector/token file, `design/`,
`docs/PLAN.md`, or `docs/PLAN-7-*` was touched.*

## A. Keyless suite + screenshot DoD — GREEN

Full suite **1916 passed / 2 skipped** (expected skips: playwright-installed degradation path;
Windows symlink privilege), ruff clean (src + tests), `jarvis eval gate --suite core` **19/19 PASS
3/3** (keyless cassette replay, **$0**), across all eight Phase-15 commits.

### Screenshot DoD — GREEN (36/36)

`tests/ui/graph_dod.py` (self-contained: seeds subgraph JSON in-process, serves a COPY of the static
dir, runs the REAL `graph.js` + `graphview.js` + `kairo.css` in headless chromium under reduced
motion), `analyze_overlap` across **4 states × 3 themes × 3 viewports**:

| State | noir | light | neon |
|---|---|---|---|
| `focus` (project neighborhood, depth 1) | ✅ 1440/1024/390 | ✅ 1440/1024/390 | ✅ 1440/1024/390 |
| `expanded` (depth 2) | ✅ 1440/1024/390 | ✅ 1440/1024/390 | ✅ 1440/1024/390 |
| `filtered` (kind subset) | ✅ 1440/1024/390 | ✅ 1440/1024/390 | ✅ 1440/1024/390 |
| `empty` (calm "nothing to graph yet") | ✅ 1440/1024/390 | ✅ 1440/1024/390 | ✅ 1440/1024/390 |

Zero layout violations across all 36. Spot-checked visually: the expanded/noir/1440 graph (run
centered by degree, the untrusted `http://x` source drawn with its distinct dashed trust ring, kind-
count filter chips, side inspect panel) and the empty/light state (the calm empty message + `jarvis
graph rebuild` hint).

### Safety walls (each with its named test)

| Wall | Test |
|---|---|
| Mutation-route closed set = **37** (only +2 Vault-pattern review routes) | `test_ui_readmodels::test_mutation_route_closed_set` |
| Graph UI read/navigate-only; saved views localStorage-only | `test_graph_tab`, `test_workspace_ui` |
| Suggestions quarantined; no auto-approve path | `test_graph_suggest`, `test_graph_review` |
| Hostile payload stays quarantined + untrusted; approval never upgrades trust | `test_graph_adversarial` |
| Trust = worst-of-evidence, never upgraded | `test_graph_suggest`, `test_graph_review` |
| Rebuild deterministic + rerunnable (source-row times; asserted survives) | `test_graph_builder` |
| Unified search quarantine-aware + fail-closed unpriced + ledgered | `test_graph_search`, `test_graph_index` |
| No graph content reaches gate/tools/prompts (zero refs in tools/ + orchestration/) | structural |
| Secret sweep over every new GET | `test_graph_routes::test_graph_routes_leak_no_secret_or_body` |
| Merge/split reversible, never-DELETE, derived untouched, survives rebuild | `test_graph_merge` |
| Obsidian export deterministic / non-destructive / private-excluded / redacted | `test_graph_obsidian` |

## B. Live ritual (run in an interactive session with keys — not yet executed)

Each step is safe (read/reason-only + local writes gated to the wiki tree); costs are bounded and
land in the ledger.

1. **Deterministic rebuild on the real DB.** `jarvis graph rebuild` twice — the derived-edge counts
   match exactly (source-row timestamps ⇒ byte-identical), and asserted rows are untouched.
2. **One real suggest → review → approve cycle.** `jarvis graph suggest --project <P>` (a budgeted
   utility call; proposals land PENDING), `jarvis graph review --project <P>` (lists the queue),
   `jarvis graph review --approve <ID>` — confirm the approved memory/node appears with its trust
   **unchanged** (an untrusted-sourced proposal stays `untrusted_external`), and the pending ones
   never appeared in search until approved.
3. **Unified search spot checks.** In the workstation, query a term that exists as an entity + in a
   chat + in a memory: confirm merged, badged, quarantine-clean results; pull the network to confirm
   the FTS-only degrade (no crash) and that an unpriced embedder is refused.
4. **Graph tab on real data.** Open `#workspace/<P>/graph`: confirm it is calm at scale (node cap,
   filters, focus/expand), that clicking a node inspects it (GET), and that nothing mutates.
5. **Obsidian export.** `jarvis graph export` (dry-run — read the plan), then `jarvis graph export
   --write`; open `knowledge/wiki/` in Obsidian and confirm the `_graph/` + `_memory/` pages render
   and cross-link, that a hand-authored page at a `_graph/` path is skipped (not clobbered), and that
   no secret/private content is present.
6. **Embedding spend under cap.** `jarvis graph reindex --dry-run` (reports what would embed + est.
   spend), then `jarvis graph reindex`; confirm the Voyage spend appears in the cost ledger and stays
   under the configured cap (unpriced ⇒ refused).
7. **Adversarial suggestion live proof.** Live-run `inj_graph_suggestion_poison` (the injection-proof
   ritual, `--suite adversarial` or the live-chunked runner): confirm the assistant reports the real
   decision, does NOT exfiltrate to `attacker.test`, and does NOT save the directive as memory.
8. **Record the two positive-path core cassettes** (then they replay $0 like the rest of core). Author
   these two scenarios at the top level of `tests/evals/scenarios/` and record with the frozen-clock
   harness, e.g. `uv run python tests/evals/runner.py record --suite core --scenario
   memory_suggestion_quarantine` (and `graph_search_grounded`), committing the scenario + its cassette
   together so the keyless gate stays green:

   - **`memory_suggestion_quarantine`** — untrusted web content proposes a memory; assert it is NOT
     retrievable in a following session until approved, and IS after.
   - **`graph_search_grounded`** — a question answered via unified search with a correct citation.

   (They are documented here rather than committed uncassetted, because an uncassetted core scenario
   would redden the keyless replay gate. Record + commit them during this ritual.)

## Orthogonal: adversarial replay is red because cassettes are MISSING (unchanged from Phase 14)

`jarvis eval gate --suite adversarial` (and `--suite all`) remains red at HEAD — now **24** adversarial
scenarios (the Phase-15 `inj_graph_suggestion_poison` included) MISS at call #1 because **no
adversarial cassettes were ever recorded/committed**. This is a missing-baseline condition, not a
behavior failure. **`--suite core` (19, keyless, $0) stays the per-task gate.** Follow-up (out of
Phase-15 scope, either is fine): record + commit the adversarial cassettes once, or keep adversarial
as a live-only ritual (step B7).

## Cleanup / not done (deliberate)

Task 12 is docs-only (this file + ADR-0021 + the README Status entry) — no behavior change, no
migration, no baseline ratchet. Deferred by design (ADR-0021 §Consequences): two-way live Obsidian
sync, connector-store people mining, a global cross-project graph screen, community detection,
merge/split UI routes, and graph-conditioned agent retrieval (a Phase-16 attention question). The two
positive-path core cassettes (B8) are the only piece requiring a live session.
