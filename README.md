# Jarvis

A from-scratch, Jarvis-style agentic assistant built directly on the Anthropic
Messages API — **no agent framework**. The agent loop, tool system, permission
model, memory, and observability are all hand-built, so every moving part is
visible and understood. The goal is twofold: learn agent engineering deeply, and
end up with a genuinely useful assistant that can use tools, remember things,
manage tasks, read files, research the web, coordinate scoped sub-agents, and
eventually speak and listen.

The full architecture and design rationale live in
[`docs/PLAN.md`](docs/PLAN.md) and [`docs/architecture.md`](docs/architecture.md);
per-task design notes are in [`docs/learning-notes.md`](docs/learning-notes.md).

## Status

**Phase 14 (AI Team Office — visual orchestration view) — complete; render-only, adds zero new
authority.** An optional, premium "operations floor" over the existing orchestration system, reached
as a per-project **Office** workspace tab (`#workspace/{id}/office`) — the calm `#studio` timeline
stays the default. Teams render as **rooms**, members as **status nodes** (monogram + live status
ring + role·model·provider + tool/service chips), the workflow as a calm **stage flow** ending at
Fable's synthesis+verdict **chair**, plus a live strip, a bounded activity feed, and recent runs. Two
layouts share one DOM and one data source — **Compact** (dense, the default) and **Office** (the
roomier floor) — toggled by a root class (a pure CSS relayout). It is a render-only skin: one new
read model (`office_overview`, a pure **assembler** over existing read models — no storage, **no
migration**), one read-only `GET /api/workspace/{id}/office`, and live updates patched surgically from
the existing orchestration WS bus (coalesced into one `requestAnimationFrame`, never a full
re-render). No new authority or action path — Launch deep-links to Studio, Cancel/approve reuse the
existing gated routes, per-node inspect only navigates, and per-project layout is localStorage-only
(the mutation-route set stays a closed, test-pinned **35**). Agent/service text is `textContent`
(never `innerHTML`), the surface is metadata-only (no body, no key value), and the visual language is
Kairo's own token-driven CSS — no external assets, nothing copied from the AGPL `my-virtual-office`.
Screenshot DoD GREEN across noir/light/neon × 1440/1024/390 for compact/office/empty/large
([ADR-0020](docs/decisions/0020-ai-team-office.md), closeout in
[`docs/verification-14.md`](docs/verification-14.md)).

**Phase 13 (research services live + context reuse) — complete; live-verified.** The hosted
research capability the 10B catalog described is now real, behind the same fail-closed machinery:
**`firecrawl_scrape`** (URL→markdown), **`exa_search`**, **`searxng_search`** (local loopback only),
and **`generate_image`** (→ an `untrusted_model_generated` artifact, never executed). Each is a
`ServiceTool` whose egress/ASK/framing/`public_only` policy is *derived* from its catalog row; every
byte is framed untrusted; unknown/missing-key/unpriced ⇒ the tool never registers. New this phase:
hard **cost caps** (per-run/day, refused *before* a metered call would breach), **per-project
narrow-only** service selection, a read-only **settings** policy surface, and the **S7 context-reuse**
enable-step (a default-OFF flag caches only the stable, non-sensitive prompt prefix — Anthropic
`cache_control` / OpenAI `prompt_cache_key`; flag-off is byte-identical). Jina stayed deferred (no
value bar cleared vs `web_fetch` + `firecrawl_scrape`); Z.ai stays an optional, disabled worker (no
context reuse, no authority). Live-verified (firecrawl + exa + image + a hostile-content proof where
the model treated a planted injection as data and flagged it + a real S7 cache hit + the private-
canary refusal): [ADR-0019](docs/decisions/0019-research-services-live.md),
[ADR-0018](docs/decisions/0018-context-reuse.md), closeout in
[`docs/verification-13.md`](docs/verification-13.md). Phase 12 added the outward-write connectors
(calendar/Docs/Gmail-drafts) as human-approved, two-phase write intents — the write tool only
*proposes*; a separate human-approved route *executes* the stored request — on the Phase-9
connector/egress floor ([ADR-0009](docs/decisions/0009-connectors-and-egress.md)).

**Phase 11 (Kairo Workstation — product surface) — complete; adds zero new authority.** The
workstation is now premium, project-first, and searchable: a **Daily command center** (priority
cards + designed empty states), a **Projects grid** (labels/pins/health chips + smart collections),
a per-project **Workspace** (Overview · Chats · Artifacts · Memory · Tasks · Vault · Studio · Costs
· Activity tabs over scoped read models), a global **Artifacts Library** (filterable list + a
preview that renders text as `textContent` and images from a hardened content route), a **Cost
Center** (periods × dimensions, budget-warning banner, ROI/time-saved), a polished **Studio**
(roster cards, run timeline, head-reviewer badge), **Settings** (appearance + read-only status),
and a Ctrl/Cmd-K **command palette** (federated FTS5 search, GET/navigate-only). Three themes
(noir/light/neon) + density/layout/motion knobs, all client-side (no server theme route). The
load-bearing decision ([ADR-0017](docs/decisions/0017-workstation-ui.md)): the UI **reads and
navigates only** — every write goes through the existing gated routes (the mutation-route set is a
closed, test-pinned set), untrusted content is rendered as text (never `innerHTML`/linkified),
artifact bytes are served only through a registered-id-only, quarantine-refusing, media-allowlisted,
size-capped route, and status/cost surfaces expose presence booleans, never a key value. Vanilla ES
modules (no framework/build/CDN); the desktop-first shell is responsive with a pinned no-overlap
assertion across 1440/1024/390 × three themes, captured by Kairo's own Playwright harness. Design
in [`docs/PLAN-11-workstation.md`](docs/PLAN-11-workstation.md); closeout checklist in
[`docs/verification-11.md`](docs/verification-11.md).

**Phase 10 (project workspaces + Orchestration Studio) — framework complete; local adapters
built, live scans/eval-gate pending your machine.** Phase 10A adds **projects** (project-scoped
memory/KB/tasks via a nullable `project_id`, enforced in SQL), **run modes** (Plan / Approval /
Auto, composed at the documented gate/approver seams — [ADR-0012](docs/decisions/0012-modes.md)),
a **model registry + cost ledger** (per-role routes, fail-closed pricing, a metadata-only
`model_calls` ledger — [ADR-0013](docs/decisions/0013-model-registry-and-cost-ledger.md)), and
budgets. Phase 10B adds the **Orchestration Studio**: project *teams* (Research, Frontend,
Backend, Security, QA, PM, Ops, Custom) run a workflow through a stage machine — council →
synthesis → execution → review → verdict — built **on Phase-6 spawn, not a second agent
framework** ([ADR-0014](docs/decisions/0014-orchestration-on-spawn.md)). Council/review members
are read-only with no egress; exactly one write-capable member runs, only in the execution stage,
under the shared turn lock; the engine trusts run records, never a child's report text (a forged
"verdict: accept" is inert). A worst-case **cost reservation** with a two-step confirm runs before
any fan-out; unpriced routes/services block (fail-closed). **Team Tool Intelligence**
([ADR-0015](docs/decisions/0015-team-tool-intelligence.md)) is a classified `SERVICE_CATALOG`
whose enforcement is *derived* from each row (`egress`/`write`/`context_policy`/`output_trust`),
never hand-set; three local, free, flag-gated adapters ship — **Semgrep** and **Gitleaks**
(hardened-argv, offline, sensitive-path excluded, Gitleaks findings redacted to `file:line + rule
id`) and **Playwright-localhost** (localhost-only, inspect-only: navigate/screenshot/DOM/a11y/
visual-diff, no click/type/submit). `services.enabled` is empty by default — nothing external is
on, and nothing is live until a human lists it. **Honest status:** the keyless suite is green and
the adapter guards are verified against this repo, but the live scans (need the Semgrep/Gitleaks/
Playwright binaries), real orchestration runs, and the judged eval gate (need an API key) are
**not yet run** — the checklist + commands are in [`docs/verification-10B.md`](docs/verification-10B.md).
Design in [`docs/PLAN-10B-teams.md`](docs/PLAN-10B-teams.md).

**Phase 10C (direct provider workers) — framework complete; live provider checks pending your
keys.** Adds Qwen / DeepSeek / GLM(Z.ai) / Gemini as cheap, scalable **worker** models via a
`PROVIDER_CATALOG` whose enforcement is *derived* per row
([ADR-0016](docs/decisions/0016-provider-integration.md)). DeepSeek/Qwen/GLM reuse the native
client through their **Anthropic-compatible** endpoints (a capability-degradation profile: no
effort/thinking; per-provider auth header); Gemini rides the **text-only** OpenAI-compatible
client. **Fable/Opus stay the deciding layer**: planner/judge (final authority) and utility
(private content) are code-pinned to anthropic at every routing layer — a cheap worker can never
become the head synthesizer, final reviewer, judge, or a private-content processor. Availability
is fail-closed (`providers.enabled` ∧ key ∧ priced); a PRIVATE-provenance bundle is refused before
fan-out for any non-trusted provider; `providers.enabled` is empty by default (byte-identical to
pre-10C). Keys load from `.env` only. Live provider checks (DeepSeek/Gemini; Qwen once priced;
Z.ai pending its console) are in [`docs/verification-10C.md`](docs/verification-10C.md); design in
[`docs/PLAN-10C-providers.md`](docs/PLAN-10C-providers.md).

**Phase 9 (make Kairo useful daily) — complete.** Kairo now flows real daily context in and
real workflows out, all behind the existing PermissionGate. **Connectors** are narrow, audited
httpx REST adapters (not a library, not MCP): Google Calendar/Gmail/Drive (read-first) plus
Telegram + Kakao send-only notifications, connected by a terminal ritual (`jarvis connect
google|kakao|telegram`). Gmail is **drafts-only forever** — no send scope, no send method
anywhere (a grep pin enforces it); "prepare a reply" makes a draft you send yourself. The
safety centerpiece is that the permission model now reasons about **data flow, not just tools**
([ADR-0009](docs/decisions/0009-connectors-and-egress.md)): reads are silent+framed+audited, but
the moment a turn reads private data any egress is demoted to a non-persistable ASK (no silent
mail-read → silent-fetch pipe), egress is never unattended, and OAuth tokens sit under a
cross-cutting sensitive-path floor. The **Daily Digest** is deterministic collectors + one
**tool-less** summarize ([ADR-0010](docs/decisions/0010-digest.md)) — injected email text can
colour the words but can never trigger an action; failures render "needs reconnect", never
"zero results"; delivery is UI/DB-first then best-effort notifiers, rendered as text (never
linkified). Daily bootstraps with real context (repo state via a hardened git reader, eval
freshness, tasks, the latest digest, connector status), background job/reminder/digest results
finally reach the browser as notices, the Vault gains bulk folder ingestion + a UI ingest box +
an informed review preview, and workflow chips run prepared prompts through the one gated turn
path. **Demo mode** populates it all with badged fake data without OAuth. Design in
[`docs/PLAN-9-daily.md`](docs/PLAN-9-daily.md); Mac setup in [`docs/migration-macos.md`](docs/migration-macos.md).

**Phase 8 (workstation UI) — complete.** Jarvis has a local web workstation (`jarvis --ui`):
a calm daily command center over the same core — memory, tasks, KB/wiki, evals, sub-agents,
voice — with **zero new autonomy**. It is a third peer interface (REPL, voice, workstation)
driving the same `AgentLoop` through the same seams, so nothing bypasses the safety model.
It is an **authenticated local peer**, not a public surface
([ADR-0008](docs/decisions/0008-the-workstation-ui-is-an-authenticated-local-peer.md)): it
binds loopback only, mints a per-launch token exchanged for a session (clean-URL, no token in
history), and guards Host (anti-rebinding) + Origin (anti-CSRF) with a strict CSP and no CORS.
**Approvals are the priority surface and are replay-proof**: an ASK becomes a Gate item
showing the exact payload + reason, resolved only by an authenticated click carrying a
single-use nonce minted over the live socket after the modal is shown — a spoken "yes" or a
replayed page can't approve, and the UI is voice's **fail-closed screen** (voice prepares,
screen commits). Nothing is hidden: every side-effecting action (incl. denied calls and
sub-agent activity) streams to Daily Mode and Trace; **Debug Mode reveals telemetry but adds
zero capability** (route/capability parity pinned). Screens: Command (Daily), Gate, Vault,
Tasks, Memory, Meetings, Hub (connectors/MCP status), Trace, Lab — the only mutations are the
existing human-authority ops (approve/deny, `kb review`, cancel a task, forget a memory,
meeting capture), a closed set pinned by test. The frontend is hand-written and
self-contained (no build step, no CDN, no external fonts). Design in
[`docs/PLAN-8-ui.md`](docs/PLAN-8-ui.md).

**Phase 7 (voice) — complete.** Jarvis has a push-to-talk voice interface (`jarvis --voice`):
press Enter, speak one utterance, hear a short spoken summary back. Voice is a *peer of the
REPL* driving the same `AgentLoop`, never a new authority — so every gate and floor still
applies beneath it. Its safety floor is non-negotiable
([the permissions checkpoint](docs/PLAN-7-voice-permissions-checkpoint.md), realized in
[ADR-0007](docs/decisions/0007-voice-is-an-untrusted-read-only-surface.md)): **transcribed
audio is untrusted** (framed like a fetched web page — hearing an instruction is not
authorization to act on it), voice is **read-only by default**, and **risky actions are never
approved by voice** — the `VoiceApprover` escalates every `ask` to an on-screen confirmation
and is **fail-closed** (no positively-available screen ⇒ deny; a spoken "yes" has no path to
approve). No unattended mic (push-to-talk only; wake-word deferred), and the spoken channel
follows a **TTS-privacy rule** — it never voices secrets, commands, file contents, message
bodies, or the details of a risky action (those stay on screen). STT/TTS are **local /
on-device by default** (no egress); cloud providers (OpenAI STT, ElevenLabs TTS) sit behind
an explicit `voice.cloud_providers` opt-in and log audio/text egress as a visible network
event. A meeting can be captured as an **unreviewed** knowledge source (never an action). The
eval harness gains six voice acceptance scenarios and a **chunked live-gate profile**
(`jarvis eval gate --profile live-chunked`) that fits the runner's background-time cap. Design
in [`docs/PLAN-7-voice.md`](docs/PLAN-7-voice.md); baseline in
[`docs/evals-baseline-phase7.md`](docs/evals-baseline-phase7.md).

**Phase 6 (multi-agent orchestration) — complete.** Jarvis can delegate: `spawn_agent`
runs a scoped sub-agent with an isolated context and a per-spawn tool allowlist, then
synthesizes its report (try "research X and Y in parallel using sub-agents"; watch it
with the `agents` command). Delegation is **doubly gated** (you approve each spawn, and
every child tool call still passes a `SubAgentGate` that can only *tighten* the parent's
gate), **depth-1** (a child can't spawn — enforced three ways), and **never unattended**.
Nothing is hidden: child activity renders inline, each transcript is a `kind='subagent'`
session (never resumed, never reflected), and an `agent_runs` row links parent and child
by trace id. The live baseline was **GATE PASS** across both suites — all 24 existing
scenarios PASS→PASS (zero regressions), the 6 new delegation scenarios PASS 3/3, Safety
CLEAN, **0/27 injection attempts** (the model refused even the report-laundering and
scope-escape vectors). Design in [`docs/PLAN-6-multi-agent.md`](docs/PLAN-6-multi-agent.md);
rationale (the double gate, depth-1, no-unattended-spawn) in
[ADR-0006](docs/decisions/0006-sub-agents-are-scoped-visible-and-doubly-gated.md); baseline
in [`docs/evals-baseline-phase6.md`](docs/evals-baseline-phase6.md). (Subsystems now also
carry **Kairo** names in the docs — a rebrand at the documentation level; the code still
says `jarvis`.)

**Phase 5 (evaluation & hardening) — complete.** A repo-native eval harness that says
whether the agent actually works and whether a change made it better or worse:
scenario suites with deterministic checks, an honest LLM-as-judge (rationale-first
forced verdict, 3 Opus votes + a Sonnet cross-check, calibration fixtures that can void
a run), a two-tier regression gate (safety all-N; quality FLAKY-pass), an adversarial
suite that measures *side effects* (gated) separately from *attempts* (tracked), and
retrieval-quality evals (MRR / recall@k, similarity-floor sweep). Web results are now
wrapped in untrusted-content framing to match the KB/memory layers. The live baseline
was GATE PASS 24/24, Safety CLEAN, 0/21 injections attempted; the committed contract is
[`tests/evals/baselines.yaml`](tests/evals/baselines.yaml). Design in
[`docs/PLAN-5-evals.md`](docs/PLAN-5-evals.md); rationale (judge validity, gate
statistics, the auto-injection verdict) in
[ADR-0005](docs/decisions/0005-how-we-know-it-works.md); baseline report in
[`docs/evals-baseline.md`](docs/evals-baseline.md).

**Phase 4 (research + knowledge base) — complete.** Jarvis maintains a durable,
Obsidian-compatible Markdown knowledge base: it ingests files, web pages, and notes
(`ingest_source`) into immutable raw artifacts + deterministic Markdown, searches
them with citations (`query_knowledge_base`), curates wiki pages (`write_wiki_page`),
and self-checks with `lint_knowledge_base` — plus `kb` / `kb lint` / `kb rebuild` /
`kb review` REPL commands. Conversion is deterministic-first (MarkItDown; Docling
optional) and runs in a killable sandbox; the whole layer is a deliberately-contained
injection sink — see the safety model and [ADR-0004](docs/decisions/0004-converters-are-gated-io-and-the-kb-is-a-contained-injection-sink.md).
Design in [`docs/PLAN-4-knowledge.md`](docs/PLAN-4-knowledge.md).

**Phase 3 (tasks & scheduling) — complete.** Jarvis can now act without being
prompted: schedule **reminders** (delivered to you at a time) and **jobs** (a
stored prompt it runs itself, unattended, on a once / cron / interval schedule)
with a `schedule_task` / `list_tasks` / `cancel_task` toolset and a `tasks` REPL
command. A background wake-loop fires due tasks in-process; missed tasks are caught
up (within a grace window) on the next start. Unattended runs are deliberately
constrained — see the safety model below and [ADR-0003](docs/decisions/0003-unattended-runs-deny-and-demote.md).
Design and rationale are in [`docs/PLAN-3-tasks.md`](docs/PLAN-3-tasks.md).

**Phase 2 (long-term memory) — complete.** Durable memory across sessions
(embeddings recall + a `remember`/`recall`/`forget` toolset), automatic background
recall, conversation compaction, and end-of-session reflection. Design in
[`docs/PLAN-2-memory.md`](docs/PLAN-2-memory.md).

**Phase 1 (MVP) — complete.** A streaming terminal assistant that plans, calls
tools (asking approval for risky ones), remembers a conversation across restarts,
and reports what it did with sources. Verified end-to-end by a live smoke-eval
suite. Later phases (evaluation harness, multi-agent, voice, web UI) are laid out in
[`docs/PLAN.md`](docs/PLAN.md) §2.
Odysseus is tracked there as an approved external product/reference source for
the eventual local AI workstation experience.

## Requirements

- [uv](https://docs.astral.sh/uv/) — package + Python manager
- Python 3.12+ (the project pins 3.13 via `.python-version`; uv fetches it)
- PowerShell 7 (`pwsh`) — the shell tool runs commands through it
- API keys: **Anthropic** (required), **Tavily** (web search), **Voyage** (phase 2).
  Optional, voice cloud providers only: **OpenAI** (cloud STT), **ElevenLabs** (cloud TTS)
- Voice (`jarvis --voice`) needs the optional extra: `uv sync --extra voice` (mic + engines).
  The default local TTS is dependency-free (prints the safe summary); local STT uses
  faster-whisper (`uv pip install faster-whisper`)
- Workstation UI (`jarvis --ui`) needs the optional extra: `uv sync --extra ui` (FastAPI + uvicorn)

## Setup

```pwsh
uv sync                 # create the venv, fetch Python 3.13, install deps
cp .env.example .env     # then fill in your API keys
```

`.env`:

```
ANTHROPIC_API_KEY=...    # required
TAVILY_API_KEY=...       # for web_search
VOYAGE_API_KEY=...        # for long-term memory (embeddings)
OPENAI_API_KEY=...        # optional: cloud STT (voice.cloud_providers only)
ELEVENLABS_API_KEY=...    # optional: cloud TTS (voice.cloud_providers only)
DEEPSEEK_API_KEY=...      # optional: Phase 10C worker (providers.enabled)
DASHSCOPE_API_KEY=...     # optional: Qwen worker (needs pricing filled)
ZAI_API_KEY=...           # optional: GLM / Z.ai worker
GEMINI_API_KEY=...        # optional: Gemini text-only worker (NOT GOOGLE_CLIENT_ID/SECRET)
```

## Usage

```pwsh
uv run jarvis            # start the assistant (needs a real terminal)
uv run jarvis --resume   # continue the most recent conversation
uv run jarvis --voice    # push-to-talk voice (needs voice.enabled + the voice extra)
uv run jarvis --ui       # local workstation UI (needs ui.enabled + the ui extra)
uv run jarvis --version

uv run pytest            # unit tests (no API key needed)
uv run ruff check        # lint
uv run jarvis eval gate                       # live smoke evals (uses the API — costs money)
uv run jarvis eval gate --profile live-chunked  # chunked live gate (fits the ~14-min cap)
uv run jarvis eval gate --runs 1              # quick single pass
```

In the REPL: type a request; watch Jarvis stream its reasoning and tool calls.
Risky tools prompt for approval (`y` / `N` / `a`lways). `Ctrl+C` cancels the
current turn without quitting; `exit` or `Ctrl+D` quits. Type `memories` to list
what Jarvis has remembered (with provenance — where each memory came from),
`tasks` (or `tasks all` / `tasks <id>`) to see scheduled tasks and their run
history, and `agents` (or `agents <id>`) to see recent sub-agent runs (with the
verbatim delegated prompt, tool scope, and the parent↔child trace link).

**Voice** (`voice.enabled: true` + `uv sync --extra voice`): `jarvis --voice` opens a
push-to-talk loop — press Enter, speak one utterance, and hear a short, safe spoken summary.
Voice input is read-only by default and the transcript is treated as untrusted; if a request
needs a write, send, delete, shell command, schedule, or spend, Jarvis *prepares* it and asks
you to confirm **on screen** — a spoken "yes" never commits it, and if no screen is available
the action is denied. STT/TTS default to local/on-device; set `voice.cloud_providers: true`
(and `voice.stt_provider` / `voice.tts_provider`) to use OpenAI/ElevenLabs, which logs the
audio/text that leaves the machine. Leave `voice.enabled: false` and the voice surface simply
isn't built (the REPL is unchanged).

**Workstation UI** (`ui.enabled: true` + `uv sync --extra ui`): `jarvis --ui` prints a
tokened `http://127.0.0.1:8787/?token=…` URL once — open it, and the token is exchanged for a
session and dropped from the address bar. Daily Mode is the calm default (one chat stream, a
quiet status bar with an emergency Stop); the Gate is the priority surface where risky actions
wait for your explicit, audited approval (the same permission model as the REPL — the UI can't
bypass it). Vault (review the KB queue), Tasks, Memory, Meetings, Hub (connector status), Lab
(eval history), and Trace (live tool/sub-agent activity) are a click away; Debug Mode reveals
telemetry without adding any capability. Leave `ui.enabled: false` and no server is built.

**Delegation** (`sub_agents.enabled: true`): ask Jarvis to "research X and Y in
parallel using sub-agents and compare them" — it spawns scoped sub-agents (you
approve each spawn, seeing the full prompt and the tools it may use), their activity
renders inline as it happens, and it synthesizes one answer. A sub-agent runs with an
isolated context and only the tools you granted; if it hits a risky action it prompts
you (labeled as the sub-agent's), and it can't spawn further, schedule tasks, or write
memory. Set `sub_agents.enabled: false` to remove delegation entirely.

**Tasks & scheduling:** ask Jarvis to "remind me to stretch in 20 minutes" or
"every weekday at 9am, summarize my notes.txt" — it schedules a reminder or an
unattended job (you approve the schedule, and the prompt shows the full payload
plus the computed local fire time). Reminders are delivered as a line at the
prompt; jobs run themselves in the background and report a result. Set
`scheduler.enabled: false` in `settings.yaml` to turn it off.

**Research & knowledge base** (needs `VOYAGE_API_KEY`): ask Jarvis to "ingest this
PDF" or "ingest https://… and summarize it into a wiki page" — sources are converted
to Markdown, stored with provenance, and indexed; later, "what do we know about X?"
searches them and answers with citations. The vault at `data/knowledge/wiki/` is
plain Obsidian-compatible Markdown (point `knowledge.dir` at a git-versioned dir or
an existing Obsidian vault to keep it under version control). `kb` shows stats,
`kb lint` reports issues, `kb rebuild` re-indexes, and `kb review` approves anything
a background job staged. Set `knowledge.enabled: false` to turn it off.

**Long-term memory** (needs `VOYAGE_API_KEY`): tell Jarvis something worth keeping
and it asks before saving it; on exit it reflects over the session and stores
durable facts. Next session, relevant memories are recalled automatically. Try:
tell it your favorite editor, `exit`, restart, and ask what your favorite editor
is — it answers from memory. Set `memory.enabled: false` in `settings.yaml` (or
omit the key) to turn it off.

## Safety model

Every tool call passes through a **permission gate** before it runs, and every
model call, tool call, and permission decision is written to an append-only JSON
audit log at `logs/jarvis-YYYY-MM-DD.jsonl`, correlated by a per-turn `trace_id`.

- **Decisions are `allow` / `ask` / `deny`**, configured in
  [`config/permissions.yaml`](config/permissions.yaml). Safe defaults: reads are
  allowed; writes, shell, and network (`web_search` / `web_fetch`) ask first.
- **Secrets are off-limits by a code floor.** Reads *and* writes of credential
  paths (`.env`, SSH/GPG keys, `.aws/credentials`, `.npmrc`, `*.pem`, …) are
  denied outright — a floor in `jarvis/paths.py` that policy can extend but not
  disable. Committed templates like `.env.example` are the one exception.
- **Filesystem writes** are checked against an allowlist — a write outside it is
  escalated to `ask`, never allowed silently. The gate and the tools resolve every
  path the *same* way (against the workspace root, collapsing `..` and symlinks),
  so an approval and the action it authorizes always refer to the same file.
- **Shell commands** are refined by longest-prefix rules (e.g. `git status` allow,
  `rm ` ask), matched at a token boundary. An `allow` is downgraded to `ask` if the
  command contains shell metacharacters (`; | & > <` …), so an allowlisted prefix
  can't smuggle a chained command past the gate. A tool-level `deny` is absolute.
- **"Always allow"** at the prompt persists the narrowest rule that covers it (a
  shell prefix, a resolved write directory, or a whole tool), and refuses to
  persist an over-broad grant (a drive root or your home directory).
- **File reads are bounded** to a byte ceiling read straight from disk, so a huge
  file can't exhaust memory or evict the conversation.
- **Memory writes are guarded against poisoning.** The `remember` tool asks first
  (a memory persists into every future prompt, so a fetched web page can't silently
  plant one), showing the full content at the approval prompt. End-of-session
  reflection — which forms most memories — strips tool-result bodies before the
  extractor sees them and only keeps facts the *user* stated, so untrusted fetched
  content can't be laundered into permanent memory ([ADR-0002](docs/decisions/0002-reflection-writes-bypass-the-gate.md)).
- **Scheduling a task asks, and can never be "always"-allowed.** `schedule_task` is
  a deferred-execution sink (the payload later runs with tools), so the approval
  prompt shows the full untruncated payload and the computed local fire time, and
  the "always" shortcut is refused for it.
- **Unattended jobs run under a stricter gate** ([ADR-0003](docs/decisions/0003-unattended-runs-deny-and-demote.md)):
  with no human to prompt, every `ask` becomes a `deny`; and — because it's the real
  escalation channel — an interactive "always allow" for `run_shell` / `write_file`
  does *not* extend to background runs (opt in explicitly via
  `scheduler.unattended_allow_tools`). Memory/scheduling meta-tools are hard-denied,
  so a job can't schedule more jobs or write memory on its own. Background sessions
  are marked `kind='task'`, so they never hijack `--resume` or feed reflection.
- **Ingesting into the knowledge base is gated, sandboxed, and provenance-tracked**
  ([ADR-0004](docs/decisions/0004-converters-are-gated-io-and-the-kb-is-a-contained-injection-sink.md)):
  a converter opening an attacker-supplied file is gated like a read (sensitive-path
  floor on `ingest_source`'s `path`) and runs in a killable subprocess with
  decompression-bomb caps; web URLs are SSRF-guarded (no loopback/private hosts, on
  every redirect hop). The KB is a contained injection sink — citations and
  front-matter are derived from the database, never from content; excerpts are
  delimited as untrusted; `write_file` can't write into the KB dir (use the tracked
  `write_wiki_page`); and unattended ingests are quarantined `unreviewed` until you
  run `kb review`.
- **Delegating to a sub-agent is doubly gated** ([ADR-0006](docs/decisions/0006-sub-agents-are-scoped-visible-and-doubly-gated.md)):
  `spawn_agent` asks (and is never "always"-able — the approval shows the full task
  prompt and the child's tool scope), and then *every* tool call the child makes still
  passes a `SubAgentGate` that can only tighten the parent's gate — it hard-denies
  recursion and the meta tools, enforces the child's tool scope, and preserves every
  floor (sensitive paths, write allowlist, shell metacharacters). A child's risky call
  forwards to you like any other, with a run-scoped "a" that grants a narrow *pattern*
  (a host, a directory — never `run_shell`/`write_file`) and is never persisted. Children
  can't spawn (depth 1, enforced three ways) and can't run unattended (`spawn_agent` is
  hard-denied for background jobs). Nothing is hidden: child activity renders inline,
  the transcript is a `kind='subagent'` session (never resumed, never reflected), and an
  `agent_runs` row links parent and child by trace id — see `agents`.
- **Voice is an untrusted, read-only surface** ([ADR-0007](docs/decisions/0007-voice-is-an-untrusted-read-only-surface.md)):
  a microphone is an open channel to anyone in the room, so a finalized transcript enters the
  model wrapped in the same untrusted-content framing as a fetched page (instructions inside
  it are content to weigh, not commands). Risky actions are **never approved by voice** — the
  injected `VoiceApprover` escalates every `ask` to an on-screen confirmation and is
  fail-closed (no positively-available screen ⇒ deny; there is no path by which a spoken
  "yes" can approve). There is **no unattended mic** (push-to-talk only; wake-word is
  deferred), and the spoken channel obeys a **TTS-privacy rule** (never voices secrets,
  tokens, commands, file contents, message bodies, or the details of a risky action). Cloud
  STT/TTS is off unless `voice.cloud_providers` is set, and any audio/text egress is logged.
- **The workstation UI is an authenticated local peer, not a public surface**
  ([ADR-0008](docs/decisions/0008-the-workstation-ui-is-an-authenticated-local-peer.md)): it
  binds loopback only (a non-loopback host is refused at config load), earns TTY-equivalent
  authority via a per-launch token exchanged for an `HttpOnly; SameSite=Strict` session (clean
  URL, no token in history/logs), and enforces a Host allowlist (anti DNS-rebinding), an Origin
  check (anti-CSRF), a strict `Content-Security-Policy`, `Referrer-Policy: no-referrer`, and
  **no CORS at all**. It reaches tools only through `AgentLoop` under the same gate; its
  mutations are a **closed, route-pinned set** of existing human-authority ops. Approvals are
  **replay-proof** — resolvable only by an authenticated click carrying a single-use nonce
  minted over a live socket after the modal is shown, so neither a spoken "yes" nor a stale
  page can approve — and the UI is voice's **fail-closed screen** (no live watching surface ⇒
  the action is denied). Debug Mode reveals telemetry but adds no capability; a secret value
  never crosses the wire (Hub shows presence booleans only). Both pinned by tests.
- **Tool failures, denials, and unknown tools become results the model reads** and
  recovers from — they never crash the session.

## Configuration

Non-secret settings (model IDs, limits, paths) live in
[`config/settings.yaml`](config/settings.yaml); each has a code default so the app
runs without the file. Quality-first: the most capable model at every tier
(`claude-opus-4-8` for reasoning, `claude-sonnet-5` for background work), adaptive
thinking, and high effort — API cost is treated as observability, not a constraint.

## Project layout

```
src/jarvis/
  cli/          REPL + rich rendering + background job execution (jobs.py)
  core/         agent loop, context/compaction, model clients, prompts, events
  tools/        Tool base, registry (+ScopedRegistry), executor, builtin/ (filesystem, shell, web, memory, tasks, knowledge, agents)
  permissions/  policy + gate + unattended gate + sub-agent gate (the double gate)
  memory/       long-term memory: store, embeddings, service, reflection
  scheduler/    tasks & scheduling: store, triggers, service, background runner
  knowledge/    research + wiki: store, chunking, converters (+ sandbox worker), links, service
  agents/       multi-agent delegation: SubAgentService + agent_runs audit store
  voice/        push-to-talk voice: approver (screen escalation), session, calm renderer, STT/TTS adapters, meeting capture
  ui/           workstation UI: auth + server (FastAPI), approvals (nonce), turn session + event stream, read models, voice screen, static/ (hand-written frontend)
  net.py        SSRF guard (shared by web fetch + knowledge ingest)
  persistence/  SQLite sessions/messages/memories/tasks/kb/agent_runs + migrations
  observability/ structured logging + cost accounting
  config.py     settings + secrets   ·   paths.py  path resolution + secret floor
tests/          unit tests + evals/ (live smoke scenarios)
docs/           PLAN, PLAN-2-memory, PLAN-3-tasks, PLAN-4-knowledge, architecture, learning notes, decisions/ (ADRs)
```

## License

MIT
