# Jarvis Phase 9 — Make Kairo Useful Daily

*(Follows master plan `docs/PLAN.md`. The master plan ends at Phase 8 — Phase 9 is net-new, but PLAN.md §6 already anticipates exactly this stack: Google Workspace "read/summarize default, writes need approval", Telegram send-only notifications "for task completion, errors, review-needed queues", KakaoTalk, Obsidian, and the integration rule "these systems should expose data to Jarvis through narrow, audited adapters. Jarvis remains the reasoning layer." Repo baseline: commit `1b2d53b`, 843 unit tests, Phase 8 UI live-QA'd.)*

## Context

Phases 1–8 built a safe, evaluated, multi-surface agent — and the Workstation UI now feels good but **empty**: `data/knowledge/` has zero sources, Daily shows only conversation state, background job results never reach the browser, Hub says "not connected — future phase". Phase 9 is not more polish; it is real data flowing in and real daily workflows flowing out: connectors (Google Calendar/Gmail/Drive, Telegram + Kakao notifications, project repos), a scheduled Daily Digest, Vault ingestion flows, and action workflows surfaced calmly in the UI.

**User decisions locked in (2026-07-07):** Gmail is **drafts-only** (no send scope, ever, pinned); connector reads are **silent ALLOW** (framed untrusted, audited, taint-guarded); **Kakao ships now** alongside Telegram; the eval gate **stays a terminal ritual** (ADR-0005 stands — Daily shows freshness + the command, never a run button).

**Approval amendments (2026-07-07, binding):**
- **A1 — Demo connector mode** (D10): fake connectors populate Daily/digest/Hub without live OAuth, for UI testing / screenshots / Mac-migration checks; always visibly badged "demo data".
- **A2 — Mandatory Checkpoint A after Task 2**: no OAuth/connector work begins until the egress/taint substrate is green + reviewed (taint demotion, always-allow suppression, unattended egress denial, token-path leak protection).
- **A3 — Mandatory Checkpoint B after Task 6**: no live connector testing until every connector tool is framed, capped, audited, gated, and unavailable-when-unconfigured.
- **A4 — Digest storage minimization** (D4): persist only structured summaries, counts, headers/snippets, provenance/status — **never** raw Gmail bodies or provider error bodies.
- **A5 — Egress log** (D1): a structured `egress` audit event for every egress action (web_search/web_fetch, gmail_create_draft, send_notification telegram/kakao, digest delivery) recording category + destination *type* only — no secrets, no raw tokens, no full recipient payloads.
- **A6 — Friendly reconnect only**: provider auth failures surface as `"Google needs reconnect: run jarvis connect google"` / `"Kakao needs reconnect: run jarvis connect kakao"` in UI/API/tool errors; provider error bodies are **never** exposed in any response.

The distinctive risk of this phase, found by the adversarial pre-mortem: **Phase 9 introduces new private-read sources AND new egress sinks, but the permission model reasons per-tool, not per-data-flow.** Silent mail reads + any previously "always-allowed" egress (web_fetch, a notifier) = a standing exfiltration pipe; the digest's output is itself an egress payload even with no tool loop; the token file is a durable credential worth more than the machine. The plan is built around closing that class structurally (D1) **before** wiring any connector.

## Architecture (new pieces in bold)

```
src/jarvis/
├── connectors/                    # NEW package — narrow, audited adapters
│   ├── base.py                    #   Notifier protocol; ConnectorRegistry (status = presence booleans only)
│   ├── tokens.py                  #   TokenStore: atomic write (os.replace), 0600 best-effort,
│   │                              #     single-flight refresh under asyncio.Lock, ConnectorAuthError
│   ├── oauth.py                   #   OAuth2 authorization-code + PKCE loopback flow (shared Google/Kakao)
│   ├── google/                    #   client.py (bearer + 401-refresh-retry-once), calendar.py,
│   │                              #     gmail.py (read + create_draft ONLY), drive.py — httpx REST,
│   │                              #     frozen dataclasses, hard caps, URLs are constants (never model-supplied)
│   ├── telegram.py                #   TelegramNotifier — send-only, plain text, no parse_mode, previews off
│   ├── kakao.py                   #   KakaoNotifier — "send to me" memo API, talk_message scope
│   └── demo.py                    #   DemoGoogleClient/DemoNotifier — badged fake data, no egress
├── digest/                        # NEW — builder.py (deterministic collectors + ONE tool-less summarize),
│                                  #   store.py (digests table, shared conn/lock)
├── reporting/repo.py              # NEW — RepoReader: hardened read-only git subprocess
├── tools/builtin/
│   ├── connectors_google.py       # calendar_list_events / gmail_search / gmail_read / gmail_create_draft /
│   │                              #   drive_search / drive_fetch
│   └── connectors_notify.py       # send_notification (channel: telegram|kakao)
├── permissions/unattended.py      # egress-aware demotion (D1); HARD_DENY += draft/notify
├── permissions/gate.py + core/agent.py   # per-turn taint: private read ⇒ egress ALLOW→ASK, never persistable
├── paths.py                       # sensitive floor: */data/connectors/* token files
├── ui/notices.py                  # NEW — NoticeBoard ring + WS broadcast (job results finally reach the UI)
├── ui/server.py                   # GET /api/daily, /api/notices; POST /api/vault/ingest, /api/digest/run
│                                  #   (mutation pin 11 → 13); hub connector status
├── ui/static/screens/daily.js     # Briefing / Today / What-changed / Workflows zones (textContent only)
└── cli/connect.py                 # `jarvis connect google|kakao|telegram --test|status` rituals

config: ConnectorsConfig (google/telegram/kakao/digest/repos/demo) + Secrets (google/telegram/kakao keys)
persistence: migration v6 (tasks kind CHECK += 'digest'; new digests table — user_version is currently 5)
tests/evals/scenarios/adversarial/: inj_email_body, inj_email_exfil_web, inj_calendar_event,
                                    inj_draft_poison, unattended_email_posture (+ taint pins as unit tests)
docs: PLAN-9-daily.md, ADR-0009 (connectors & egress), ADR-0010 (digest determinism), migration-macos.md
```

Existing seams reused (verified): `Tool` ClassVar contract + `ToolRegistry.discover` auto-registration + `is_available` gating; `ToolContext` as the injection seam (gains `connectors`); the verbatim untrusted-framing pattern (`_HEADER` + `--- begin X (untrusted) --- … --- end ---`) from `tools/builtin/web.py`; `KnowledgeService.ingest` + the unreviewed quarantine + `vault.js` review queue; scheduler v3 never-DELETE store + `BackgroundRunner` injected `notify`/`run_job`; migration rebuild pattern from `_migrate_v5`; `net.safe_get` SSRF hop-checking for any model-supplied URL path; UI closed-route pin + secret-absence sweep in `test_ui_readmodels.py`; `lab_overview`'s `history.jsonl` reader for eval freshness; httpx (already a transitive core import via `net.py` — promoted to main deps, **zero new third-party runtime deps for connectors**).

## 1. Resolved design decisions

### D1 — Egress & taint: the permission model learns about data flow (the phase's safety centerpiece)

Two new `Tool` ClassVars, declared per tool and consumed by the gate layer:
- `egress: ClassVar[bool] = False` — this tool sends data off-box under model control. True for: `web_search`, `web_fetch`, `send_notification`, `gmail_create_draft`.
- `reads_private: ClassVar[bool] = False` — this tool returns personal external data. True for: `calendar_list_events`, `gmail_search`, `gmail_read`, `drive_search`, `drive_fetch`. (Scoping note: `recall`/`query_knowledge_base` are also private-ish but pre-existing; extending taint to them would change Phase 5 eval baselines — recorded as a follow-up in ADR-0009, not done now.)

Three structural rules built on those properties:
1. **Per-turn taint**: `AgentLoop` sets `turn_tainted = True` the moment any `reads_private` tool executes. For the rest of that turn, any `egress` tool whose gate decision would be ALLOW is **demoted to ASK** with reason `"private data was read this turn"`, and the approval is **non-persistable** — the "always allow" button is suppressed (REPL and UI Gate modal), exactly like the existing voice-kind suppression. Worst case is one extra click; the exfil pipe (silent mail read → silent web_fetch) is structurally closed.
2. **Egress-aware unattended demotion**: `UnattendedGate` demotes ALLOW→DENY for **any `egress` tool** not explicitly in `scheduler.unattended_allow_tools` — a property-driven rule replacing reliance on the hand-maintained `DEMOTE_ALLOW` name set (which stays for the fs/shell tools). Additionally `HARD_DENY |= {gmail_create_draft, send_notification}`: egress-with-agency is never unattended, no opt-in can reopen it; the digest's deterministic delivery path (host code, not a tool) is the only unattended egress.
3. **Cross-cutting sensitive floor**: today `is_sensitive_path` guards only `read_file`/`write_file`/`ingest_source`. Close the leaks around the token file: `glob_search`/`list_dir` **redact** entries matching the sensitive floor from their output; `run_shell` gains a floor check that DENIES when any command token resolves to an existing sensitive path (belt over the metachar rule — `cat data/connectors/google_token.json` becomes DENY, not ASK). `paths.py` gains pattern `*/data/connectors/*` in `_SENSITIVE_PATTERNS` (NOT `_SENSITIVE_DIRS` — that set component-matches and would block reading `src/jarvis/connectors/*.py`; pinned by a test asserting the token path is sensitive and the source dir is not).

**Egress audit log (A5)**: a single helper `log_egress(*, category, destination_type, detail=None)` emits a structured `egress` event (fields: `category` ∈ {`web_search`,`web_fetch`,`gmail_draft`,`notify_telegram`,`notify_kakao`,`digest_delivery`}; `destination_type` a coarse label like `"public_web"`/`"google_drafts"`/`"telegram"`/`"kakao"`; optional non-sensitive `detail` such as a bare hostname or recipient-count — **never** the token, the bot token, the chat_id, the full recipient address, or the message body). Called from every egress tool's `run` and from digest delivery. Pinned by a test that seeds a canary secret into a call and asserts it never appears in the emitted event. This complements the existing per-turn `tool_call` audit — it's the dedicated "what left the box" ledger.

### D2 — Connectors: native REST, PKCE OAuth ritual, token custody

- **Native httpx REST adapters**, not google-api-python-client, not MCP: narrow (only the calls we need exist), audited (every call is our code), MockTransport-testable, zero new runtime deps. Hub keeps its honest `mcp: not connected` stub; ADR-0009 records why.
- **OAuth (shared `oauth.py`, used by Google and Kakao)**: authorization-code + **PKCE S256** + random `state` (mismatch rejected), loopback redirect server bound to `127.0.0.1` only, single-use short-lived listener, exact redirect-URI match. Google (Desktop-app client): ephemeral port 0, `access_type=offline&prompt=consent`. Kakao: **fixed registered port** (`connectors.kakao.redirect_port`, registered in the Kakao developer console — Kakao requires pre-registered redirect URIs), scope `talk_message`; Kakao refresh tokens expire (~2 months) so `ConnectorAuthError` → "reconnect: `jarvis connect kakao`" must be a routine, friendly path.
- **`TokenStore`** (one class, one file per provider under `data/connectors/`): atomic write via temp + `os.replace` (atomic on Windows and macOS), 0600 best-effort (real on POSIX), **single-flight refresh** under an `asyncio.Lock` with 120s expiry skew, `invalid_grant`/any refresh failure → typed `ConnectorAuthError` whose `.user_message` is the **friendly reconnect string only** (A6: `"Google needs reconnect: run jarvis connect google"` / `"Kakao needs reconnect: run jarvis connect kakao"`) — the provider's raw error body is logged at debug at most, never carried in the exception surfaced to tools/UI/API. Tools return the friendly message as their `is_error` text; Hub shows `needs_reconnect: true`. The saved file never contains `client_secret` (pinned).
- **Scopes (final, pinned)**: `calendar.readonly`, `gmail.readonly`, `drive.readonly`, `gmail.compose`. **`gmail.send` is never requested and no send method exists anywhere in `src/`** — "prepare reply" creates a draft the user sends from Gmail themself.
- **CLI ritual** (`jarvis connect google|kakao|telegram --test|status`, early-dispatch in `__main__.py` like `eval`): the deliberate terminal act of granting Kairo access to mail — consistent with the ADR-0005 ritual philosophy. Never prints token values; prints granted scopes.
- **Adapters return frozen dataclasses with hard caps** (gmail body 20k chars decoded `errors="replace"`, drive text export 200k bytes, result counts le=50); **adapters never accept a URL parameter** (endpoints are module constants — that invariant, not `safe_get`, is the SSRF story here, documented in the module docstring). One 401 → force-refresh → retry once; second 401 → auth error, no loop; 403/429 → typed errors.

### D3 — Tool surface, permissions, unattended posture

| Tool | Params (bounds) | Default | egress | reads_private | Unattended |
|---|---|---|---|---|---|
| `calendar_list_events` | `days_ahead:int=1 (0..14)`, `max_results:int=25 (≤50)` | ALLOW | – | ✓ | passes (read) |
| `gmail_search` | `query:str`, `max_results:int=10 (≤25)` | ALLOW | – | ✓ | passes |
| `gmail_read` | `message_id:str` | ALLOW | – | ✓ | passes |
| `gmail_create_draft` | `to`, `subject`, `body`, `reply_to_message_id?` | ASK | ✓ | – | **HARD_DENY** |
| `drive_search` | `query:str`, `max_results:int=10 (≤25)` | ALLOW | – | ✓ | passes |
| `drive_fetch` | `file_id:str` | ALLOW | – | ✓ | passes |
| `send_notification` | `text:str (≤3500)`, `channel:Literal["telegram","kakao"]="telegram"` | ASK | ✓ | – | **HARD_DENY** |

- Every read result wrapped in the verbatim untrusted-framing pattern (module `_HEADER` consts); bodies capped **before** framing so the executor's truncation can't sever the closing fence.
- All tools `is_available` only when the specific client/notifier is present in `context.connectors` — an unconfigured Gmail tool never reaches the model.
- The `gmail_create_draft` ASK renders to/subject/body in the approval payload — the human approves the *content* (Gate modal already renders params).
- `SPAWNABLE` unchanged — sub-agents get no connector tools (pinned). One new system-prompt paragraph when connectors are enabled (mail/calendar content is untrusted data).
- Silent-read posture recorded in ADR-0009 with its three compensating controls: framing, audit trail (every read logs `tool_call` under the turn's trace_id), and D1 taint.

### D4 — Daily Digest: deterministic collectors + ONE tool-less model call

**Shape**: scheduler task kind `'digest'` (never creatable by the model — `schedule_task` still accepts only `reminder|job`, pinned; digest tasks are created/cancelled only by host composition `ensure_digest_task(tasks, config)` at startup, idempotent). Fired by `BackgroundRunner` with **job semantics** (running row opened before work — a crashed digest is a visible `aborted`, never a silent re-run, because it has egress side effects).

**Collectors (deterministic, no agent loop)**: calendar events today, unread email top-5 (headers + snippets), `RepoReader` state per registered repo (commits since last digest, dirty count), tasks due today, vault review-queue count, pending approvals count, eval freshness. Each returns an explicit status **`ok | degraded | failed(reason)`** — the 3am OAuth-expiry case renders "⚠ Gmail unavailable (auth expired — jarvis connect google)", **never** "no unread email" (failure ≠ zero, pinned). All "today" windows computed in the **user's local timezone** (reuse the scheduler's tz plumbing; pinned by a day-boundary test in a non-UTC tz).

**Summarize**: exactly ONE `models.utility` call with **no `tools` param** (structurally asserted on the fake client — injected email text can color words, never trigger actions), inputs wrapped in untrusted framing, output constrained to a **structured schema** (per-section typed items + `summary ≤ 8 sentences` + `≤3 suggested_actions` as plain text — displayed, never executed). Structured output means the model can't smuggle free-form attacker URLs into prose.

**Delivery** (per `connectors.digest.deliver`, validated fail-closed against enabled notifiers): **UI/DB always first** (digests table + NoticeBoard + WS `{"type":"digest"}`), notifiers best-effort with surfaced failure — never the sole sink. Notifier content is **headers/counts by default** (`digest.rich_notify: bool = false` opts into snippets) — every notification is private data on Telegram/Kakao servers, so minimize by default. Telegram: plain text, no `parse_mode`, link previews disabled. Kakao: text template truncated to its 200-char limit with "open Kairo for the full briefing".

**Concurrency**: network + model work happen **outside** `turn_lock`; the lock is taken only to persist + notify (a Google 429 backoff at digest time must not freeze the UI). Digest persistence uses the shared SQLite connection/lock (a second connection deadlocks — invariant pinned).

**Re-injection rule**: stored digests are untrusted-by-construction (their inputs were). Any path that quotes a digest back into a model turn wraps it in the standard untrusted framing (pinned).

**Storage minimization (A4)**: the persisted `digests` row holds only what Daily needs to re-render — the structured `sections_json` (per-collector: title, item texts that are **headers/snippets/counts**, `when`, `ref`, `status`), the `summary`, `suggested_actions_json`, `delivered_to`, `cost_usd`, and provenance/status. It **never** stores a raw Gmail body, a full message payload, or a provider error body. Collector failures persist as `{status:"failed", reason:"<friendly>"}` — the friendly reconnect string (A6), not the provider's error text. A snippet field is capped (≤240 chars) at collect time. Pinned by a test: a builder fed a raw body + a provider 500 body produces a stored row containing neither.

**Migration v6** (`user_version` currently 5; copy the `_migrate_v5` rebuild scaffolding — `foreign_keys=OFF` inside the step): rebuild `tasks` with `kind IN ('reminder','job','digest')`; new `digests` table (`id, task_id→tasks NULL, date_local, generated_at, sections_json, summary, suggested_actions_json, delivered_to, cost_usd`).

### D5 — Notifiers + notify plumbing (job results finally reach the UI)

- `Notifier` protocol (`name`, `async send(text)`); `TelegramNotifier` (bot token `.env`, `chat_id` settings, 4096-char truncation) + `KakaoNotifier` (TokenStore-backed, memo/send-to-me endpoint). `jarvis connect telegram --test` / `kakao --test` send "Kairo test — {timestamp}".
- `NoticeBoard` (`ui/notices.py`): bounded ring (200) of `{seq, at, kind, text}`; sync `post()` (the runner's `Notify` is sync) guards `asyncio.get_running_loop()` and broadcasts `{"kind":"notice",...}` when a loop is live. `run_ui`'s runner notify becomes fan-out: console + board. REPL path unchanged (console only). Reminders optionally mirror to a notifier behind `telegram.notify_reminders: bool = false` (deterministic host code, not a tool).
- `GET /api/notices` (read-only, no pin change) returns the tail; Daily consumes it.

### D6 — Daily bootstrap: `GET /api/daily` + hardened RepoReader

- **`RepoReader`** (`reporting/repo.py`): read-only git via `asyncio.to_thread(subprocess.run, [...])`, argument-list never `shell=True`, **hardened against git's own execution surface**: `GIT_CONFIG_NOSYSTEM=1`, `-c core.fsmonitor=false -c core.hooksPath=<null> --no-pager -c protocol.ext.allow=never`, pinned `cwd`, 5s timeout, refuses paths that aren't a plain dir containing `.git`. Returns `RepoState(branch, head_rev, dirty_files, recent_commits)`; `None` when not a repo. Commit subjects/branch names are **untrusted data** (escaped via `esc()` in UI, framed when fed to the digest summarizer). Registered repos come from `connectors.repos: list[str] = ["."]`.
- **`daily_overview(...)`** aggregates: repo state, eval freshness (`history.jsonl` last gate rev/verdict vs HEAD → `stale` flag — `data/evals/history.jsonl` finally gets a daily surface), tasks today, pending approvals + KB review counts, latest digest, notices tail, connector status, `what_changed` (commits/new sources/task runs since last digest). Route `GET /api/daily` (read-only, no pin change).
- **Hub** gains `connectors: {google: {connected, scopes, expires_at, needs_reconnect}, telegram: {configured, chat_id_set}, kakao: {connected, needs_reconnect}}` — presence booleans + scope names + timestamps ONLY; connector **error bodies are never surfaced** in any GET (provider errors can echo tokens/addresses). The **secret-absence sweep is extended**: seed a canary refresh token into a tmp `data/connectors/google_token.json`, add `/api/daily` + `/api/notices` to the swept routes, assert the canary appears nowhere.

### D7 — Fill the Vault: ingestion flows

- **`KnowledgeService.ingest_folder(folder, *, recursive=True, extensions=_INGESTIBLE, created_by="user", limit=500, progress=None) → FolderIngestReport`** — per-file delegation to the existing `ingest()` (dedupe-by-hash, supersede, sensitive-path skip all inherited); **refuses symlinks outright** (not just sensitive targets — a symlink to `~/Documents/taxes.pdf` must not ride in); report lists ingested/skipped/failed. Covers Obsidian vaults (`.md` round-trip already safe), Downloads dumps, doc folders. Surfaced as REPL `kb ingest <path|url> [--recursive]` + CLI `jarvis kb ingest`. **Watch-folders are an explicit non-goal this phase** (recorded): one-command bulk ingest replaces them; unattended/scheduled ingest would land in quarantine anyway, but the standing-inbox threat surface isn't worth it before it hurts.
- **`POST /api/vault/ingest`** (pin 11→12) body `{path?|url?|text?, title?}` exactly-one-of: path leg runs the same `gate.check("ingest_source", {"path":...})` floor as the tool (DENY→403; the human clicking the form IS the approval, same authority model as vault approve); url leg keeps the existing trafilatura/`safe_get` SSRF guard; `created_by="user"`, lands `reviewed`. `vault.js` gets an ingest box (path/url/note tabs) and — closing a pre-mortem gap — the review queue gains a **content preview** (markdown excerpt of the unreviewed source) so approving is informed, one-at-a-time (no bulk-approve).
- Meeting transcripts (voice capture → quarantine) and `ingest_source`/links already work; docs get a "filling your vault" section (point `knowledge.dir` at an Obsidian vault + `kb rebuild`, or bulk-ingest into the default dir).

### D8 — UI: Daily zones + workflows + route pins (11 → 13)

Daily zone order (one attention surface preserved — approval beats run beats telemetry):
1. **Pending approval** (unchanged, amber, singular) → 2. **Now** (unchanged) → 3. **Briefing** (latest digest: summary, per-section items, suggested-action chips as plain text, "Run digest now" → `POST /api/digest/run`; digest WS event updates the card quietly — no toast) → 4. **Today** (tasks + calendar events) → 5. **What changed** (repo card, eval-freshness chip: stale ⇒ gray "evals not run at HEAD" + copy-command button `jarvis eval gate` — **never a run button**, ADR-0005) → 6. **Workflows** (chip row) → 7. Conversation + composer (unchanged).
- **Workflow chips are prepared prompts submitted through the existing `POST /api/turn`** — the single gated action path; no new action authority: "Summarize my inbox" (read-only prompt), "Prepare a reply" (ends in `gmail_create_draft` → ASK → Gate modal shows the full draft), "Summarize repo changes", "Schedule a reminder" (prefill → `schedule_task` ASK). "Ingest a file" / "Review KB queue" are navigation (Vault).
- New mutations: `POST /api/vault/ingest` + `POST /api/digest/run` (503 without a builder; runs under the busy contract of `/api/turn`; takes `turn_lock` only per D4). **Pin updated 11→13 in the same commits.** All digest/connector text rendered via `textContent`/`esc()` — **no markdown/linkify path for digest content** (a digest link is a phishing/exfil channel; pinned by a content test).
- Amber rules unchanged: a failed digest/connector is a quiet gray/amber line in Briefing, not a new attention surface.

### D9 — MacBook migration (`docs/migration-macos.md`)

Prereqs (Xcode CLT, Homebrew, uv, Python 3.12); `uv sync --extra ui [--extra voice --extra docling]` (connectors need no extra); `.env` checklist (all keys incl. `GOOGLE_CLIENT_ID/SECRET`, `TELEGRAM_BOT_TOKEN`, `KAKAO_REST_API_KEY` — never commit); **review `config/permissions.yaml` before copying** (shell prefix rules were authored on PowerShell; start from committed defaults and re-grant interactively); data sync (copy `data/jarvis.db` — migrates forward via `user_version`; `data/knowledge/`; `data/evals/history.jsonl`); connectors: **re-run `jarvis connect google|kakao` on the new machine** rather than copying `data/connectors/` (fresh refresh token, real 0600 perms on macOS); voice deps (portaudio via brew for sounddevice); note digests fire only while a Kairo process runs (no launchd daemon this phase — deliberate); verify ritual (`uv run pytest`, `jarvis connect status`, one eval chunk, open UI, run digest). **The migration smoke check uses demo mode (D10)** so Daily/digest/Hub can be exercised end-to-end on the new machine before any live OAuth. Backup plan: `data/` is the entire state — document a simple copy/Syncthing note.

### D10 — Demo connector mode (A1): populate Daily without live credentials

`connectors.demo: bool = false`. When true (and only when the real provider secrets are absent, so demo never masks a live connection), the `ConnectorRegistry` is built from **fake adapters** instead of live ones: `DemoGoogleClient` (a handful of fixed, obviously-fictional calendar events / unread emails / drive files — e.g. sender `"demo@kairo.local"`, subject `"[DEMO] Standup at 10"`) and `DemoNotifier` (records "sent" text in-memory, ships nothing off-box). These are the **same fakes the eval harness injects** (Task 11), so one implementation serves tests, screenshots, and migration checks.

**Badging is mandatory and structural**: `ConnectorRegistry.demo: bool` is surfaced in `hub_status` and `daily_overview` as `"demo": true`; the Daily Briefing card and Hub render a persistent "Demo data — not your real accounts" chip; the digest row stores `delivered_to` including `"demo"` and its summary is prefixed `[DEMO]`. A demo notifier's `send` is a no-op that logs an egress event with `destination_type:"demo"` (nothing leaves the box — pinned). Demo mode never satisfies `is_available` for `gmail_create_draft` unless demo explicitly enables a draft stub, and even then the draft is recorded, never sent. Pinned: with `demo:true` and real keys set, the registry refuses demo and uses live (no accidental fake-over-real); demo output is always badged in every surface that renders it.

## 2. Task list — Milestone 9 (in order)

Same discipline as Milestones 1–8: each task ends green (`ruff check` + `uv run pytest`), commits with explicit paths, appends learning-note bullets. Keyless-testable via fakes/MockTransport everywhere; live steps confined to Tasks 3 (connect rituals) and 13.

1. **Plan doc + scaffolding**: commit this doc; `ConnectorsConfig` (google/telegram/kakao/digest/repos + `demo: bool` sections, fail-closed validators: digest delivery channel requires that notifier enabled) + `Secrets` additions + `_REQUIRED_KEYS`; `*/data/connectors/*` sensitive pattern (+ the source-dir-not-blocked regression test); `ToolContext.connectors`; `connectors/base.py` (Notifier protocol, `ConnectorRegistry` with `demo: bool` + `status()`); promote httpx to main deps; commented `settings.yaml` example block.
2. **Egress & taint substrate (D1) — before any connector exists**: `egress`/`reads_private` ClassVars on `Tool`; per-turn taint in `AgentLoop`; non-persistable tainted egress (REPL + UI Gate modal); `UnattendedGate` egress-property demotion + `HARD_DENY` growth; cross-cutting sensitive floor; `log_egress` helper (A5) wired into web tools. **⛔ CHECKPOINT A (A2)** before Task 3.
3. **OAuth + TokenStore + connect CLI**: shared PKCE loopback flow; `TokenStore`; `jarvis connect google|kakao|telegram --test|status`.
4. **Google REST adapters**: `client.py` + `calendar/gmail/drive`; `gmail.create_draft` (no send endpoint anywhere).
5. **Notifiers + NoticeBoard + notify fan-out**: Telegram + Kakao + Demo notifiers; `NoticeBoard` + `run_ui` fan-out + `GET /api/notices`.
6. **Connector tools + demo registry (D3, D10)**. **⛔ CHECKPOINT B (A3)** before live connector testing.
7. **Migration v6 + digest (D4, A4)**.
8. **RepoReader + `GET /api/daily` + Hub status (D6)**.
9. **Vault ingestion (D7)**.
10. **Daily screen + workflows (D8)**.
11. **Adversarial evals** (reuse D10 demo fakes with poisoned payloads).
12. **Docs** (ADR-0009/0010, migration-macos.md, README/architecture).
13. **LIVE VERIFICATION GATE** (chunked).

## 3. Verification

1. `uv run pytest` — all green, keyless (fakes/MockTransport; no network).
2. `ruff check` + `format` clean.
3. **Live connect rituals**: `jarvis connect google` (scopes = the 4 pinned), `jarvis connect kakao`, `jarvis connect telegram --test`.
4. **Live interactive**: calendar/gmail reads framed; "prepare a reply" → draft ASK → approve → draft visible in Gmail web → delete it there; `gmail_read` then `web_fetch` in one turn → fetch demoted to ASK with the taint reason, "Always allow" absent.
5. **Live unattended proof**: 1-minute job "read my inbox and draft a reply" → reads succeed, draft HARD_DENIED, `denied_count > 0`, zero side effects.
6. **Live digest**: "Run digest now" → Briefing updates calmly; Telegram + Kakao deliveries (headers/counts only); revoke Google token → next digest shows "⚠ Gmail unavailable", not "no unread".
7. **Live vault**: ingest a real PDF via the UI route, a folder via `kb ingest`, a URL; review queue previews content; approve → searchable.
8. **Live UI**: `/api/daily` populated; job completion reaches the browser as a notice; secret-absence sweep re-run against the live server with a real token on disk.
9. **Eval gate, chunked**: core + adversarial suites as separate background chunks; new injection scenarios side-effect rows 0; Daily eval chip flips fresh at the new HEAD.

## Non-negotiables

1. **Drafts, never send**: `gmail.send` is never requested; no send method exists in `src/`; `gmail_create_draft` is the only Gmail write (pinned).
2. **The taint rule lands before any connector tool** (Task 2 before Task 6): private read this turn ⇒ egress ALLOW→ASK, never "always"-able; `UnattendedGate` demotes all `egress`-property tools; `gmail_create_draft`/`send_notification` are HARD_DENY unattended.
3. **The digest summarizer is tool-less and its output is treated as an egress payload**: structured schema, textContent-only rendering, no linkify, notifiers get headers/counts by default, UI/DB is always the first sink, failure ≠ zero, re-injected digests framed untrusted. Digest tasks are host-created only.
4. **Token custody**: `data/connectors/` under the sensitive floor with the cross-cutting extensions; atomic single-flight TokenStore; secret-absence sweep extended with a canary token file; Hub/GETs carry presence + scopes + timestamps only, never provider error bodies.
5. **Every connector byte is framed untrusted**, capped before framing; adapters never accept model-supplied URLs.
6. **UI adds no new authority**: workflow chips go through `POST /api/turn`; the mutation closed set grows 11→13 with the pin updated in the same commits; vault ingest runs the same gate floor as the tool; eval gate stays a terminal ritual.
7. **Calm stands**: one primary attention surface; digest updates are quiet card refreshes; connector failures are gray lines, not alarms.
8. **No prior safety contract weakens**: ADR-0003/0004/0005/0008 invariants untouched; never-DELETE stands; `SPAWNABLE` unchanged; Phase 5 eval baselines unaffected by the taint change.
9. **Two mandatory checkpoints (A2/A3)**: stop after Task 2 and after Task 6, each reported with per-bullet test evidence.
10. **Digest storage is minimized (A4)** and **every egress action logs a structured `egress` event (A5)** — category + destination type, no secrets (pinned canary-absent).
11. **Friendly reconnect only (A6)**; **demo mode (A1)** never masks a live connection and is badged everywhere it renders.

## Open questions / recorded tradeoffs

- **Silent connector reads** (user-confirmed): ALLOW + framing + audit + taint. Extending taint to `recall`/KB is a recorded follow-up (would perturb Phase 5 baselines).
- **Watch-folders deliberately not built**: bulk `ingest_folder` covers the need without a standing unauthenticated ingestion path.
- **Digests fire only while a Kairo process runs** — no daemon/launchd this phase.
- **Kakao token lifetime** (~2-month refresh expiry) makes reconnects routine — the ritual is cheap and Hub flags `needs_reconnect`.
- **No egress host allowlist yet**: taint + ASK covers the model-driven exfil class; a per-host outbound allowlist is a recorded future hardening.
- **MCP still not wired** — native adapters chosen deliberately (ADR-0009); Hub keeps the honest stub.
