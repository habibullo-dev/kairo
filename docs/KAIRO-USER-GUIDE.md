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
screenshots. The Hub shows presence and scope names, never token values. Ordinary Telegram
notifications remain count-only. If you separately enable Remote Operator, Telegram also carries
the exact work proposals and minimized tool previews that you explicitly approve with expiring,
single-use codes.

### Telegram remote control

Kairo can also answer a small set of Telegram messages while the Kairo process is running at home.
Put the bot token in `.env`, then configure one **private** Telegram chat id in
`config/settings.yaml`:

```yaml
connectors:
  telegram:
    remote_control:
      enabled: true
      allowed_chat_id: '123456789'
      operator:
        enabled: true
        default_status_interval_minutes: 15
        allowed_status_intervals: [0, 1, 5, 15, 30, 60]
        max_active_jobs: 3
```

Start Kairo normally (`uv run jarvis --ui` or `uv run jarvis`), then send a fresh `/start` from
that exact chat. `/status` reports whether Kairo and its scheduler are running; `/tasks` lists
active task metadata; `/inbox` reports only the unread Inbox count; `/calendar` reports the number
of events in the next 24 hours and the next start time; and `/briefing` combines those read-only
summaries. Retained Telegram messages from before the first enable are intentionally discarded, so
they cannot become work after a restart. Ordinary remote questions and proposal preparation use
Kairo's economical utility model, preserving the expensive Fable model for its deliberate
skills-authoring workflow.

With `operator.enabled: true`, use `/projects` to see the aliases already registered in Kairo.
Then speak naturally, for example:

```text
Kairo, inspect the frontend API wiring in project jarvis, fix what is broken,
and update me every 15 minutes.
```

The model can only prepare one inert proposal. Kairo replies with the exact project, instruction,
schedule, update cadence, and a random code. Nothing is scheduled until you send the displayed
`/approve CODE`; `/deny CODE` leaves it inert. Use `/jobs` for job state, `/approvals` to mint fresh
codes for pending proposals or tool calls, and `/cancel ID` to cancel an active Remote Operator
job. Refreshing `/approvals` invalidates an older unconsumed code for the same item.

An approved job receives only `read_file`, `list_dir`, `glob_search`, `write_file`, and
`run_shell`. It has the selected registered project's linked-repository context, but it receives no
memory, connectors, email, browser, sub-agent, or notification tools. Reads can proceed; a write or
shell command parks the exact saved model continuation and sends a second preview with the tool,
path or command, and input hash. Only the new `/approve CODE` resumes that exact call. A casual
"yes", a repeated code, an expired code, or a code for a changed call grants nothing.

Status intervals are host-generated heartbeats, not extra model calls. `0` means milestone-only;
the configured positive values are minutes. Kairo also reports start, completion, failure, and
approval-needed milestones. The workstation and Kairo process must remain running; this feature is
remote control of your local process, not a cloud wake-up service.

`/inbox`, `/calendar`, and `/briefing` require the existing Google connector to be enabled and
connected locally (`connectors.google.enabled: true`, Google client credentials in `.env`, then
`uv run jarvis connect google`). They return no message sender, subject, snippet, body, event title,
location, attendee, or identifier. Google checks have a separate 60-per-hour default limit; change
`connectors.telegram.remote_control.max_read_requests_per_hour` only if you need a different safe
ceiling.

If you already use Kairo's Telegram notifications for your personal conversation, reuse that same
positive chat ID as `allowed_chat_id`. Do not use a group or channel ID (those are normally
negative): remote control deliberately accepts one private chat only.

When Remote Operator is disabled, remote chat has no Kairo tools, memory, project context, approval
route, shell, scheduler, or connector access. The deterministic workspace commands perform fixed
read-only calls outside the model and disclose only the minimized summaries above. When Remote
Operator is enabled, the proposal and exact-code protocol is the only additional authority path.

## Voice

The composer has two modes:

- **Dictation** records and transcribes into the composer for you to edit. It does not auto-send.
- **Conversation** records, transcribes, sends through normal Chat, then shows/speaks a safe
  caption when available.

Voice uses the safe caption/TTS path; it never speaks raw tool payloads, approval details, secrets,
commands, or risky action contents. Voice prepares work. The authenticated screen commits risky
actions—there is no voice-only approval.

## Safety in plain English

Kairo asks before risky actions. Approvals are screen-based by default; the optional Telegram Remote
Operator accepts only expiring single-use codes bound to an exact proposal or parked tool input.
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
