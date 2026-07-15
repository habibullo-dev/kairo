# Jarvis Phase 3 — Tasks & Scheduling

*(The approved Phase 3 design. Follows master plan `docs/PLAN.md` §2 row 3 — "Task store, reminders, background jobs that wake the agent — agents that act without being prompted" — designed with an adversarial pre-mortem pass, same discipline as `docs/PLAN-2-memory.md`.)*

## Context

Phases 1–2 are complete (244 tests, N=3 eval gate 5/5): a streaming REPL agent with tools, permissions, persistence, compaction, and long-term memory. But Jarvis only acts when spoken to. Phase 3 adds the task store, reminders, and background jobs — and with it the genuinely new problem of this phase: **an agent acting with no human present**. Most of this design is about making that safe.

Two task kinds, deliberately distinct:

1. **Reminder** — text delivered to the human at a time. **No model call.** Deterministic, free, instant.
2. **Job** — a stored prompt run as a synthetic turn through the *same* `AgentLoop`, in a **fresh session**, unattended. Persistence, compaction, and audit all work because it *is* a normal turn — but under a stricter permission regime (below).

Quality-first: jobs run on `claude-opus-4-8` with full retries — a background run is not a second-class run.

## Architecture (new pieces in bold)

```
cli/repl.py ───────────────┬─ `tasks` commands, startup catch-up, patch_stdout, shutdown seq
     │                     │
     │   asyncio turn lock (one model turn / terminal writer at a time)
     ▼                     ▼
core/agent.py ◀── **scheduler/runner.py**  BackgroundRunner: wake loop → check_due()
     │                     │    ├─ reminder → notify line + run row
     │                     │    └─ job → fresh session, framed prompt, headless approver
     │                     ▼
     │            **scheduler/service.py**  TaskService: schedule/cancel/due/complete-run,
     │                     │        misfire + failure-cap policy; fully clock-injected
     │                     ├─ **scheduler/triggers.py**  APScheduler triggers wrapped as
     │                     │        pure validate()/compute_next() — no scheduler object
     ├─ tools: **tools/builtin/tasks.py**  schedule_task / list_tasks / cancel_task
     ├─ **permissions/unattended.py**  UnattendedGate (demotes interactive grants)
     │                     ▼
     │            **scheduler/store.py**  TaskStore — schema v3, SAME shared connection
     ▼
persistence/ ── append (3, _SCHEMA_V3); **write lock + transaction() helper**; sessions.kind
```

Phase 1/2 seams throughout: tools reach `TaskService` via a new `ToolContext.tasks` field (the `memory` pattern); tools register only when the service exists (`Tool.is_available`); the runner is an optional collaborator — `scheduler.enabled: false` ⇒ byte-identical Phase 2 behavior (pinned by test).

## 1. Resolved design decisions

### D1 — APScheduler as a trigger library, not as the scheduler

Use only APScheduler's **triggers** (`CronTrigger.from_crontab`, `IntervalTrigger`, `DateTrigger`) for next-fire computation, wrapped in one pure function. Do **not** run `AsyncIOScheduler`. Our own ~40-line asyncio wake loop owns firing; `tasks.next_run_at` in SQLite is the only source of truth.

- **Testability**: `compute_next(kind, spec, tz, after) → datetime|None` is pure and table-testable; `TaskService` takes an injected clock — the whole lifecycle unit-tests with zero sleeps.
- **One source of truth**: `AsyncIOScheduler` + jobstore means re-registering jobs at startup and keeping two states consistent forever (and its persistent jobstores are pickle — a non-starter). Triggers-only leaves nothing to keep consistent.
- **Learning goals**: the wake loop *is* the concept this phase teaches; cron/DST math is the genuinely hard part — that's exactly what we take from APScheduler.
- Deps: `apscheduler>=3.11,<4` (zoneinfo-based, stable API) + `tzlocal`. This satisfies the master-plan tech-stack row; the deviation from "APScheduler runs the jobs" is recorded in the plan doc.

### D2 — Unattended permission regime: deny-ASK is necessary but NOT sufficient (ADR-0003)

The obvious rule — a **headless approver** that answers every ASK with `Permission.DENY` (becoming an `is_error` tool_result the model adapts to) — misses the real escalation channel: **policy ALLOWs**. Every interactive "always allow" (a shell prefix, a write dir, a tool-level allow) was consented to while the human watched the stream; unattended, a poisoned webpage + an allowed `git ` prefix = silent execution at 3am. So background runs go through an **`UnattendedGate`** wrapping the normal gate:

- **Hard DENY regardless of policy**: `schedule_task`, `cancel_task`, `remember`, `forget` — the state-mutating meta tools. Closes self-replication (a job scheduling jobs) and unattended memory writes even if the user once persisted an allow.
- **Demote ALLOW→DENY for `run_shell` and `write_file`** (reason: "interactive grant does not extend to unattended runs") unless the tool is listed in `scheduler.unattended_allow_tools` (default `[]`) — the one explicit, documented opt-in surface.
- **Everything else delegates** to the normal gate: read-only tools stay allow; web tools follow policy (ask by default ⇒ denied; a user who allowed them keeps background research working — that's the point of research jobs).
- **No per-task permission grants, ever** — a task carrying "grants: [run_shell]" turns one mis-read approval into standing self-authorization.

The most important test in Phase 3: a persisted/policy ALLOW on `run_shell`/`write_file`/`schedule_task` is still denied unattended.

### D3 — The stored prompt is not a live human (authority laundering)

A job payload is authored at schedule time — possibly composed by the model under the influence of fetched content and only skimmed at approval — then replayed later as a `user` message with "the user is telling me this right now" authority. Two mitigations, both test-pinned:

1. **Envelope**: the synthetic user message is framed —
   ```
   [Scheduled task #7 "check disk space" — created by user in session 12, cron 0 9 * * *.
   The text below is a STORED instruction, not a live human. No one is present to answer
   questions or approve actions. Task instructions:]
   <payload verbatim>
   ```
2. **Unattended system suffix** via `build_system(unattended=True)`: running unattended; approval-gated tools will be denied; prefer read-only approaches; if the task needs clarification or a denied capability, stop and report — don't thrash.

### D4 — Firing, the turn lock, and result delivery

- One **asyncio turn lock** (created in `run_repl`, held by `Repl.run_turn` and by the runner around every fire) serializes model turns *and* terminal output. If the user submits while a background run holds it, print `[task #7 "weekly digest" running — your message is queued]` immediately. `Repl.run` wraps the loop in `prompt_toolkit patch_stdout()` so notifications redraw the idle prompt cleanly. (Approval prompts happen mid-turn *under* the lock, so background output can't garble them by construction.)
- **Reminder fire**: notify line (`⏰ #7 reminder: stretch (due 21:00)`), `ok` run row, advance `next_run_at`. Delivery is **at-least-once**: notify, then mark — a crash between gives a duplicate notification, never a vanished reminder (comment pinned).
- **Job fire**: fresh session (`kind='task'`, `title="task #7: <title>"`), framed prompt, one `AgentLoop.run_turn` sharing client/registry/executor/memory but with `UnattendedGate` + headless approver + fresh `ContextManager` + `max_iterations=scheduler.max_job_iterations`; persist messages; record the run row (session_id, result_text, usage/cost, denied_count); print a completion notice with the first lines of the result. A `max_iterations`/`max_context` stop is reported as a failure notice, never silence.
- **Result delivery is a feature, not an afterthought**: `task_runs.result_text` stores the final text (truncated ~10k chars); the REPL `tasks` command lists tasks (id, kind, title, schedule, next run in local time, status, last error), `tasks <id>` shows run history (when, outcome, cost, denied count, result, session id). Every listing shows `created_by` provenance — a surprising task is traceable, like memories.
- **Coalescing by construction**: `due()` excludes tasks with an unfinished run; the single lock means max one background run in flight. No `max_concurrent` knob — the system deliberately supports exactly one.

### D5 — Missed jobs & crash recovery

On startup and every wake, for `status='active'` with `next_run_at <= now`:

| | within `misfire_grace_seconds` (3600) | beyond grace |
|---|---|---|
| **reminder** | fire (annotate `MISSED (was Tue 15:00)`) | **still fire, annotated** — late beats silent |
| **job** | fire normally | record **one** `missed` run row (never one per skipped cron slot); recurring → `next_run_at` from now; once → task `status='missed'` |

**Half-run is never silently retried**: at startup, any run row with `started_at` and no `finished_at` → outcome `aborted`, and the task is advanced *past* that occurrence (its side effects may have completed before the crash — the email may have sent). Notify; never auto-re-run. Test-pinned.

`once` tasks with a past time at creation are **rejected** at the tool (error includes the current local time so the model self-corrects a timezone slip), with a ≤2-minute tolerance that runs now — models routinely compute wrong-tz datetimes, and "fire immediately" would turn a tz bug into an instant execution the human approved thinking it was for tomorrow.

### D6 — Recurrence, failure cap, drift

After each run: `next_run_at = compute_next(..., after=scheduled_fire_time)` — from the *scheduled* time, not completion, so intervals don't drift by run duration. Once → `done`. On error: `consecutive_failures += 1`, `last_error` set; at `max_consecutive_failures` (3) the task flips `failed` + announced — a broken recurring job must not silently burn a model call per interval forever. Success resets the counter.

### D6a — Expected final-output verification (bounded, not a side-effect proof)

A scheduled **job** may opt into one exact output contract: `verify_contains`, a bounded list
of up to eight literal phrases. After a clean model completion, the runner compares the final
answer case-insensitively against every phrase. It records a per-run verdict of `passed`,
`failed`, or `not_run` alongside a cardinality-only summary such as “missing 1 of 2 phrases.”

- The contract is explicit user-reviewed task data, stored separately from the job prompt and
  shown in the scheduling confirmation and task history.
- A failed check turns the run into a non-retryable error. The job may already have performed a
  side effect, so retrying merely to improve its final wording could duplicate work.
- A missed, crashed, interrupted, or owner-rejected run is `not_run`; it is never mislabeled a
  pass. Existing jobs remain `not_configured`.
- This is intentionally **not** a general assertion engine: no shell commands, filesystem checks,
  regexes, arbitrary expressions, or model-as-judge calls. It verifies only final answer text and
  never proves that an external action occurred.

### D7 — Timezones

All stored timestamps UTC ISO-8601 (existing `_now()` convention — lexicographic `WHERE next_run_at <= ?` stays correct). Cron is human intent: each task stores an IANA `timezone` captured at creation (local via `tzlocal`); `compute_next` evaluates in that zone, returns UTC. One regression test crosses a DST boundary (`America/New_York` spring-forward).

## 2. Data model — schema v3 (`persistence/migrations.py`)

Same discipline as `memories`: CHECK constraints, status lifecycle, provenance, **nothing ever DELETEd**. Two status machines, deliberately split: `tasks.status` is *lifecycle*, `task_runs.status` is *per-execution* — conflating them makes orphan recovery and recurrence ambiguous.

```sql
CREATE TABLE tasks (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    kind                 TEXT NOT NULL CHECK (kind IN ('reminder','job')),
    title                TEXT NOT NULL,
    payload              TEXT NOT NULL,      -- reminder text | job prompt, verbatim
    schedule_kind        TEXT NOT NULL CHECK (schedule_kind IN ('once','cron','interval')),
    schedule_spec        TEXT NOT NULL,      -- once: ISO-8601; cron: 5-field; interval: seconds
    timezone             TEXT NOT NULL,      -- IANA zone cron is evaluated in
    next_run_at          TEXT,               -- UTC ISO; NULL iff not active
    status               TEXT NOT NULL DEFAULT 'active'
                         CHECK (status IN ('active','done','cancelled','failed','missed')),
    created_by           TEXT NOT NULL CHECK (created_by IN ('user','agent')),
    source_session_id    INTEGER REFERENCES sessions(id) ON DELETE SET NULL,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    last_run_at          TEXT,
    last_error           TEXT,
    created_at           TEXT NOT NULL,
    updated_at           TEXT NOT NULL,
    CHECK (status = 'active' OR next_run_at IS NULL)   -- terminal states never look due
);
CREATE INDEX idx_tasks_due ON tasks(next_run_at) WHERE status = 'active';

CREATE TABLE task_runs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id       INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    scheduled_for TEXT NOT NULL,             -- fire time this run serviced (UTC ISO)
    started_at    TEXT,
    finished_at   TEXT,
    status        TEXT NOT NULL CHECK (status IN ('running','ok','error','missed','aborted')),
    session_id    INTEGER REFERENCES sessions(id) ON DELETE SET NULL,  -- job transcript
    result_text   TEXT,                      -- final text (truncated ~10k) / delivery note
    denied_count  INTEGER NOT NULL DEFAULT 0,-- ASK→DENY / demotion events during the run
    error         TEXT,
    cost_usd      REAL,
    created_at    TEXT NOT NULL
);
CREATE INDEX idx_task_runs_task ON task_runs(task_id, id);

ALTER TABLE sessions ADD COLUMN kind TEXT NOT NULL DEFAULT 'interactive'
    CHECK (kind IN ('interactive','task'));
```

**`sessions.kind` is the most load-bearing line in the migration.** Without it: (a) `latest_session_id()` (ORDER BY updated_at) makes `--resume` land the user *inside a background job's transcript*; (b) reflection catch-up learns from unattended sessions — a standing poisoning pipeline (web content → assistant paraphrase → permanent memory, no human ever in the loop; the tool_result-stripping firewall does NOT cover the assistant's own text). Fixes: `latest_session_id()` filters `kind='interactive'`; `unreflected_session_ids`/`needs_reflection` skip `kind='task'` unless `scheduler.reflect_job_sessions: true` (default **false**).

## 3. Persistence hardening — write lock + `transaction()`

Phase 3 introduces the first real write concurrency on the **single shared aiosqlite connection**. `save_messages` is DELETE → INSERT → UPDATE → commit across four awaits; an interleaved write from another coroutine joins the same implicit transaction, and either coroutine's `commit()` commits the other's half-done work — crash at the wrong moment and a session's entire history is gone. Correctness must live in the persistence layer, not in call-site discipline:

- One shared `asyncio.Lock` created alongside the connection; `persistence/db.py` gains an `async with transaction(db, lock):` helper (`BEGIN IMMEDIATE` … COMMIT/ROLLBACK under the lock). All store constructors accept the shared lock (each creates a private one when standalone, e.g. in tests).
- Every multi-statement write routes through it: `save_messages`, finish-run bookkeeping (run outcome + `next_run_at` + failure counters — **one transaction**, else a crash between commits double-runs or silently skips an occurrence), fire-time bookkeeping. Single-statement writes acquire the lock too (so they can't land inside someone else's open transaction).
- Pinned: two concurrent `save_messages` for different sessions both survive; kill injected between finish-run statements leaves consistent state.

The turn lock (D4) remains — but as UX policy (one thing talks to the terminal), not the correctness mechanism.

## 4. Module design (`src/kira/scheduler/`)

- **`store.py` — `TaskStore(db, lock)`** (+ frozen `Task`, `TaskRun` dataclasses): `add(...) -> int`, `get`, `list(include_finished=False)`, `due(now_iso)` (active, due, no unfinished run, ordered), `set_next_run`, `set_status`, `record_failure -> int`, `reset_failures`, `start_run(task_id, scheduled_for) -> run_id`, `finish_run(run_id, status, *, session_id, result_text, denied_count, error, cost_usd)` (atomic with task advancement via the service), `runs_for(task_id, limit=20)`, `sweep_stale_runs()` (orphaned `running` → `aborted`, advance the task). Module docstring repeats the shared-connection warning like `memory/store.py`.
- **`triggers.py`** — the only file importing APScheduler: `validate(schedule_kind, spec, tz) -> str | None` (human-readable error) and `compute_next(schedule_kind, spec, tz, *, after) -> datetime | None` (aware-UTC in/out; interval floor **60s**; once returns the instant if ahead of `after`, else None).
- **`service.py` — `TaskService(store, config.scheduler, *, now=utc_now)`** — semantics, fully clock-injected: `schedule(...)` (validates; rejects past once-times with current-local-time in the error; ≤2 min tolerance), `due() -> list[Due]` classifying `fire | fire_late | missed` per D5, `begin_run`/`complete_run` (D6), `cancel`, `describe(task)` (human schedule + next fire in local time — reused by tool results and the `tasks` command), `bound_session_id` for tool-created provenance.
- **`runner.py` — `BackgroundRunner(service, *, notify, run_job, turn_lock, log)`**: `check_due() -> int` is the testable core — classify, then under the turn lock fire each (reminder → notify + rows; job → `run_job(task) -> JobOutcome(session_id, text, usage, denied_count, error)`), apply outcomes. **No sleeping in this method.** `start()`/`stop()` run the thin loop: wait on an event with `timeout=min(seconds_until_next_due, wake_cap_seconds)` then `check_due()` — the 30s cap bounds drift across laptop suspend (asyncio timers don't tick through system sleep); `kick()` wakes it when a task is scheduled so new tasks fire promptly.
- `run_job` is a closure in `run_repl`: fresh `kind='task'` session, framed prompt (D3), `AgentLoop` with `UnattendedGate` + headless approver + `build_system(..., unattended=True)` + fresh `ContextManager` + capped iterations; persists messages; returns the outcome. `bind_trace()` per run for audit correlation.

## 5. Tools (`tools/builtin/tasks.py`) + prompts + permissions

`_NeedsTasks` mixin (`is_available` ⇔ `context.tasks is not None`) — scheduler off ⇒ tools absent, prompt unchanged.

| tool | params | default |
|---|---|---|
| `schedule_task` | `kind: 'reminder'\|'job'`, `title`, `payload`, exactly one of `once_at: str` / `cron: str` / `every_seconds: int (≥60)` | **ask** |
| `list_tasks` | `include_finished: bool = False` | **allow** |
| `cancel_task` | `task_id: int` | **ask** (the model must not silence your reminders) |

**Why `schedule_task` asks (non-negotiable):** it is a *deferred-execution* injection sink — strictly worse than Phase 2's `remember`, because the payload eventually **runs with tools**. The approval prompt (`_call_summary`) shows the **full untruncated payload + kind + schedule + computed first fire time in local terms** ("fires 2026-07-07 09:00 local, in 11h 23m") — a human can't catch a hidden instruction at char 900 of a truncated preview, nor a wrong-timezone datetime without the relative delta. **`schedule_task` and `cancel_task` are excluded from "always allow" persistence in `_persist_always`** (per-instance approval only) — one "a" keystroke must not permanently open deferred execution. Success results echo `describe(task)` so the model confirms its time math.

`prompts.py`: `build_system` gains `tasks_enabled` (short guidance: what tasks are for, payloads must be **self-contained** — no one is present to answer questions later; times are local; the user approves every schedule) and `unattended` (D3 block). `AgentLoop._system_with_extras` appends a volatile `Current local time: <ISO>` line (clock injected; sorts last, after recall, preserving cache-stability ordering) — scheduling is impossible if the model must guess the date.

`config/permissions.yaml`: `schedule_task: ask`, `list_tasks: allow`, `cancel_task: ask` + injection-rationale comment.

## 6. Config (`config.py` + `settings.yaml`)

```yaml
scheduler:
  enabled: true
  misfire_grace_seconds: 3600    # due-jobs older than this on catch-up are 'missed', not run
  max_consecutive_failures: 3    # recurring job flips 'failed' after this many errors
  wake_cap_seconds: 30           # loop re-checks at least this often (survives laptop sleep)
  max_job_iterations: 15         # unattended runaway bound (a denied-everything loop must not thrash 25×)
  reflect_job_sessions: false    # unattended transcripts do NOT feed long-term memory by default
  unattended_allow_tools: []     # explicit opt-in: tools whose policy-ALLOW survives demotion (D2)
```

`SchedulerConfig(BaseModel)` with those defaults; `Config` gains `scheduler: SchedulerConfig = Field(default_factory=SchedulerConfig)`.

## 7. REPL integration (`cli/repl.py`)

- `run_repl`: build `TaskStore`/`TaskService` on the shared connection+lock; `sweep_stale_runs()`; one startup `check_due()` (prints `2 fired on catch-up, 1 missed (see 'tasks')`); construct `Repl` with the turn lock + `tasks`; `ToolContext(config, memory, tasks)`; `runner.start()` after the banner. **Shutdown sequence** (replaces the bare `finally`): stop the wake loop (no new fires) → if a run is in flight, prompt `background task #7 running: [w]ait / [a]bort` (abort = cancel, mark run `aborted` in one transaction) → reflect → close db. Pinned by test.
- `Repl`: `run_turn` acquires the turn lock (with the queued-notice if held); prompt loop wrapped in `patch_stdout()`; commands `tasks` and `tasks <id>`; `_call_summary` branches for `schedule_task` (full payload) / `cancel_task`; `_persist_always` exclusion list.

## 8. Task list — Milestone 3 (for Opus 4.8, in order)

Same discipline as Milestones 1–2: each task ends green (`ruff check` + `pytest`), commits, appends 3–5 learning-note bullets. Tasks 1–9 fully keyless — firing is tested by driving `service`/`runner.check_due()` with a stepped fake clock, never by sleeping.

1. **Plan doc + scaffold**: commit this doc as `docs/PLAN-3-tasks.md`; deps `apscheduler>=3.11,<4` + `tzlocal`; `scheduler/` package; `SchedulerConfig` + settings block; `ToolContext.tasks`. Tests: config defaults + YAML override; ToolContext default None.
2. **Schema v3 + persistence hardening**: migration (tasks, task_runs, `sessions.kind`); shared write lock + `transaction()` helper; `latest_session_id`/`unreflected_session_ids`/`needs_reflection` kind-filtering. Tests: v2→v3 on a *populated* db preserves sessions/messages/memories; `--resume` isolation (a newer `kind='task'` session doesn't win `latest_session_id`); reflection isolation (task sessions skipped by default, included when config opts in); two concurrent `save_messages` both survive; CHECK constraints enforced (terminal ⇒ next_run_at NULL).
3. **TaskStore**: CRUD, `due()` (active + due + no unfinished run), run bookkeeping, `sweep_stale_runs`. Tests: due ordering/filtering; cancel is a status flip (row still fetchable — never DELETE); run round-trip; sweep flips orphaned `running`→`aborted` AND advances the task (the never-silently-retry pin); finish-run atomicity (injected failure between statements ⇒ consistent).
4. **Triggers**: `validate` + `compute_next`. Table-driven tests: cron next-fire incl. DST spring-forward; interval floor (59s rejected); once-in-past → None; naive vs offset-aware ISO; invalid specs → readable errors; aware-UTC returns.
5. **TaskService**: lifecycle with injected clock. Tests: first-fire computation; past-once rejected (error text contains current time) with ≤2 min run-now tolerance; stepped clock ⇒ `fire`; beyond grace ⇒ reminder `fire_late`, job `missed` (exactly one missed row for N skipped cron slots); `complete_run` advances from *scheduled* time (no interval drift), once→done; 3 errors ⇒ `failed` + next NULL; success resets; provenance lands.
6. **UnattendedGate + headless approver — HARD PREREQUISITE for task 7**: the D2 wrapper + the deny-and-count approver, committed green *before any BackgroundRunner code exists*. Tests — **the safety contract of the phase**: ASK ⇒ denied `is_error`; hard-deny list wins over a persisted `tools: {schedule_task: allow}`; a persisted shell prefix rule / write-allowlist dir is demoted unattended; `unattended_allow_tools` opt-in restores exactly the named tool; read-only tools still allow; the approver provably never reads stdin. No unattended run may inherit interactive shell/write/meta-tool grants by accident — these tests are what make that impossible rather than merely intended.
7. **BackgroundRunner + run_job** (requires task 6 committed): `check_due`, turn-lock serialization, framing, outcome application. `run_job` takes the gate as a required constructor argument — there is no code path that constructs an unattended `AgentLoop` with the interactive gate. Tests (FakeClient, zero sleeps): reminder ⇒ notify once + `ok` row + advanced next (at-least-once ordering pinned); job ⇒ fresh `kind='task'` session whose first user message contains the **envelope**, run row carries session_id/result_text/cost; scripted `tool_use(write_file)` ⇒ denial flows back as `is_error`, run ends `ok` with `denied_count==1`; a held turn lock delays firing; job exception ⇒ `error` + failure bookkeeping; missed job ⇒ no model call (FakeClient with no script would raise).
8. **Tools + prompts + permissions**: the three tools, `_NeedsTasks`, `build_system(tasks_enabled/unattended)`, per-turn local-time line, permissions.yaml entries, `_call_summary` + `_persist_always` exclusions. Tests: schedule→list→cancel through a real AgentLoop + FakeClient; exactly-one-schedule-field validation; scheduler disabled ⇒ no tools + Phase-2-identical system prompt (null-path pin); `_call_summary` contains the full payload + fire time; policy defaults asserted; "always" on schedule_task does NOT persist an allow.
9. **REPL wiring**: run_repl construction, startup sweep + catch-up, `tasks` commands, turn lock + queued notice, patch_stdout, shutdown sequence. Tests at Repl level (scripted store/console): commands render with provenance + last_error; startup catch-up fires a due reminder before the first prompt; disabled scheduler wires nothing; shutdown aborts an in-flight run cleanly.
10. **ADR-0003 + live evals + docs**: ADR-0003 "Unattended runs: ASK degrades to DENY, interactive grants don't extend, no per-task grants". Eval runner: scenario support for seeding tasks + a post-turn `check_due()` hook + a `task_run_matches` db check. Scenarios: (a) *schedule_via_tool* — "remind me to stretch every hour" ⇒ `tool_called: schedule_task`, db row kind=reminder, active; (b) *unattended_job_readonly* — seed a due job "read notes.txt, report the key number" ⇒ run `ok`, result matches; (c) *unattended_job_denied* — seed a due job requiring `write_file` ⇒ run `ok`, `denied_count ≥ 1`, result acknowledges the denial (ADR-0003, executable). README + architecture.md + learning notes.

## 9. Verification

1. `uv run pytest` — all green, keyless (fake clock + FakeClient; no sleeping tests).
2. `uv run ruff check` / `format --check` — clean.
3. Live reminder: "remind me in 2 minutes to stand up" → approval shows full payload + fire time → keep chatting → reminder line appears without corrupting the prompt → `tasks` shows the run.
4. Live unattended job: "schedule a job for one minute from now: list the repo files and note the largest" → approve → completion notice; `tasks <id>` shows result + cost; the job's session exists with `kind='task'`; `--resume` still returns the interactive session; startup does NOT reflect the job session.
5. Kill the process before a due time; restart within grace ⇒ job runs on catch-up; restart after ⇒ `missed` run row and the reminder still delivers late, annotated.
6. `uv run python tests/evals/runner.py` — all 5 prior scenarios still pass + 3 new scenarios, then the full N=3 gate.

## Non-negotiables (for the Opus handoff)

1. **`schedule_task` defaults to ask, full untruncated payload + computed fire time shown at approval, and it is excluded from "always allow"** — deferred execution is a stronger injection sink than `remember`.
2. **Unattended runs use the `UnattendedGate`: ASK ⇒ DENY, interactive ALLOW grants demoted for shell/write, hard-deny on task/memory-write meta tools, no per-task grants** (ADR-0003). The gate and its safety tests (task 6) are written and committed green **before any BackgroundRunner code is written** (task 7) — no unattended run may inherit interactive shell/write/meta-tool grants by accident, and the runner's constructor makes the unattended gate mandatory, not optional.
3. **`sessions.kind` lands in the same migration as the tasks table**: background sessions never hijack `--resume` and are not reflected into long-term memory by default.
4. **Nothing is ever DELETEd; the write lock + `transaction()` helper guard every multi-statement write**; a half-run (`started_at`, no `finished_at`) is never silently re-run.

## Known risks recorded, not solved here

- The REPL is a session process, not a daemon: tasks fire only while Jarvis runs. A future OS-level service/daemon mode is out of scope; D5's catch-up makes the in-process model predictable.
- `task_runs` grows forever (personal scale: fine). A `tasks prune`/retention cap is deferred until it matters.

## Model switch

After approval: switch to **Opus 4.8**, execute Milestone 3 tasks 1–10 under the Milestone 1 rules (`docs/PLAN.md` §9) plus the four non-negotiables above.
