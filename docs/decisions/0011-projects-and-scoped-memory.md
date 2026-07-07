# ADR-0011: Projects are the scope unit; scope is enforced in SQL, never by the UI

- **Status:** Accepted
- **Date:** 2026-07-07
- **Context phase:** Phase 10A (project workspaces)

## Context

Before Phase 10 everything lived in one global pool: one memory bucket, one KB, chats that
the UI never even persisted. "Project workspaces" makes a project the first-class unit that
owns chats, memory, tasks, KB sources, and settings. The safety-critical requirement (the
user's amendment A1) is hard isolation: **Project A must never retrieve Project B's memory
or KB content** — and that guarantee cannot depend on the UI offering the right filter,
because a crafted request bypasses the UI.

## Decision

### 1. A nullable `project_id` on every scoped table; NULL == global

Migration v7 adds `projects` + a nullable `project_id` foreign key to `sessions`, `memories`,
`tasks`, `kb_sources`, `digests`, `agent_runs` (and `model_calls`/`orchestration_runs`).
Purely additive — no CHECK is widened, so it is a plain SQL migration, not a table rebuild.
Every pre-Phase-10 row keeps `project_id = NULL` and stays global with zero backfill. Global
knowledge is visible everywhere; a project's rows are visible only in that project.

### 2. Scope is a SQL predicate, applied before scoring — enforced server-side

`MemoryStore.search`, `KnowledgeStore.search` (source-level), and `TaskStore.list` take a
scope with one shape:

- **active project P** → `(project_id = P OR project_id IS NULL)` — P's own rows plus global.
- **global scope** → `project_id IS NULL` only — a global chat must never surface a project's
  rows (closes the leak where "no project active" would otherwise recall everything).
- **unscoped (a sentinel)** → no filter, for admin/REPL views and byte-identical legacy paths.

The filter runs in the SQL `WHERE`, so the numpy similarity matmul only ever sees in-scope
rows. The UI passes a `project_id`, but the backend is the enforcement point; a request naming
a foreign id simply returns nothing for it.

### 3. Dedup uses EXACT scope, never the recall union

`MemoryService.remember`'s nearest-neighbour search is scoped `project_id = P` *exactly*
(`include_global=False`) — deliberately narrower than recall's `P OR NULL` union. If dedup used
the union, a new project memory could "supersede" a global memory (or the reverse), silently
retiring it: cross-scope data loss. Exact-scope dedup means a project write can only ever
supersede within its own scope.

### 4. A session belongs to one project for its life; reflection follows the session

Switching project starts a **new** session (the REPL `project use` command and the UI
`/api/projects/select` both reset the conversation). Reflection reads the session's
`project_id` and attributes extracted memories to it — so a project chat's memories are
project-scoped, and a global chat's are global. This is why mid-conversation switching can't
launder one project's content into another's memory.

### 5. KB scope is source-level; wiki pages stay global

`kb_sources` carry `project_id`; retrieval filters candidate chunks by their source's scope.
Curated wiki pages (no source) remain visible in every scope — they are Jarvis-authored, not
raw project uploads. Chunk-level project scoping is a documented follow-up; the source-level
filter is the smallest change that blocks cross-project *source* retrieval (A1).

### 6. Export/import is a human ritual that ignores inbound provenance

`jarvis project export|import` round-trips a project's memories to Markdown (reusing the wiki
front-matter machinery + path jail). Import **forces** `project_id = target` and
`source = 'import'` (untrusted) and ignores any `project_id`/`source` a file claims — a
hand-crafted file cannot launder text into a trusted or cross-project memory. Never reachable
by a tool or agent.

## Consequences

- Isolation is a structural property of the query layer, proven by an adversarial no-leak
  suite (recall / dedup / reflection / KB retrieval / import), not a UI convention.
- `project_id` foreign keys are enforced: a scoped row cannot reference a nonexistent project
  (surfaced repeatedly as FK errors in tests that forgot to create the project first — the
  integrity is real).
- The three `AgentLoop` construction sites (REPL, UI, voice) each take a `project` provider
  read once per turn, so switching applies from the next turn, never mid-turn.
- Voice inherits the process's active project with a global fallback and announces it at turn
  start (ADR context for A3); it never *sets* a project — screen selection commits scope.
