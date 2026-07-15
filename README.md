# Kira

Kira is a local-first AI workplace for project-based chat, knowledge, memory, safe automation,
connectors, voice, agent teams, and approval-gated work. Its agent loop, tool registry, permission
model, persistence, routing, and observability are implemented directly in this repository so the
authority boundary stays visible and testable.

The canonical command is `kira`. The exact `jarvis` command remains temporarily available as a
compatibility alias for existing launch scripts during the deprecation window.

Start with the [`Kira User Guide`](docs/KIRA-USER-GUIDE.md). Architecture and design history live in
[`docs/architecture.md`](docs/architecture.md), [`docs/PLAN.md`](docs/PLAN.md), and
[`docs/decisions/`](docs/decisions/). The [documentation index](docs/README.md) distinguishes
current operator guidance from preserved historical snapshots.

## Current status

**Kira 0.1.0** uses database schema **v33**. Phase 16 Tasks 1–9 are shipped: Kira has one unified
Notification Center, minimized attention routing, proposal-only dreaming builders, a tool cage,
budget controls, adversarial coverage, and an attended CLI. Development is intentionally stopped at
the mandatory **Checkpoint K** before Task 10. Dreaming is **NOT scheduled** and no unattended
dreaming schedule or Phase 16 closeout ADR exists yet.

Stop Kira before exercising one of the five proposal-only jobs manually:

```powershell
uv run kira dream run morning_briefing
uv run kira dream run nightly_review
uv run kira dream run bottleneck
uv run kira dream run roi_summary
uv run kira dream run self_improvement
```

Each command runs one attended job. It may create an untrusted proposal or artifact for review; it
cannot execute the proposal.

## Quick start

Requirements:

- [uv](https://docs.astral.sh/uv/) and Python 3.12+; the repository pins Python 3.13.
- PowerShell 7 (`pwsh`), including on macOS/Linux, because Kira's shell tool executes through it.
- The UI extra for the workplace, plus capability-specific extras/keys as needed.
- `ANTHROPIC_API_KEY` is required to start Kira. Tavily, Voyage, Gemini, OpenAI, ElevenLabs,
  and worker-provider keys are optional and capability-specific.

From a PowerShell terminal in the checkout:

```powershell
uv sync --extra ui
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
uv run kira doctor
uv run kira --ui
```

A clean checkout has `ui.enabled: false`. Set `ui.enabled: true` in
[`config/settings.yaml`](config/settings.yaml) before the last command. `kira doctor` is local and
read-only: it checks configuration, optional dependencies, database integrity/schema, credential
presence by name, disk headroom, and never calls a provider or prints a secret.

On the first launch, Kira prints a one-use setup link valid for 10 minutes. Enroll the single owner
with a username and a non-common passphrase of at least 15 characters. Later launches use `/login`;
the separately labeled process-bound link is for recovery, and recovery revokes previous sessions.
Passwords are verified with Argon2id. The database stores password verifiers and SHA-256 bearer
digests, never plaintext passwords or replayable session values. Sessions expire after 30 idle days
and always expire after 90 days.

The browser opens in **Chat**. Create or select a project, leave the model on **Auto** for
cost-aware routing, and review any risky action in the Notification Center/Gate.

## Workplace surfaces

| Surface | Purpose |
| --- | --- |
| Chat | Primary conversation, project/model/mode state, uploads, dictation, and safe captions. |
| Daily | Briefing, attention summary, tasks, artifacts, and a quick path back to Chat. |
| Notification Center | Live Gate approvals, durable write intents, graph suggestions, proposals, and alerts. |
| Projects / Workspace | Project-scoped chats, repositories, tasks, knowledge, memory, artifacts, runs, and costs. |
| Knowledge / Graph | Provenance-tracked sources, Obsidian-compatible pages, memory, and reviewed graph data. |
| Artifacts | Registered generated outputs served through hardened, type/size-bounded routes. |
| Studio / Office | Launch and inspect agent-team workflows; Office is a read-only visual status layer. |
| Hub | Connector, provider, service, voice, and capability truth with honest disabled reasons. |
| Costs | Ledger-backed spend by chat, provider/model, project, team, and orchestration run. |
| Settings / Debug / Lab | Appearance and capability status; optional diagnostics add no authority. |

Kira uses hand-written, build-free browser modules with no CDN dependency. Noir, light, and neon
themes share the same product structure and approval boundary. Chat is the shipped default screen;
Daily is a secondary command center.

## Everyday commands

```powershell
uv run kira                 # terminal assistant
uv run kira --resume        # resume the latest eligible conversation
uv run kira --voice         # terminal push-to-talk; requires the voice extra and config flag
uv run kira --ui            # local workplace; requires the UI extra and config flag
uv run kira --version

uv run pytest -q
uv run ruff check .
uv run kira eval gate       # keyless cassette replay; $0 by default
uv run kira eval gate --live --runs 1 --max-cost-usd 1.00
```

`--live` calls providers and records cassettes. `--record` fills only missing cassettes. Always use
an explicit hard cap for live work; bare `kira eval gate` is the deterministic replay gate.

## Models and cost controls

Auto routing classifies intent, difficulty, sensitivity, and tool need, then applies a deterministic
policy. Classifier failure escalates safely; unavailable or unpriced routes fail closed. Manual
main-chat model choices remain Anthropic-only. Auto can route eligible tool-free simple turns to
Gemini and otherwise escalates within Anthropic. The `private_ok` catalog includes Anthropic, Gemini,
and OpenAI, but OpenAI is an opt-in backup/utility route rather than a default Auto or manual chat
pick. Trusted authority remains Anthropic-only. Qwen, DeepSeek, and Z.ai are non-private scoped
workers; they cannot become the final planner, reviewer, or private main-chat route.

Ordinary browser chat has independent hard defaults of 8 iterations, 4,096 output tokens, and
$0.75 per turn. Preflight refuses an unpriced or over-cap call. Every successful model call is
ledgered, and the UI shows quiet per-turn context while Costs provides the full breakdown.

## Connectors and Remote Operator

Stop the Kira runtime before changing connector grants:

```powershell
uv run kira connect google
uv run kira connect status
uv run kira connect telegram --test
uv run kira connect kakao
uv run kira connect kakao --test
```

Google Calendar can read and prepare approved event changes. Gmail can read and create/update
**drafts only**; Kira has no mail-send scope or method. Drive reads permitted content and works with
Kira-created Docs through the narrow `drive.file` scope. Connector writes always use
preview → approve → execute. Hub shows credential presence and scope names, never token values.

Telegram notifications are minimized. The optional Remote Operator accepts one configured private
chat, exposes a bounded deterministic command set, keeps only a short memory-only delivered-turn
window, and can prepare one inert project proposal. Execution still requires an expiring,
single-use code bound to the exact proposal or parked tool input. It is remote control of the local
running process, not a cloud wake-up service. See
[`docs/REMOTE-OPERATOR.md`](docs/REMOTE-OPERATOR.md) and the
[`Kira User Guide`](docs/KIRA-USER-GUIDE.md).

## Projects, attention, and recovery

Project **Archive & start fresh** preserves the predecessor's audit history, creates a clean active
successor, and requires fresh owner-password step-up plus the exact project name. It does not delete
linked repositories.

For a whole-instance reset, stop Kira first:

```powershell
uv run kira reset data
```

The reset is offline, owner-password and exact-phrase gated, and quarantine-first. It moves runtime
state into a recoverable quarantine before creating a clean identity; it is not a hard delete.

Create a private backup before migrations, packaging, or major changes, also with Kira stopped:

```powershell
uv run kira backup create
uv run kira backup verify data/backups/kira-backup-<timestamp>-manual-<id>
```

Kira backup format v2 includes a consistent `data/kira.db` plus available knowledge, artifacts, and
eval history. It excludes known environment files, configuration, logs, connector token stores, and
secret-shaped filenames. User-authored content can still contain private material, so protect every
backup accordingly. Verification checks hashes and SQLite integrity without overwriting live data.
Restore is not supported. Legacy v1 archives remain verification-compatible, and an offline startup
can safely promote a single legacy `data/jarvis.db` into `data/kira.db`; ambiguous dual identities
fail closed.

## Safety model

- Every tool call passes through the shared `allow` / `ask` / `deny` PermissionGate.
- Sensitive paths are denied by a code-level floor that configuration can only narrow further.
- Filesystem writes, shell commands, schedules, connector writes, spawning, and spend are reviewed
  through exact, auditable payloads. Broad or ambiguous grants are refused.
- Web, connector, converted-document, attachment, and model-generated content is framed as
  untrusted data and cannot grant authority.
- Private data can reach only approved `private_ok` routes; trusted decision authority remains
  Anthropic-only.
- Unattended scheduled jobs use a stricter gate: interactive asks become denies, risky persistent
  grants do not carry over, and parked calls resume only from the exact saved continuation.
- Voice never approves. Its risky work escalates to an attended terminal or authenticated browser
  screen; Remote Operator uses exact expiring codes for its deliberately bounded path.
- Dreaming has an enumerated read-only cage, tool-less builders, a spending cap, quarantined output,
  no automatic context injection, and no schedule before Checkpoint K approval.
- Structured, rotating, compressed, redacted audit logs are written as
  `logs/kira-YYYY-MM-DD.jsonl`, correlated by `trace_id`. Legacy log names are compatibility reads.

See [`docs/architecture.md`](docs/architecture.md) and [`docs/decisions/`](docs/decisions/) for the
design rationale and security boundaries.

## Configuration and data

Non-secret settings live in [`config/settings.yaml`](config/settings.yaml), permissions in
[`config/permissions.yaml`](config/permissions.yaml), pricing in [`config/pricing.yaml`](config/pricing.yaml),
and secrets in `.env`. Never paste keys, OAuth refresh tokens, client secrets, session values, or
connector token files into chat, screenshots, issues, or logs.

Canonical runtime state lives under `data/`, with SQLite at `data/kira.db`. Knowledge pages are
plain Markdown and can point at an existing Obsidian vault. The graph exporter writes only its
reserved namespaces and only overwrites files carrying the canonical `generated_by: kira-graph`
marker; the old marker is accepted solely for safe migration.

## Project layout

```text
src/kira/       canonical Kira Python package
  actions/        previewed connector/write intents
  agents/         scoped sub-agent runs and audit records
  attention/      Notification Center, routing, dreaming cage/builders
  cli/            REPL, UI composition, connector/eval/backup/reset commands
  connectors/     narrow Google, Telegram, and Kakao adapters
  core/           agent loop, prompts, execution context, model clients
  graph/          derived/asserted graph, review, export, code dependencies
  intelligence/   read-only project assessment jobs and reports
  knowledge/      ingestion, sandboxed conversion, retrieval, wiki
  memory/         durable memory, embeddings, recall, reflection
  models/         provider registry, routing, prompt layout, context reuse
  orchestration/  project teams, workflow stages, budgets, review
  permissions/    shared gate plus unattended/sub-agent narrowing
  persistence/    SQLite, migrations, locks, backup/reset recovery
  projects/       project scope, snapshots, reset lifecycle
  remote/         bounded Telegram Remote Operator
  scheduler/      reminders, jobs, retries, durable parked approvals
  search/         unified search across first-party state
  services/       classified service catalog and adapters
  skills/         reviewed workplace skill packs
  tools/          registry, executor, and built-in capabilities
  ui/             owner auth, FastAPI server, read models, static workplace
  voice/          dictation/conversation, safe captions, local/cloud adapters
tests/            unit tests and deterministic/live eval harnesses
docs/             user guide, architecture, plans, ADRs, and verification evidence
```

The dated plans, ADRs, baselines, and verification reports preserve the exact names, commands,
paths, versions, and counts that were true at their checkpoints. They are historical evidence, not
the current operator guide.

## Current limitations

Kira is a local web workplace, not a native desktop or mobile app. Connector enrollment and several
recovery rituals remain CLI-only. Gmail sending is intentionally unsupported. Office is a status
surface, not an independent execution surface. Dreaming remains attended and unscheduled. Backup
verification is read-only and no restore command exists.

## License

MIT
