# Jarvis — A Real Agentic Assistant, Built From Scratch

## Context

You (an advanced engineer, new to agent architectures) want to build a Jarvis-style agentic system in `C:\Users\habib\Desktop\jarvis` — not a toy chatbot. Goals: learn agent engineering deeply while producing a genuinely useful assistant that will eventually use tools, remember things, manage tasks, read files, research the web, speak/listen, and coordinate multiple agents.

Decisions already made:
- **Python**, agent core built **from scratch** on the raw Anthropic Messages API (maximum learning; no framework hiding the loop).
- **MVP interface: terminal REPL** (streaming, tool-call visibility, approval prompts). Web/voice come later.
- **API cost is a non-issue (company-provided). Every choice below optimizes for quality, never for price** — the most capable model at every step, including background/utility work. Token/cost tracking stays in the design purely as observability, not as something to optimize.
- Planning by Fable 5 (this document); implementation by **Opus 4.8** (handoff prompt at the end).

The core thesis of the design: **an agent is a loop, and everything else is infrastructure around that loop** — tools, memory, permissions, observability, evaluation. Each phase adds one infrastructure layer, so each phase is also one learning module.

---

## 1. Overall Architecture

Layered, with strict dependency direction (interfaces → core → services → persistence). Any interface (REPL, web, voice, Telegram) drives the same core through one session API.

```
┌─────────────────────────────────────────────────────────┐
│ INTERFACES        repl (MVP) │ web api │ voice │ bot     │
│                   render streams, prompt for approvals   │
├─────────────────────────────────────────────────────────┤
│ AGENT CORE                                               │
│  AgentLoop      the while-loop: model ⇄ tools           │
│  ContextManager token budget, compaction, memory inject  │
│  ModelRouter    opus for reasoning, haiku for cheap ops  │
│  Prompts        system prompt assembly (identity+memory) │
├─────────────────────────────────────────────────────────┤
│ SERVICES                                                 │
│  ToolRegistry + ToolExecutor   (schema, timeout, trunc)  │
│  PermissionGate                (allow / ask / deny)      │
│  MemoryService                 (remember, recall, reflect)│
│  TaskService + Scheduler       (phase 3)                 │
│  SubAgentService               (phase 6)                 │
├─────────────────────────────────────────────────────────┤
│ FOUNDATION                                               │
│  Persistence   SQLite: sessions, messages, memories,     │
│                tasks, audit log                          │
│  Observability structlog JSON events, trace ids,         │
│                token/cost accounting                     │
│  Config        pydantic-settings + yaml policies         │
└─────────────────────────────────────────────────────────┘
```

Key architectural rules (these are the learning payload — hold them firmly):
1. **The model is stateless.** All state (history, memory, tasks) lives in your persistence layer; every model call reconstructs context from it.
2. **Tools are data, not code, to the model.** The model only ever sees JSON schemas; the executor owns the actual side effects. This boundary is where safety lives.
3. **Every side effect passes through the PermissionGate and lands in the audit log.** No exceptions, including internal tool calls from sub-agents.
4. **Interfaces are thin.** The REPL knows how to render and ask for approval; it knows nothing about the loop internals. This is what makes web/voice cheap later.

---

## 2. Project Phases (learning roadmap)

Each phase produces a working, shippable assistant that is strictly better than the last, plus a written learning note (`docs/learning-notes.md`).

| Phase | Deliverable | Core concept you learn |
|---|---|---|
| **0. Scaffold** | Repo, config, logging, CI-quality tooling | Production Python project hygiene |
| **1. MVP agent** | REPL + agent loop + filesystem/shell/web tools + permissions + sessions + audit log | The agent loop; tool-use protocol; safety gating |
| **2. Memory** | Long-term memory (embeddings + recall), conversation compaction, end-of-session reflection | Context engineering; what to remember and when to inject it |
| **3. Tasks & scheduling** | Task store, reminders, background jobs that wake the agent | Agents that act without being prompted |
| **4. Research + Knowledge Base** | Search/fetch/ingest pipeline plus a self-maintained Markdown "LLM Wiki" | Grounding; source ingestion; durable external knowledge |
| **5. Evaluation & hardening** | Eval harness (scenario suites, LLM-as-judge), regression gate | How you know the agent actually works |
| **6. Multi-agent** | `spawn_agent` tool: planner delegates to scoped sub-agents with isolated contexts | Orchestration, context isolation, result synthesis |
| **7. Voice** | Push-to-talk STT → agent → TTS; optional wake word | Realtime UX constraints on the loop |
| **8. Web UI / API** | FastAPI + WebSocket chat surface over the same core | The payoff of the thin-interface rule |

Simple pytest unit tests exist from Phase 1; Phase 5 is where *agent-level* evaluation gets serious. Phases 4 and 5 can swap order; 6–8 can reorder by interest.

---

## 3. Tech Stack

| Concern | Choice | Why |
|---|---|---|
| Language / runtime | Python 3.12+, **async core** (asyncio) | Streaming, parallel tool calls, later WebSockets |
| Package manager | **uv** | Fast, lockfile, modern standard |
| LLM API | **anthropic** SDK, raw Messages API | The point of the project — no framework |
| Main model | `claude-opus-4-8` | Best available tool-use reasoning — quality first |
| Utility model | `claude-sonnet-5` | Compaction, memory extraction, reflection. Deliberately NOT a small model: bad summaries silently corrupt context and memory, so quality matters here too. Only trivial cosmetic ops (session titles) may drop lower. |
| Judge model (P5) | `claude-opus-4-8` | Eval grading must be at least as strong as the agent being graded |
| Validation/config | **pydantic v2 + pydantic-settings** | Tool schemas generated from typed params |
| REPL | **rich** (rendering) + **prompt_toolkit** (input) | Streaming markdown, tool-call panels, approval prompts |
| Persistence | **SQLite** via `aiosqlite`, plain SQL + tiny migration runner | See the actual data model; no ORM magic |
| Embeddings | **voyageai** (`voyage-3-large`) | Anthropic's recommended partner; largest/highest-quality retrieval model |
| Vector search | Cosine sim in **numpy** over SQLite-stored vectors | At personal-assistant scale (<100k memories) this is fine; swap to sqlite-vec later if needed |
| Web search | **Tavily API** | Agent-oriented answers + sources |
| Web fetch/extract | **httpx + trafilatura** | Clean article text from raw HTML |
| Document-to-Markdown conversion (P4) | **Microsoft MarkItDown** first, model cleanup second | Deterministic conversion is cheaper, auditable, and preserves structure for LLM pipelines; use model tokens for semantic cleanup, summaries, cross-links, and contradiction checks rather than raw extraction |
| Logging | **structlog** → JSON lines | Machine-parseable audit + traces |
| Scheduling (P3) | **APScheduler** | Cron + interval jobs in-process |
| Voice (P7) | **OpenAI Whisper API or faster-whisper large-v3** (STT) + **ElevenLabs** (TTS) | Best transcription accuracy and the most natural voice available — quality first |
| Web UI (P8) | **FastAPI + WebSocket** | Same async core underneath |
| Tests / lint | **pytest + pytest-asyncio, ruff** (lint+format) | Standard |

Windows notes: shell tool executes **PowerShell 7** (`pwsh -NoProfile -Command ...`); force UTF-8 (`PYTHONUTF8=1`); use `pathlib` everywhere.

### Model strategy checkpoints
Current model defaults are intentionally conservative: keep `claude-opus-4-8` as the main daily agent, `claude-sonnet-5` for high-quality utility work, `claude-opus-4-8` as the judge, and `voyage-3-large` for embeddings until Phase 5 produces measurements. Do not switch the working baseline just because a newer model exists; switch only when evals show better task success, retrieval quality, safety, or latency without unacceptable regressions.

Planned evaluation path:
- **Phase 5 model telemetry**: record per-call latency, tokens, cost, model id returned by the provider, effort, retries, stop reason, tool attempts, and scenario outcome. This makes model comparisons auditable instead of vibe-based.
- **Fable 5 experiment**: `claude-fable-5` is the preferred next-generation upgrade candidate. Add it first as an optional `deep_planner` / long-running-agent model, not the default daily agent. Compare it against Opus 4.8 on hard planning, research, coding, scheduling, KB, and adversarial scenarios. Promote it only if it improves success/reliability without increasing unsafe attempt rates or making latency painful.
- **Effort sweep**: compare `high`, `xhigh`, and `max` on selected hard scenarios. Keep `high` as the default unless the eval report shows a clear win for higher effort on specific classes of work.
- **Judge hardening**: keep Opus 4.8 as the counted judge initially; record a Sonnet 5 shadow vote to flag disagreements. A judge score never overrides deterministic safety checks.
- **Embedding migration**: evaluate `voyage-4-large`, `voyage-4`, and contextualized Voyage embeddings against the existing `voyage-3-large` baseline on memory and KB golden sets. If a newer model wins, rebuild memory/KB indexes, retune similarity thresholds, and record the migration in an ADR. Do not compare vectors across incompatible embedding spaces.
- **Router follow-up**: after Phase 5, consider expanding `ModelRouter` roles to `main`, `utility`, `judge`, `deep_planner`, `fast_ui`, `embedding_document`, and `embedding_query`. This is a routing optimization, not a reason to hide the loop behind a framework.

---

## 4. MVP Definition (end of Phase 1)

A terminal assistant where you can type: *“Look at the files in my Desktop/reports folder, summarize the newest one, and search the web for anything that contradicts its main claim.”* — and watch it plan, call tools (asking approval for anything risky), and answer with sources.

**In scope:**
- Streaming REPL: markdown rendering, live tool-call panels (name, args, result preview), Ctrl+C interrupts a turn without killing the session.
- Agent loop with parallel tool execution and max-iteration guard.
- Built-in tools: `read_file`, `write_file`, `list_dir`, `glob_search`, `run_shell`, `web_search`, `web_fetch`.
- PermissionGate: per-tool `allow | ask | deny` policy from `config/permissions.yaml`; path allowlist for filesystem writes; "always allow" persists.
- Session persistence in SQLite; `--resume` picks up the last session.
- Audit log: every model call (with token usage + computed cost) and every tool call/decision as JSON events with a per-turn `trace_id`.
- Unit tests for registry, gate, loop (mocked client); one live smoke eval script.

**Explicitly out of MVP:** long-term memory, tasks/scheduler, sub-agents, voice, web UI, MCP.

---

## 5. The Agent Loop (precise spec)

```python
async def run_turn(session, user_input) -> str:
    trace_id = new_trace_id()
    messages = session.messages + [user(user_input)]

    for iteration in range(MAX_ITERATIONS):            # guard: default 25
        messages = context_manager.fit(messages)       # compact if near token budget
        response = await client.create(
            model=router.main, system=prompts.build(session),
            messages=messages, tools=registry.schemas(), stream=True,
        )                                              # stream text deltas to UI as they arrive
        log_model_call(trace_id, response.usage, cost)
        messages.append(assistant_blocks(response))    # text + tool_use blocks verbatim

        if response.stop_reason != "tool_use":
            session.persist(messages)
            return final_text(response)

        tool_calls = extract_tool_uses(response)
        results = await asyncio.gather(*[               # parallel execution
            execute_one(call, trace_id) for call in tool_calls
        ])
        messages.append(user_tool_results(results))    # one user msg, one result block per call

async def execute_one(call, trace_id):
    decision = gate.check(call)                        # allow / deny / ask-the-human
    if decision is ASK:
        decision = await ui.approve(call)              # y / n / always
    if decision is DENY:
        return tool_result(call.id, "Denied by user/policy.", is_error=True)
    try:
        result = await asyncio.wait_for(registry.execute(call), TOOL_TIMEOUT)
    except Exception as e:
        result = error_result(call.id, e)              # errors go BACK TO THE MODEL, not up the stack
    return truncate(result, MAX_RESULT_TOKENS)         # protect the context window
```

Non-obvious rules encoded above (each is a classic agent bug when violated):
- **Tool errors are model feedback, not crashes** — the model self-corrects when it sees the error text.
- **Denials are also tool results** — the model must learn the user said no, not silently retry.
- **Every `tool_use` id must get exactly one `tool_result`** — the API rejects the turn otherwise.
- **Truncate tool results** — one giant file read can silently destroy the rest of the conversation.
- **Max-iteration guard** — runaway loops are a matter of when, not if.

`ContextManager.fit`: count tokens (API `count_tokens`); if > ~70% of budget, summarize the oldest turns with haiku into a single summary block and keep recent turns verbatim. From Phase 2 it also injects recalled memories into the system prompt.

---

## 6. Memory, Tools, Permissions, Logging, Evaluation

### Memory (Phase 2)
Three tiers:
1. **Working** — the message list, managed by ContextManager compaction.
2. **Long-term** — `memories` table: `id, type (fact|preference|project|episode), content, embedding BLOB, source, created_at, last_accessed_at`. Tools `remember(content, type)` and `recall(query)`; plus **automatic recall**: embed each user message, inject top-k above a similarity threshold into the system prompt (marked as background, not instructions).
3. **Episodic** — full transcripts persisted; an end-of-session **reflection** step (haiku) extracts durable facts/preferences into long-term memory, deduplicating against existing entries.

### Tools
- `Tool` base: `name`, `description`, pydantic `Params` model (→ JSON schema), `permission_default`, `async execute(params) -> ToolResult`.
- `ToolRegistry` auto-discovers `tools/builtin/*`; `registry.schemas()` feeds the API call.
- Executor owns timeouts, error capture, result truncation. Adding a tool = one file + one policy line — this is the extensibility story.
- Later: an MCP client adapter so third-party MCP servers register as tools (Phase 4+, optional).

### Permissions
- `config/permissions.yaml`: per-tool `allow | ask | deny`, plus scoped rules (filesystem write allowlist paths; shell command prefix rules, e.g. `git status` allow / `rm` ask).
- Defaults: reads allowed; writes/shell/anything-external ask; nothing deny by default.
- "Always allow" at the prompt persists the rule to the yaml. Every decision (and who made it: policy vs human) is audited.

### Logging / observability
- structlog JSON lines → `logs/jarvis-YYYY-MM-DD.jsonl`. Event types: `turn_start`, `model_call` (model, tokens in/out, cost, latency, stop_reason), `tool_call`, `permission_decision`, `tool_result`, `turn_end`, `error`.
- `trace_id` per user turn ties everything together; session totals (tokens, cost) shown in REPL status bar.
- Pricing table constant per model → cost computed at call time.

### Evaluation (basic in P1, serious in P5)
1. **Unit** (pytest): tools, gate, registry, loop with mocked client.
2. **Scenario evals**: YAML files (`tests/evals/scenarios/*.yaml`) with prompt, allowed tools, and programmatic assertions (expected tool called with matching args / file created with expected content / final answer regex). A runner executes them against the live API in a sandboxed temp dir. Since cost is free, **run each scenario N=3 times** and require all passes — agents are stochastic, and single-run evals hide flakiness.
3. **LLM-as-judge** (P5): grade final answers on a rubric (groundedness, completeness, safety) using Opus 4.8 as judge, with 3 independent judge votes per answer (majority wins); track scores across git revisions to catch regressions.

### Phase 4 direction: Research + LLM Wiki
Steal the best idea from Hermes/Karpathy-style LLM Wiki systems: Jarvis should not only remember personal facts in the `memories` table; it should maintain an external Markdown knowledge base that compounds over time.

Planned shape:
- **Raw sources are immutable**: PDFs, docs, webpages, videos/transcripts, notes, and imports are kept as source artifacts with provenance and hashes.
- **Markdown is the agent-facing layer**: convert sources into Markdown, then let Jarvis build wiki pages, indexes, summaries, entity pages, backlinks, and research briefs.
- **Use deterministic converters before model tokens**: Microsoft MarkItDown is the default candidate for converting PDFs, Office docs, HTML, text formats, ZIP contents, YouTube URLs, and similar files into Markdown. The model should spend tokens on semantic work — cleaning rough sections, extracting claims, linking concepts, resolving contradictions, and writing durable wiki pages — not on recreating basic file parsing.
- **Conversion is gated like file reads**: MarkItDown performs I/O with the process's privileges, so any `convert_to_markdown` tool must reuse Jarvis's path resolver, sensitive-path floor, bounded output caps, and permission/audit model. Plugins/OCR/cloud analyzers are opt-in, not silently enabled.
- **Core operations**: `ingest_source`, `query_knowledge_base`, and `lint_knowledge_base` (orphans, stale claims, contradictions, missing citations). This becomes the bridge between memory (what Jarvis knows about the user) and knowledge (what Jarvis knows about the world/projects).

### External reference: Odysseus
Odysseus is an approved reference implementation/product source for Jarvis. Borrow its strengths deliberately, with attribution and license hygiene, while keeping Jarvis's core loop, permissions, persistence, and eval discipline as the source of truth.

What to study or adapt:
- **AI workstation shape**: chat, agents, research, documents, notes, tasks, calendar, email, providers, and settings in one local dashboard. This informs Phase 8's web UI/API, not Phase 4's core storage.
- **Local-first operations**: Docker/native setup guidance, LAN/Tailscale cautions, backup/restore for `data/`, provider health checks, degraded-state reporting, and clear troubleshooting logs.
- **Model/provider cookbook**: hardware-aware local model recommendations and serving workflows as an optional privacy/offline layer. API quality remains the default for Jarvis while cost is not a constraint.
- **Security reminders**: treat the eventual web UI like a private admin console; keep shell, file, email, MCP, and model-serving tools behind the same permission/audit model.
- **Real-world hardening backlog**: prompt/context bloat dashboards, prompt-injection tests for notes/docs/memories/fetched pages, scheduler visibility, integration smoke tests, and first-run setup polish.

### External reference: Meetily
Meetily is an approved reference for local-first meeting intelligence. It should inform Jarvis's later voice/web workstation work, while Phase 4 prepares the storage path by treating meeting transcripts and summaries as first-class knowledge sources.

What to study or adapt:
- **Meeting capture workflow**: microphone + system-audio capture, real-time transcript display, meeting history, transcript editing, and import-existing-audio flows.
- **Local transcription stack**: Whisper/Parakeet-style local speech-to-text, GPU acceleration options, and offline-first operation. Cloud transcription can remain optional when quality or convenience wins.
- **Meeting-to-KB pipeline**: recordings/transcripts become immutable sources; cleaned summaries, decisions, action items, and follow-ups become Obsidian-compatible wiki pages with `source_ids`.
- **Meeting-to-task bridge**: action items can create Jarvis tasks/reminders through the existing Phase 3 scheduler, with human approval before creating or assigning work.
- **Privacy and consent posture**: meetings are sensitive. Recording/transcription needs explicit user start/stop control, clear local storage, provenance, retention controls, and no unattended recording by default.
- **Future UX reference**: a meeting dashboard belongs with the Phase 8 web UI/API, not inside the core loop. Jarvis should own the data model, permissions, and evals; Meetily informs capture and product ergonomics.

### Approved local/self-hosted services backlog
These services are approved candidates for the local workstation stack around Jarvis. They are not core agent frameworks; they provide durable data sources, deterministic workflow surfaces, or private utilities that Jarvis can query/control through explicit tools, APIs, or MCP adapters.

Initial priority:
- **Paperless-ngx**: document/OCR archive. Bills, contracts, scans, receipts, and PDFs become searchable sources for the Phase 4 KB.
- **Karakeep**: first-choice bookmark/capture inbox. It is the better Jarvis fit over Linkwarden because it saves links, notes, images, and PDFs; has AI tagging; and exposes API/webhook-style integration surfaces. **Linkwarden** remains the alternative if collaborative reading and high-fidelity webpage preservation become more important.
- **Syncthing**: peer-to-peer sync for the Obsidian vault, exports, backups, and other local artifacts across machines without introducing a cloud dependency.
- **n8n self-hosted AI starter kit**: deterministic workflow layer. Jarvis should call approved n8n workflows for repeatable integrations instead of rebuilding every SaaS connector inside the agent core.
- **SearXNG**: optional private metasearch backend for research. Use as a configurable source behind the existing web-search/research boundary, not as a replacement for cited fetching.

Second wave:
- **Actual Budget**: local-first finance source. Start read-only (summaries, reminders, anomaly checks); require explicit approval before creating or changing transactions/budgets.
- **Mealie**: recipe, meal-planning, and shopping-list backend. Useful for preference-aware meal planning and scheduled grocery/task flows.

Integration rule: these systems should expose data to Jarvis through narrow, audited adapters. Jarvis remains the reasoning layer; external apps remain the domain-specific systems of record.

### Approved MCP / connector shortlist
MCP servers are powerful executable dependencies, so every server must be pinned, reviewed, least-privilege scoped, and wrapped by Jarvis's permission/audit model before it can perform writes or external side effects.

Selected daily stack:
- **Google Workspace**: Gmail, Drive, Calendar, Docs, Sheets, Slides, Contacts, Tasks, and Google Meet link creation through Calendar. Default to read/search/summarize; draft/send/upload/share/create/update/delete require approval with exact previews.
- **GitHub**: repo, issue, PR, code-search, and workflow context. Prefer read-only by default; issue/PR/comment/workflow writes require approval.
- **Telegram notifications**: start with send-only notifications for task completion, errors, review-needed queues, and long-running agent updates. Full Telegram chat management is a later, separately gated capability.
- **KakaoTalk**: useful for Korea-first daily communication, notifications, and information capture. Preferred path is official Kakao Developers / channel / message APIs for approved sends or self-notifications. Reading normal personal chats is not available as a clean official API, so any read/summarize workflow must be explicit local desktop/export automation, limited to selected chats, visible to the user, and never unattended by default. Replies/sends require exact preview approval.
- **Obsidian**: bridge access for the human-facing vault. Jarvis's Phase 4 KB remains the source of truth for provenance and indexing; external Obsidian MCP writes must preserve front-matter and stay inside the vault jail.
- **Browser / Playwright**: supervised website operation when no API exists. Keep it interactive/screenshot-visible; no unattended browser control until separately designed.
- **Postgres / SQLite / Supabase**: database querying and admin work. Start read-only; writes, migrations, RLS/auth/storage changes, destructive SQL, and `service_role`/secret-key use require task-specific approval and verification.
- **Docker**: inspect local services, logs, health, ports, and compose status for the self-hosted stack. Restart/stop/delete/prune/image-pull actions require approval.
- **Filesystem**: optional MCP only if it is faster/easier than Jarvis's native tools. Must be root-scoped, deny sensitive paths, and never bypass the existing permission/audit model.
- **Maps / Places**: routes, places, commute, local search, travel planning context. Usually read-only; booking or contacting places is out of scope.
- **Weather**: daily planning, travel, reminders, and schedule context. Read-only by default.
- **Discord**: read/search/summarize selected servers/channels and send approved notifications/messages. Posting, moderation, invites, and bulk actions require approval.
- **OneDrive**: file search/read/import/export through Microsoft Graph; writes/sharing require approval.
- **Spotify**: low-risk personal-control tool for playback/search/playlist actions; still audit calls because OAuth tokens are involved.

---

## 7. Repo Structure

```
jarvis/
├── pyproject.toml              # uv-managed; ruff + pytest config
├── README.md
├── .env.example                # ANTHROPIC_API_KEY, VOYAGE_API_KEY, TAVILY_API_KEY
├── .gitignore                  # data/, logs/, .env
├── config/
│   ├── settings.yaml           # models, token budgets, limits
│   └── permissions.yaml        # per-tool + scoped policies
├── src/jarvis/
│   ├── __main__.py             # `python -m jarvis` / `jarvis` entry
│   ├── cli/
│   │   ├── repl.py             # input loop, approval prompts
│   │   └── render.py           # rich streaming markdown, tool panels
│   ├── core/
│   │   ├── agent.py            # AgentLoop (section 5)
│   │   ├── context.py          # token budget, compaction, memory injection
│   │   ├── router.py           # ModelRouter (main vs utility model)
│   │   ├── prompts.py          # system prompt assembly
│   │   └── client.py           # Anthropic wrapper: retries, streaming, usage capture
│   ├── tools/
│   │   ├── base.py  registry.py  executor.py
│   │   └── builtin/
│   │       ├── filesystem.py  shell.py  web.py
│   │       └── memory.py       # phase 2   tasks.py  # phase 3
│   ├── permissions/gate.py  policy.py
│   ├── memory/store.py  embeddings.py  reflection.py      # phase 2
│   ├── scheduler/                                          # phase 3
│   ├── agents/                                             # phase 6 (sub-agents)
│   ├── voice/                                              # phase 7
│   ├── persistence/db.py  migrations.py  sessions.py
│   ├── observability/logging.py  cost.py
│   └── config.py               # pydantic-settings
├── tests/
│   ├── unit/
│   └── evals/runner.py  scenarios/*.yaml
├── docs/
│   ├── PLAN.md                 # this document, committed into the repo
│   ├── architecture.md         # kept current as phases land
│   ├── learning-notes.md       # your concept log per phase
│   └── decisions/              # ADRs: 0001-from-scratch-loop.md, ...
├── data/                       # jarvis.db (gitignored)
└── logs/                       # *.jsonl (gitignored)
```

---

## 8. First Coding Task List (Milestone 1, for Opus 4.8)

Ordered; each task ends green (tests pass, ruff clean). Tasks 1–6 are pure infrastructure testable without an API key; 7+ go live.

1. **Scaffold**: `uv init`, git init, pyproject (ruff, pytest, pytest-asyncio), src layout, `.env.example`, `.gitignore`, README stub, commit `docs/PLAN.md` (this file).
2. **Config**: `config.py` (pydantic-settings: API keys, model ids, budgets, paths) + `config/settings.yaml` loader; fail fast with a clear message on missing keys.
3. **Observability**: structlog JSON setup, `trace_id` contextvar, pricing table + cost calculator. Unit tests for cost math.
4. **Tool framework**: `Tool` base (pydantic params → JSON schema), `ToolRegistry` with auto-discovery, `ToolExecutor` (timeout, error capture, truncation). Tests with a dummy tool, including schema-generation snapshots.
5. **Permissions**: policy loader for `permissions.yaml`, `PermissionGate.check`, persist-on-always. Tests: allow/ask/deny, path allowlist, shell prefix rules.
6. **Agent loop (mocked)**: `client.py` wrapper interface + `AgentLoop` per section 5, tested end-to-end against a scripted fake client (text-only turn; tool turn; parallel tools; error tool; denial; max-iterations).
7. **Live client + streaming**: real Anthropic streaming, retries with backoff on 429/529, usage capture wired to cost logging.
8. **REPL**: prompt_toolkit input, rich streaming markdown, tool-call panels, approval prompt (y/n/always), Ctrl+C cancels turn only, status bar with session token/cost totals.
9. **Built-in tools**: `filesystem.py` (read/write/list/glob, path checks), `shell.py` (pwsh, timeout, cwd, output caps), `web.py` (Tavily search, httpx+trafilatura fetch). Unit tests; web tests mocked.
10. **Persistence**: SQLite schema v1 (`sessions`, `messages`), migration runner, save-per-turn, `jarvis --resume`.
11. **Smoke evals**: `tests/evals/runner.py` + 3 scenarios (multi-step file task in temp dir; web research question; permission-denial handling). Run live, print pass/fail + cost.
12. **Docs**: fill README (setup, usage, safety model), `docs/architecture.md` as-built, ADR-0001 (from-scratch loop rationale).

Acceptance for Milestone 1 = the MVP definition in section 4, demonstrated by the smoke evals passing.

---

## 9. Model Switch — When and How

**Switch after this plan is approved, before Task 1.** Planning/decomposition (Fable's job here) is done; everything in section 8 is implementation-heavy — Opus 4.8's job. Come back to Fable-style planning at each phase boundary (design Phase 2 memory semantics, Phase 6 orchestration, etc.); implement each phase with Opus.

Mechanics in Claude Code: approve this plan, then switch the session model to **Opus 4.8** (model selector / `/model opus-4.8`, or start a fresh session in the jarvis directory), and give it the handoff prompt below. The first task commits this plan to `docs/PLAN.md`, so the spec stays durable in-repo regardless of session history.

### Handoff prompt for Opus 4.8 (copy verbatim)

> You are implementing **Jarvis**, a from-scratch agentic assistant in Python, in `C:\Users\habib\Desktop\jarvis`. The complete approved architecture and task spec is in the plan document (if `docs/PLAN.md` doesn't exist yet, I'll paste it / it's at `C:\Users\habib\.claude\plans\i-want-to-build-staged-zephyr.md`). Read it fully before writing any code.
>
> Execute **Milestone 1, tasks 1–12 in order** from section 8 of the plan. Rules of engagement:
> - Follow the architecture exactly: async core, layered dependencies (cli → core → services → persistence), tools as pydantic-schema classes, every side effect through the PermissionGate, every model/tool event logged with a trace_id. The agent-loop invariants in section 5 (tool errors returned to the model, one tool_result per tool_use id, result truncation, max-iteration guard) are non-negotiable.
> - Work task-by-task: after each task, run `ruff check` and `pytest`, show me the results, and make a git commit with a clear message. Don't start the next task with a red build.
> - Quality over cost, always: API cost is company-provided and irrelevant. Never downgrade a model, skip a retry, shrink an eval, or trim context to save tokens — the only reasons to limit tokens are correctness ones (context-window protection, result truncation).
> - Tasks 1–6 must be fully testable without an API key (fake client). Live API code starts at task 7.
> - Windows environment: PowerShell 7 for the shell tool, pathlib for paths, UTF-8 everywhere.
> - This is also a learning project: after each task, append 3–5 bullet points to `docs/learning-notes.md` explaining the non-obvious design decisions you made and why — write them for an advanced engineer who is new to agent architectures.
> - If you hit a genuine design ambiguity the plan doesn't cover, make the simplest choice consistent with the architecture, record it as a short ADR in `docs/decisions/`, and continue.
>
> Start with task 1 (scaffold) now.

---

## Verification

Milestone 1 is done when, from a fresh clone with only `.env` populated:
1. `uv sync && uv run pytest` — all unit tests pass without an API key.
2. `uv run jarvis` — REPL starts; a multi-step request (e.g. "list the files in this repo, read pyproject.toml, and write a one-line summary to summary.txt") streams reasoning, shows tool panels, asks approval for the write, and completes; `summary.txt` exists with sensible content.
3. A denied approval produces a graceful model acknowledgment, not a crash; the denial appears in `logs/*.jsonl` with the turn's trace_id.
4. `uv run python tests/evals/runner.py` — all 3 smoke scenarios pass, with per-scenario cost printed.
5. `jarvis --resume` restores the previous conversation.
