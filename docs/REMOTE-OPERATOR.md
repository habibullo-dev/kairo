# Kira Telegram Remote Operator

Remote Operator lets one allowlisted private Telegram chat inspect bounded workstation state,
prepare work, approve exact capabilities, observe progress, and cancel Remote Operator jobs on the
Kira process running on the owner's workstation. It is remote control of that local process, not a
cloud worker, remote browser, or wake-on-demand service.

Remote chat and Remote Operator are separate opt-ins. Remote chat provides deterministic reads and
a bounded utility-model reply. Enabling `operator.enabled` additionally exposes one inert proposal
tool; it does not give Telegram text or the model ambient execution authority.

## Enable it safely

1. Stop Kira before editing connector settings.
2. Put `TELEGRAM_BOT_TOKEN` in `.env`. Never put the bot token in YAML or commit it.
3. Configure one positive private Telegram chat id and explicitly enable the operator:

```yaml
connectors:
  telegram:
    remote_control:
      enabled: true
      allowed_chat_id: '123456789'
      operator:
        enabled: true
        approval_ttl_minutes: 15
        proposal_ttl_minutes: 30
        default_status_interval_minutes: 15
        max_active_jobs: 3
        live_web_search_enabled: false
        live_web_search_max_results: 5
        default_live_location: ''
        allowed_tools:
          - read_file
          - list_dir
          - glob_search
          - write_file
          - run_shell
      attachments:
        enabled: false
```

4. Run `uv run kira connect status`, then start Kira with `uv run kira --ui` (or the terminal
   runtime) and send `/start` from the exact allowlisted chat.

`connectors.telegram.enabled` and `connectors.telegram.chat_id` configure outbound notifications;
they do not grant inbound authority. After a whole-instance data reset, Telegram stays consent-
locked until `uv run kira connect telegram` succeeds for the new owner. The current reconnect
command also expects the outbound `TELEGRAM_CHAT_ID` or `connectors.telegram.chat_id` setting.

Remote Operator actions require the scheduler, background runner, and project service. If those are
unavailable, remote chat stays read-only. Optional live public search also requires
`TAVILY_API_KEY` and the bounded `web_search` adapter; missing either disables live search without
widening the remaining channel. When enabled, each search is ALLOW egress with no per-query approval
or semantic data-loss-prevention filter. It is available only to ordinary text turns; never put
secrets or private source text in a live-search request.

## What reaches a model

Slash commands and recognized natural-language status, task, inbox, calendar, briefing, approval,
project, and job reads are handled by host code. Other supported text may reach Kira's utility model
through the Anthropic client. That model receives at most 500 output tokens and a $0.25 hard
per-turn limit; the default controller admits at most 20 model messages per hour.

Ordinary delivered model turns may use a RAM-only conversation window: at most 4 delivered turns
and 6,000 combined characters by default. The window expires after the 30-minute reference TTL and
is cleared by `/clear`, shutdown, or restart. A turn enters that window only after its Telegram
reply is delivered; conversation text is not written to SQLite.

Optional attachments are not proposals or approvals. The default bounds are 20 MB per non-image
download, 5 MB for both the downloaded and normalized image, 50,000 extracted document characters,
and 600 seconds of audio. Document and audio staging files are removed after processing; audio
transcription is local. The normalized image or extracted text/transcript is then untrusted context
for the same bounded remote model and cannot expose proposal, filesystem, shell, scheduler,
connector, memory, approval, live-search, or other egress tools. Attachment turns remain
non-egressing even when text-chat live search is enabled.

## Authority flow

1. Host-owned commands are resolved without a model. Other fresh, allowlisted text may reach the
   bounded utility-model turn described above.
2. The turn has no tools by default. An explicitly enabled operator may expose
   `remote_propose_work`; separately enabled live search may expose `remote_live_search`. Each can
   be used at most once for that Telegram message.
3. The proposal tool stores an inert `job` or `reminder` proposal. It cannot schedule work, call
   another tool, or approve itself. Live search cannot fetch arbitrary URLs or access local/private
   sources.
4. The host renders the stored fields and a random 12-hex-character, single-use approval code.
   Proposals expire after 30 minutes by default; each code expires after 15 minutes by default.
5. `/approve CODE` atomically consumes the code once and transitions the exact proposal. Host code
   then attempts to schedule and durably bind the task. Active-job limits, scheduling errors,
   binding failures, or an interrupted bind close safely without pretending the job was queued.
6. A supplied project id, slug, or exact name is resolved only through Kira's active project store
   and the stored project id is pinned as task/session context. Omitting it selects global context.
   A project is not a filesystem sandbox: the normal workspace-root path resolver, sensitive-path
   floors, permission policy, and exact write/shell approvals remain the enforcement boundary.
7. The job runner recognizes the server-owned `remote_operator` origin and exposes only the
   configured subset of `read_file`, `list_dir`, `glob_search`, `write_file`, and `run_shell`.
8. When the unattended Gate returns ASK, the runner parks the exact provider tool-use block,
   canonical input hash, and saved transcript before any tool in that assistant batch executes.
   Telegram receives a separate code bound to that saved call.
9. Approving resumes the claimed continuation once. Denial completes the original occurrence
   without executing the call. Completion, failure, approval-needed, milestone, and capped heartbeat
   events are generated by host code.

Telegram approval previews are intentionally minimized, not full diffs. A shell preview includes
the working directory and at most the first 1,000 command characters; a write preview includes the
path, character count, and a short content digest rather than the content. The displayed input hash
is also only a prefix, although the stored capability binds the full canonical input hash. Deny any
unfamiliar or truncated operation and inspect it locally: Telegram proves which saved bytes a code
binds, but it is not a full content-review surface.

Proposals may be immediate, one-time, interval, or cron schedules. Status cadence defaults to 15
minutes; the accepted values are 0, 1, 5, 15, 30, and 60 minutes, where 0 means milestone-only. No
more than 3 approved Remote Operator jobs may be active by default, and periodic status updates
stop after 100 sends per proposal. Those periodic updates are generated by host code and spend no
model tokens.

## Commands

Host-owned read and context commands:

- `/status` — show whether Kira is working plus scheduler/project counts and operator mode.
- `/tasks` — summarize active scheduler tasks.
- `/inbox [filter]` — show a bounded recent inbox view when Google is connected.
- `/calendar` — show the next-24-hours calendar summary.
- `/briefing` — combine minimized status, inbox, calendar, and task counts.
- `/clear` — erase the RAM-only conversation and reference window.

The inbox, calendar, and briefing group has a separate default limit of 60 requests per hour;
`/status` and `/tasks` remain available when that limit is reached.

Operator lifecycle commands:

- `/projects` — list up to 20 active registered aliases; it cannot register a path.
- `/jobs` — show recent Remote Operator proposals and linked scheduler state, not a full run trace.
- `/approvals` — refresh codes for up to two pending Remote Operator proposals/tool calls.
- `/approve CODE` / `/deny CODE` — resolve one exact proposal, parked tool call, or separately
  prefixed News-PDF capability.
- `/cancel ID` — cancel one active Remote Operator job, where `ID` is the proposal/job number shown
  by `/jobs`, not the scheduler task id.
- `/news-pdf [public topic]` — when live search is configured, prepare a separate sourced-PDF
  proposal. Search, model work, file creation, and delivery begin only after its `N-...` code is
  approved.

Natural action requests can prepare at most one proposal per message. Natural language is never an
approval; only the exact slash-command code path resolves a stored capability. `/approvals` may also
show up to two independently bounded News-PDF approvals when that workflow is enabled.

## Security and restart invariants

- Only one configured positive private chat id is accepted. Group, channel, unknown-chat, retained,
  duplicate, unsupported update, and disabled attachment inputs cannot create work. Unknown chats
  receive no acknowledgement.
- At every controller start, Kira discards the entire Telegram backlog retained while it was offline
  or disabled. Send a fresh message only after Kira reports the channel ready. While running, Kira
  durably claims each update before handling it. A crash or delivery failure can lose a reply or the
  preview for an already stored inert proposal; resend the request or use `/approvals`. The claimed
  message and any future effect are never replayed automatically.
- Codes are random, stored only as SHA-256 hashes, expire, are single-use, and are invalidated when
  refreshed for the same subject.
- Proposal approval is separate from tool approval. Approving a job never pre-approves later writes
  or commands.
- The remote chat model never receives ordinary filesystem, shell, scheduler, project-content,
  memory, connector, sub-agent, arbitrary-fetch, notification, or approval tools. Its optional tools
  can only store one inert proposal and/or make one bounded public search.
- Live search is separately opt-in, normalizes and limits the query to 300 characters, fixes the
  result cap at 5 or fewer, and runs at most once per Telegram message. The query leaves the
  machine; egress is audited without logging the query, and returned snippets are explicitly framed
  as untrusted content. Query normalization collapses whitespace only, so the text-query bounds are
  not semantic DLP. Attachment turns have an empty tool registry and cannot reach live search.
- Remote scheduler tasks carry a server-owned `remote_operator` origin and cannot inherit an
  interactive session's provenance or full tool registry. Standing allows for side-effecting tools
  are demoted to exact-call asks; hard-denied and egress tools remain unavailable.
- Proposal title, instruction, schedule, status cadence, proposal/task binding, scheduler task,
  parked transcript, tool id/name/input, and canonical input hash are durable in SQLite. Telegram
  delivery failures cannot change job state or grant authority.
- Restart recovery restores monitors only for durably bound active jobs, cancels orphan
  `remote_operator` tasks, and marks approved-but-unbound proposals failed so the owner can resend.
  It never replays a Telegram message, proposal, or unapproved tool call.

The workstation must already be awake with Kira running. Remote Operator does not expose the UI
port, remotely wake the workstation, or provide a cloud execution service.
