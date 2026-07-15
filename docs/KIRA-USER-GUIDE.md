# Kira User Guide

Kira is a local-first, single-owner AI workplace for project-based chat, knowledge, memory, safe
agent teams, connectors, voice, attended automation, cost control, and explicit approvals. The
canonical command is `kira`.

## Quick start

From the repository checkout:

```powershell
uv sync --extra ui
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
uv run kira doctor
```

Set `ANTHROPIC_API_KEY` in `.env` before starting Kira; every interactive entrypoint requires it.
Add other keys only for the capabilities you choose to enable.

A clean checkout keeps the workplace disabled. Enable it deliberately in `config/settings.yaml`:

```yaml
ui:
  enabled: true
```

Then start Kira:

```powershell
uv run kira --ui
```

`kira doctor` is local and read-only. It reports credential names and presence, optional Python
extras, database identity/schema/integrity, and disk headroom. It makes no changes, prints no secret,
and never contacts a provider.

On first start, open the printed one-use setup link within 10 minutes and create the single owner
account. Use a non-common passphrase of at least 15 characters. The launch link creates only an
enrollment grant; application access begins after successful enrollment.

Later starts use the normal sign-in page. The separately labeled recovery link changes the owner
password and revokes every older session. Credentials use Argon2id, browser bearer values are stored
only as SHA-256 digests, and sessions have a sliding 30-day idle lifetime with a hard 90-day absolute
limit.

Create or open a project, then begin in **Chat**. Auto routing is recommended; manual selection is
available when a specific trusted model is required. Review risky work in Notifications/Gate.

## Main surfaces

- **Chat** — primary conversation, voice, project/model/mode context, attachments, progress, and cost.
- **Daily** — briefing, current work, tasks, notices, and project-assessment status.
- **Notifications** — one surface for live approvals, write intents, graph reviews, proposals, and alerts.
- **Projects / Workspace** — scoped chats, tasks, knowledge, artifacts, activity, services, and costs.
- **Knowledge** — Vault sources, memory, and the graph; imported content remains untrusted.
- **Artifacts** — generated files and registered outputs.
- **Studio** — launch, inspect, cancel, or resume eligible interrupted post-synthesis runs.
- **Office** — read-only visual team status and navigation.
- **Hub** — connector/provider/service truth and reconnect guidance.
- **Costs** — ledger-backed spend, request health, estimates, and calibration.
- **Settings** — appearance plus read-only capability and policy truth.
- **Lab / Trace** — eval and diagnostic evidence, not daily authority surfaces.

A project's **Archive & start fresh** action requires the exact project name and fresh owner-password
step-up. It archives the current workspace for audit, creates a clean successor, and can retain the
linked repository references. It does not silently delete history or repository content.

## Connectors

Stop the Kira runtime before changing connector credentials or OAuth state:

```powershell
uv run kira connect google
uv run kira connect status
uv run kira connect telegram --test
uv run kira connect kakao
uv run kira connect kakao --test
```

Google Calendar supports read plus separately approved create, update, and cancel intents. Kira can
create and edit Kira-created Google Docs under `drive.file`. Gmail can read permitted mail and create
or update **drafts only**—Kira cannot send email. Writes follow preview → approve → execute.

Reconnect when Hub reports **Needs reconnect**. Never paste API keys, OAuth refresh tokens, client
secrets, connector token files, or browser session values into chat or screenshots. Hub reports
presence and scope names, never credential values.

### Telegram Remote Operator

Kira can answer a deliberately bounded set of Telegram messages while the local runtime is running.
Configure one positive private-chat ID; groups and channels are refused. At every controller start,
Kira discards messages retained while it was offline or disabled. Send a fresh `/start` from that
exact chat only after the runtime reports the channel ready.

The fixed read-only commands include:

- `/status` — Kira and scheduler state.
- `/tasks` — active task metadata.
- `/inbox [terms]` — a bounded, local-day view of recent Gmail sender/subject/snippet metadata.
- `/calendar` — minimized next-24-hour timing/count information.
- `/briefing` — count-only combined context.
- `/clear` — remove the memory-only delivered-turn window and typed inbox reference.

Ordinary delivered conversation keeps a short role-correct window in RAM, bounded by time, turns,
and characters. It is never written to SQLite and cannot grant authority. Failed deliveries are not
remembered. Numbered inbox references are also memory-only, expire quickly, and never send Gmail
message identifiers or full bodies to the remote model.

Optional attachments accept one bounded image, supported document, voice note, or audio file.
Images are validated/resized, documents use the existing sandbox, and audio is transcribed locally.
Raw files and derived text are discarded after the turn. Attachment content is untrusted and has no
proposal, approval, write, shell, scheduling, connector, send, live-search, or other egress authority.

Optional live search is available only to ordinary text turns and performs at most one bounded
public query for a message. The query leaves the workplace without per-query approval or semantic
DLP; results are framed as untrusted third-party data. It cannot fetch arbitrary URLs or read local
files, mail, memory, connectors, project content, or attachment content.

News-PDF requests use a separate host-owned approval path. Kira first displays an `N-...` code with
the exact date, scope, search/source/PDF limits, retention, and fixed destination. No search, model
call, file write, or Telegram document send happens until the exact `/approve N-...` code returns.
Ambiguous sends are never retried automatically because that could duplicate delivery.

With the project operator enabled, `/projects` lists registered aliases. A request such as:

```text
Kira, inspect the frontend API wiring in project Kira, fix what is broken,
and update me every 15 minutes.
```

creates one inert proposal. The reply shows the exact project, instruction, schedule, cadence, and
a random expiring code. Nothing is scheduled until the displayed `/approve CODE` is returned.
`/deny CODE` leaves it inert; `/approvals`, `/jobs`, and `/cancel ID` expose bounded lifecycle
controls.

An approved project job receives only `read_file`, `list_dir`, `glob_search`, `write_file`, and
`run_shell`. Its selected active project is pinned as routing and prompt context, not as a filesystem
sandbox; normal workspace-root path resolution, sensitive-path floors, and permission policy remain
the enforcement boundary. It receives no memory, email, connector, browser, sub-agent, or
notification tools. A write or shell call always parks the exact saved continuation and sends a
second minimized preview. Only a new code bound to that exact tool input resumes it. Casual
confirmation, reused/expired codes, or codes for changed input grant nothing.

Remote Operator controls the local running process; it is not a cloud wake-up service or remote
browser. See [`REMOTE-OPERATOR.md`](REMOTE-OPERATOR.md) for configuration details.

## Voice

The browser composer has two modes:

- **Dictation** records and transcribes into the composer for editing. It does not auto-send.
- **Conversation** records, transcribes, sends through normal Chat, then shows or speaks a safe
  caption when available.

Voice never speaks raw tool payloads, approval details, secrets, commands, or risky action content.
Voice prepares work; an attended terminal or authenticated browser screen commits it. There is
**no voice-only approval**. Terminal push-to-talk is available with `uv sync --extra voice` and
`uv run kira --voice`.

## Safety in plain English

Kira asks before risky actions. The terminal REPL can ask interactively; browser approvals require
an authenticated, live screen. Telegram Remote Operator accepts only expiring, single-use codes
bound to an exact stored proposal or parked tool input.

Connector writes are two-phase: preview → approve → execute. Connector, web, attachment, and imported
document content are untrusted data, not instructions. Private context remains behind provider-routing
boundaries. Dreaming produces proposals and artifacts only; it cannot execute an action or approve
itself.

## Attention and attended dreaming

Notifications combines approvals, reviews, proposals, and alerts without duplicating their
authority. Marking an attention row done, dismissed, or snoozed does not execute its underlying
proposal; acceptance still uses the existing gated source action.

Stop Kira before running one attended dreaming command:

```powershell
uv run kira dream run morning_briefing
uv run kira dream run nightly_review
uv run kira dream run bottleneck
uv run kira dream run roi_summary
uv run kira dream run self_improvement
```

Each command runs one budget-capped, proposal-only job and files its result in Notifications. These
jobs are **not scheduled unattended** before Checkpoint K is explicitly approved.

## Cost control

Auto routing can use an inexpensive allowed model for simple work and escalate for harder or more
sensitive work. Unknown availability or pricing fails closed. Ordinary browser chat defaults to
eight iterations, 4,096 output tokens, and a $0.75 priced per-turn hard stop. Configuration may
narrow these limits. Chat shows the cap, last cost, and selected provider/model quietly; Costs holds
the fuller ledger detail.

Manual main-chat choices remain the trusted Anthropic set. Auto currently routes eligible text-only
simple turns to Gemini and other turns to Anthropic; OpenAI is private-context eligible but is not an
Auto or manual main-chat destination. Qwen, DeepSeek, and Z.ai are non-private scoped workers and
cannot hold final authority. Orchestration has separate up-front reservation and budget controls.

## Backup and recovery

Stop Kira before creating a backup:

```powershell
uv run kira backup create
uv run kira backup verify data/backups/kira-backup-<timestamp>-manual-<id>
```

New archives use **Kira backup format v2** and contain a consistent `data/kira.db`, available
knowledge, artifacts, and `data/evals/history.jsonl`. The manifest records format/application,
creation time, app and Git revisions, database version, included roots, sizes, and SHA-256 hashes.

OAuth token stores, `.env` files, configuration, logs, and secret-shaped filenames are excluded.
Backups can still contain private workplace content and authentication verifier records; protect
them as sensitive private data. Verification checks inventory, hashes, SQLite readability, schema,
and integrity without modifying live state. **Restore is not supported.**

For a complete fresh start, stop Kira and run:

```powershell
uv run kira reset data
```

The command requires an attended terminal, the exact displayed confirmation phrase, and the current
owner password. It quarantines old runtime roots, writes a recovery manifest, builds and verifies a
fresh schema-v33 database, and preserves the checkout, `.env`, and configuration. It does not
hard-delete the prior data.

## Troubleshooting

- **UI does not start:** install the UI extra and set `ui.enabled: true`.
- **Sign-in fails:** use the normal login page; repeated failures are durably throttled.
- **Password recovery:** restart `uv run kira --ui` and use the separately labeled recovery-only link.
- **Kira may already be running:** stop the existing UI, REPL, voice, or maintenance command; one
  data root permits one owner process.
- **Connector command is blocked:** stop Kira before running `kira connect`.
- **Gmail or Calendar unavailable:** inspect Hub, then run `uv run kira connect status`.
- **Model unavailable:** inspect the missing credential, disabled provider, or unpriced-model reason.
- **Voice unavailable:** check browser microphone permission and Chat's voice reason.
- **Budget cap hit:** narrow the request or choose a cheaper allowed model.
- **Approval appears stuck:** keep the authenticated Kira tab open and use the visible approval item.
- **Backup creation is blocked:** stop Kira; verification remains read-only.

## Current limitations

Kira is not a native mobile application and its browser UI remains loopback-only. Telegram Remote
Operator is a bounded remote companion, not remote browser access or cloud wake-up. Some connector
and maintenance workflows remain CLI rituals. Gmail sending is intentionally unsupported. Office is
a visual/status surface. Dreaming is attended and unscheduled. Backup verification exists, but
restore is not supported.
