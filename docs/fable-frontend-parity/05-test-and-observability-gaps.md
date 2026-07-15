# 05 — Test and Observability Gaps

What is pinned today, what is not, and the smallest suite that keeps frontend/backend drift from returning. Baseline facts: mutation-route closed set pinned at **47** (tests/unit/test_ui_readmodels.py:136-209); whole-GET secret sweep (…:215-293); strong WS scoping tests (test_ui_workspaces.py:293-488, test_ui_scoped_events.py:74-138); screenshot DoD harnesses are standalone Playwright scripts, never collected by pytest (tests/ui/*.py).

## 1. Contract-test gaps (server ↔ browser)

| Gap | Evidence | Smallest fix |
|---|---|---|
| **No route-consumption pin.** Nothing fails when a route loses (or never had) a frontend consumer — that's how 16 routes went orphan silently. | verified by grep this session (01 §14) | **T1 `test_route_consumption_manifest`**: static-scan `src/kira/ui/static/**/*.js` for `/api/...` string literals; assert every server route is either (a) referenced, or (b) listed in an explicit `EXEMPT = {...}` literal with a one-line reason. New orphans then require a conscious exemption in the diff. (~60 lines; pure stdlib + app.routes; same pattern as the mutation pin.) |
| **Untested WS events**: `turn_cancelled`, `turn_error` (session.py:331,335), `effort_changed` (server.py:599) — code-only; `approval_nonce` envelope DoD-only (workbench_dod.py:788) | agent E §4 | **T2 `test_ws_event_contract`**: extend test_ui_session/test_ui_workspaces to drive cancel/error paths and `POST /api/effort`, asserting envelope kind + payload keys; assert `approval_nonce` frame after a simulated `approval_shown`. |
| **`GET /api/search` shape unpinned** (about to become the palette's backbone) | server.py:1960; persistence/fts.py | **T3 `test_federated_search_contract`**: seed one row per FTS domain; assert result kinds/fields; pin the kind vocabulary the palette's KIND_ROUTE map depends on (palette.js:34-45). |
| **`POST /api/effort` endpoint untested** (only in the pin literal) | agent E §2 | covered by T2. |
| **`/api/voice/status` shape unpinned** (chat.js:94 defaults `mode`) | agent B §8 | one assertion block in existing voice tests. |
| **Scoping matrix untested per-route**: locked vs trusted GETs diverge silently (server.py:2017 vs 2027) | matrix C | **T4 `test_project_scope_matrix`**: parametrized over every project-dimensioned GET: bound workspace + foreign project_id ⇒ expected (404 or documented-trusted). Forces each route to declare its isolation model; catches future mixed-model regressions. |
| **capability_truth cross-route consistency untested** (the P0-1 defect had no pin to fail) | readmodels.py:1348-1350 | **T5 `test_capability_truth_agrees`**: same composition, fetch daily/capabilities/hub/settings, assert identical `exposed_to_chat` per connector. |

## 2. UI-layer blind spots

| Gap | Evidence | Smallest fix |
|---|---|---|
| **Notifications screen (gate.js) has zero coverage of any kind** — no pytest reads its structure, no DoD state renders it | agent E §6 | add a `notifications` state to workbench_dod.py STATES (seed `/api/attention` + `/api/intents`); plus one pytest asserting resolve/approve dispatch to distinct routes (source-route vs resolve-route) |
| DoD harnesses never run in CI (not `test_*`, Playwright-gated) | agent E §2 | keep manual per repo policy (ADR-0005 live-ritual discipline), but add **T6 `test_dod_states_parse`**: import each `*_dod.py`, assert STATES lists are well-formed and seed routes exist — catches DoD rot without a browser |
| Heartbeat/reconnect behavior untested (interval leak, app.js:135) | matrix E | after P1-7 fix, a small jsdom-free check is impractical here; cover by code review + comment; optionally count timers in workbench_dod harness |
| Frontend event-handler drift (server emits kind X, app.js switch lacks a case) | `subagent_*` precedent | **T7 `test_event_kinds_handled`**: assert the set of `kind`/`type` strings emitted server-side (session.py:81-142, server.py, approver.py, voice.py, engine sink) ⊆ string literals present in app.js/conversation.js — a grep-level pin, crude but effective against silently-dropped events |

## 3. Observability gaps (backend has truth, nothing can see it)

| Gap | Evidence | Smallest fix |
|---|---|---|
| `connector_writes` journal (what actually left the box) has **no read model** | migrations v10; agent D table | small `connector_writes_view` read model + section in Notifications; metadata-only like serialize_intent (readmodels.py:1677) |
| Egress log is log-file-only (no table, no UI) | egress.py:23 | defer (product decision — repo treats egress ledger as logs deliberately, ADR-0009); document in Settings copy |
| `orchestration_runs.skills_manifest_json` / `context_manifest_json` written, never surfaced | store writes v19; serializer omits (readmodels.py:790-816) | when skills leave `off`: add pack id/version chips to run detail (bodies-free) |
| Ephemeral `POST /api/budgets` gives no signal it's non-durable | server.py:507-510 | response field `durable:false` + UI note, or remove (04 P2-6) |
| No UI signal for compaction or `cost_cap`/`max_context` stop reasons beyond a bare note | agent D §10 | render stop_reason chips in the thread (strings already in `turn_completed` events) |

## 4. The smallest drift-prevention suite (recommended, in order)

1. **T1 route-consumption manifest** — kills the orphan-route class permanently (the single highest-value test in this report).
2. **T4 project-scope matrix** — freezes each route's isolation model; catches the next `/api/workspace/{id}`-style omission at review time.
3. **T5 capability-truth agreement** — pins the promise the read model already documents.
4. **T2 WS event contract (cancel/error/effort/nonce)** — completes the event table; cheap extensions of existing test files.
5. **T7 event-kinds-handled grep pin** — prevents delivered-but-dropped events (the `subagent_*` failure mode).
6. **T3 federated-search contract** — lands with P1-2, not before.
7. **T6 DoD-states parse check + a `notifications` DoD state** — keeps the manual layer from rotting and gives the one untested screen a rendering.

All seven are keyless, replay-free, and fit the repo's existing test idioms (route-set literal pins, ASGI TestClient WS, seeded stores). Estimated total: ~500 lines of test code, no new dependencies, no live spend.
