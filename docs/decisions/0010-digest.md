# ADR-0010: The Daily Digest is deterministic collectors + one tool-less summarize

- **Status:** Accepted
- **Date:** 2026-07-07
- **Context phase:** Phase 9 (make Kairo useful daily)

## Context

The Daily Digest reads the user's morning world — calendar, unread email, repo state, open
tasks, the review queue, eval freshness — and turns it into a calm briefing, on a schedule.
The obvious implementation is "an unattended agent that reads your inbox and writes a summary."
That shape is wrong here: an agent loop reading attacker-influenced email with tools available
is exactly the injection-into-action surface Phase 9 spent its safety budget closing. A digest
runs unattended (no human to approve), so ADR-0003's headless-deny applies — but a tool loop
whose *inputs* are hostile is a standing risk even with ASK tools denied.

## Decision

### 1. No agent loop. Deterministic collectors + one tool-less model call

Collectors are plain async functions that fetch structured data (calendar events, unread email
headers/snippets, `RepoReader` state, tasks due today, KB review count). Exactly **one**
`models.utility` call turns them into prose — with **no `tools` parameter** (`tools=[]`,
asserted structurally on the fake client). The summarizer therefore *cannot* call anything:
injected email text can colour the briefing's wording, but there is no path from that text to an
action. The inputs are wrapped in untrusted-content framing regardless.

Because the summarizer's **output** is itself a payload (it is rendered in the UI and sent to
notifiers), "no tool loop" is necessary but not sufficient — see (3)/(4).

### 2. Failure is visible, never "zero results"

Each collector returns an explicit status — `ok` / `degraded` / `failed(reason)`. A 3am OAuth
expiry renders "⚠ Gmail unavailable — kira connect google", never "no unread email". Conflating
a failed fetch with zero results would quietly tell the user "all clear" when it isn't. The
failure `reason` is the friendly reconnect string (ADR-0009), never a provider error body.

### 3. Storage is minimized (amendment A4)

The persisted `digests` row holds only what Daily needs to re-render: the structured sections
(titles, header/snippet/count item texts, `when`/`ref`/`status`), the summary, suggested actions,
delivery record, and provenance. It **never** stores a raw Gmail body (the email collector uses
search snippets, capped 240 chars — it never fetches full bodies) or a provider error body
(pinned by a test that feeds both and asserts the stored row contains neither).

### 4. Delivery: UI/DB first, notifiers best-effort, output treated as egress

The digest is persisted and posted to the UI (the guaranteed sink) **before** any notifier send.
Notifier delivery is best-effort, its failure surfaced (a quiet notice), never the sole sink.
Notifier content is headers/counts by default (`rich_notify` opts into snippets) — every
notification is private data on a third-party server, so minimize. The UI renders digest text via
`textContent` only — never HTML, never linkified — because a digest link would be a phishing/
exfil surface (the summarizer's output is untrusted-by-construction; a re-injection into a later
turn is framed untrusted too). Telegram sends plain text, no `parse_mode`, previews off.

### 5. Host-created only; job semantics; off the turn lock

Digest tasks (scheduler kind `digest`) are created solely by host composition
(`ensure_digest_task`, idempotent) — the `schedule_task` tool never accepts kind `digest`, so the
model can't create or multiply digest egress. The runner fires them with **job semantics** (the
running row opens before work, so a crash is a visible `aborted`, never a silent re-run of
egress). The collectors + model call run **outside** the turn lock (a Google 429 backoff must not
freeze the UI); the lock is taken only to persist + notify. Persistence uses the shared SQLite
connection/lock (a second connection deadlocks).

## Consequences

- The digest is safe to run unattended against hostile inputs because it structurally cannot act
  on them — the strongest possible version of "read-only".
- Digests fire only while a Kairo process runs (no launchd/systemd daemon this phase — the
  single-process invariant stands); a missed digest is recorded, not silently skipped.
- The repo/eval collectors are optional inputs (wired from `RepoReader` + `history.jsonl`); a
  connector-less machine still gets a meaningful repo/tasks/KB digest.
