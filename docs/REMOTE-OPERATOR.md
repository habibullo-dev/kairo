# Telegram Remote Operator protocol

Remote Operator lets one allowlisted private Telegram chat prepare, authorize, observe, and cancel
bounded work on the Kairo process running on the owner's workstation. It extends the read-only
Telegram companion without turning Telegram text or a utility-model response into ambient
execution authority.

## Authority flow

1. A fresh, allowlisted Telegram message reaches a stateless utility-model turn.
2. The turn has no tools by default. Configured instances may expose `remote_propose_work`,
   `remote_live_search`, or both. Live search is bounded to one public query for that message.
3. The proposal tool stores an inert proposal. It cannot create a scheduler task or call another
   tool. Live search cannot fetch arbitrary URLs or access local/private sources.
4. The host renders the stored fields and a random 12-hex-character, single-use approval code.
5. `/approve CODE` atomically consumes the code and creates a server-tagged scheduler task.
6. The job runner recognizes that server-owned origin and exposes only its configured local tool
   subset. The selected project id is loaded from Kairo's project store, never from a model path.
7. A risky tool request parks the exact provider tool-use block and canonical input hash before any
   tool in that batch executes. Telegram receives a separate code bound to that saved call.
8. Approving resumes the claimed continuation once. Denial completes the original occurrence
   without executing the call. Completion, failure, approval-needed, and heartbeat events are sent
   by host code.

## Security invariants

- Only one configured positive private chat id is accepted. Group, channel, unknown-chat, retained,
  duplicate, and non-text updates cannot create work.
- Conversational language is not consent. Only `/approve CODE` can resolve a stored capability.
- Codes are random, stored only as SHA-256 hashes, expire, are single-use, and are invalidated when
  refreshed for the same subject.
- Proposal approval is separate from tool approval. Approving a job never pre-approves later writes
  or commands.
- The remote model never receives filesystem, shell, scheduler, project-content, memory, connector,
  sub-agent, arbitrary-fetch, or approval tools. Its optional tools store an inert proposal and/or
  make one bounded public search.
- Live search is separately opt-in, requires the local Tavily credential, normalizes and limits the
  query to 300 characters, fixes the result cap at five or fewer, and runs at most once per Telegram
  message. The query leaves the machine, egress is audited without logging the query, and returned
  snippets remain explicitly framed as untrusted content.
- Remote scheduler tasks carry a server-owned `remote_operator` origin and a fixed project foreign
  key. They cannot inherit an interactive session's provenance or full tool registry.
- Default execution tools are `read_file`, `list_dir`, `glob_search`, `write_file`, and `run_shell`.
  The configuration validator only allows a subset of that closed set.
- Existing standing allows for side-effecting tools are demoted to exact-call asks for remote jobs.
  Hard-denied and egress tools remain unavailable.
- The scheduler task, parked transcript, tool id/name/input, and input hash are durable in SQLite.
  Restart recovery restores status monitors but never replays an unapproved call.
- Heartbeats are host-generated and capped; they do not spend model tokens. Telegram delivery
  failures cannot change job state or grant authority.

## Operator commands

- `/projects` — list active registered aliases (no arbitrary path registration).
- `/jobs` — show recent Remote Operator proposals and scheduler state.
- `/approvals` — issue fresh codes for up to two pending proposals/tool calls.
- `/approve CODE` / `/deny CODE` — resolve one exact capability.
- `/cancel ID` — cancel one active Remote Operator job created through this protocol.

The local workstation must already be awake with Kairo running. Remote Operator does not expose the
UI port, remotely wake Windows, or provide a cloud execution service.
