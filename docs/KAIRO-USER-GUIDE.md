# Kairo User Guide

Kairo is a local-first AI workstation: project-based chat, knowledge and memory, safe agent teams,
connectors, voice, cost controls, and an approval Gate in one local workspace. The product is
**Kairo**; the current command name remains `jarvis`.

## Quick start

From the repository checkout:

```powershell
uv sync --extra ui
uv run jarvis doctor
uv run jarvis --ui
```

`jarvis doctor` is local and read-only: it reports credential **names** and presence, optional
Python extras, an existing database's schema/integrity, and disk headroom. It makes no changes and
never contacts a provider. Kairo then prints a tokened loopback URL. Open it once in your browser;
the token is exchanged for a local session and removed from the URL. Create or open a project, then
start in **Chat**. Choose **Auto** to let Kairo classify a turn before selecting a suitable allowed
model, or select a manual model when you want a specific one. Ask for a plan or task, then review
any Gate item on screen.

## Main surfaces

- **Chat** — the primary place to talk, dictate, and see a conversation's project, model, mode,
  save state, and subtle cost information.
- **Daily** — a calm briefing: attention, next tasks, latest notification, and a quick path back
  to Chat.
- **Projects / Workspace** — choose a workspace and work with its chats, tasks, knowledge,
  artifacts, and scoped activity.
- **Knowledge** — Vault sources, memory, and the graph. Add sources deliberately; connector text
  is treated as untrusted input.
- **Artifacts** — generated files and outputs.
- **Studio** — run and inspect AI team orchestration. It is where workflows are controlled.
- **Office** — a visual, read-only view of team status. Clicking an agent inspects or navigates;
  it does not execute work.
- **Hub / Connectors** — account status, scopes, allowed actions, and CLI reconnect guidance.
- **Costs** — ledger-backed chat, orchestration, provider/model/team, and project spend.
- **Settings** — local appearance and capability/status information.
- **Notifications / Attention** — pending approval and important attended work. **Debug** and
  **Lab** are optional troubleshooting surfaces, not daily navigation.

## Connectors

Use the terminal rituals rather than pasting credentials into chat:

```powershell
uv run jarvis connect google
uv run jarvis connect status
uv run jarvis connect telegram --test
uv run jarvis connect kakao
uv run jarvis connect kakao --test
```

Google Calendar can read and prepare approved create/update actions. Gmail can read mail and create
or update **drafts only**—Kairo cannot send email. Drive can read permitted content and work with
Kairo-created Docs through the narrow `drive.file` scope; it does not ask for broad Drive access.
Writes follow preview → approve → execute. Re-run the relevant connect command when Hub says
**Needs reconnect**.

Never paste API keys, OAuth refresh tokens, client secrets, or connector token files into chat or
screenshots. The Hub shows presence and scope names, never token values.

## Voice

The composer has two modes:

- **Dictation** records and transcribes into the composer for you to edit. It does not auto-send.
- **Conversation** records, transcribes, sends through normal Chat, then shows/speaks a safe
  caption when available.

Voice uses the safe caption/TTS path; it never speaks raw tool payloads, approval details, secrets,
commands, or risky action contents. Voice prepares work. The authenticated screen commits risky
actions—there is no voice-only approval.

## Safety in plain English

Kairo asks before risky actions. The Gate is the only approval path and approvals are screen-based.
Connector writes are two-phase: preview, approve, then execute. Connector and web content are
untrusted data, not instructions. Private context stays behind provider-routing boundaries; an
unapproved provider is never silently used for it. Dreaming produces proposals/artifacts only and
is not scheduled unless explicitly approved.

## Cost control

Auto routing can use an inexpensive allowed model for simple work and escalate for harder work.
Normal browser chat has a hard per-turn cap; if Kairo cannot verify the selected model's price, it
refuses before calling it. Chat shows the cap, last cost, and selected provider/model quietly; use
**Costs** for fuller ledger detail.

To avoid expensive work, keep requests focused, use a lower-effort/manual model where appropriate,
and review Studio's budget warnings before launching a large team run. Orchestration has its own
budget controls; it is separate from ordinary chat.

## Backup and recovery

Create a local recovery snapshot before migrations, packaging, or major changes:

```powershell
uv run jarvis backup create
uv run jarvis backup verify data/backups/<timestamp>-manual-<id>
```

Backups live under `data/backups/` and include a consistent copy of `data/jarvis.db`, available
knowledge, generated artifacts, and `data/evals/history.jsonl`. Each manifest records creation
time, app/git revision, database version, included paths, file sizes, and SHA-256 hashes.

Backups deliberately exclude environment files (`.env`, `.env.*`, and `.envrc`), configuration
secrets, logs, `data/connectors/`, OAuth token files, and secret-shaped filenames. `verify` checks
hashes and opens a temporary database copy for SQLite integrity validation; it does not overwrite
live data. This MVP has no real restore command yet. Kairo also makes a conservative pre-migration
snapshot when it detects a real older database.

## Troubleshooting

- **Gmail or Calendar unavailable:** open Hub, then run `uv run jarvis connect status` or reconnect
  Google.
- **UI does not start:** ensure the UI extra is installed and `ui.enabled` is enabled in your local
  settings.
- **Model unavailable:** Hub can show a missing key, disabled provider, or unpriced model. Kairo
  fails closed instead of choosing an unsafe provider.
- **Voice unavailable:** check browser microphone permission and the voice status reason in Chat.
- **Budget cap hit:** shorten the request, start a new turn, or choose a lower-cost allowed model.
- **Gate approval appears stuck:** keep the local Kairo tab open and use the visible approval item;
  voice and background activity cannot approve it.

## Current limitations

Kairo's UI and mobile experience are still improving and it is not a native mobile app. Some
connector setup and tests remain CLI rituals. Gmail send is intentionally unsupported. Office is a
visual/status surface, not an action surface. Dreaming remains unscheduled. Backup restore is
verify/dry-run only in this MVP; it never overwrites your live data.
