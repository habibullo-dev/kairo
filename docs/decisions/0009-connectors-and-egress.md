# ADR-0009: Connectors are narrow audited adapters; the permission model reasons about data flow

- **Status:** Accepted
- **Date:** 2026-07-07
- **Context phase:** Phase 9 (make Kairo useful daily)

## Context

Phase 9 makes Kairo useful every day by letting it *read the user's world* — Google Calendar,
Gmail, Drive — and *notify* the user (Telegram, Kakao). This is the first time Kairo touches a
person's private accounts and the first time it can send content off-box on the model's
initiative. Two prior contracts don't automatically cover it, and the pre-mortem found the gap:

- ADR-0003/0004/0007/0008 all reason **per tool**: each tool has a permission default, the gate
  decides allow/ask/deny for one call, the sensitive-path floor guards specific tools. But the
  new risk is a **data flow across two individually-reasonable grants**: a silent mail read
  (a read, ALLOW, like `recall`) followed by a silent `web_fetch` (egress the user once
  "always-allowed") is an exfiltration pipe that neither grant looks dangerous alone.
- A durable OAuth **refresh token** on disk is a credential worth more than the machine — it
  outlives the process and grants standing account access. The sensitive-path floor guarded
  `read_file`, but not `run_shell`, `glob_search`, or `list_dir`.
- Connector-returned text (email bodies, calendar titles, file contents) is attacker-influenced
  and flows straight into the model — the same injection surface as web/KB, which ADR-0004
  frames as untrusted.

The user's decisions (2026-07-07): Gmail is **drafts-only forever**; connector reads are
**silent ALLOW** (framed + audited + taint-guarded); scopes are **exactly what the code
implements** (no over-scoping); write actions (calendar/drive create/update) are a separately
planned **Phase 9B**, not this phase.

## Decision

### 1. Native REST adapters, not a library or MCP

Each Google API is a thin httpx REST adapter we wrote (`connectors/google/*`), returning frozen
dataclasses with hard caps. Rejected: `google-api-python-client` (huge surface, hard to audit,
pulls transitive deps) and MCP (a new capability + permission checkpoint of its own; Hub keeps
its honest "not connected — future phase" stub). Adapters **never accept a model-supplied URL**
— endpoints are module constants — so there is no SSRF surface in the connector path (unlike
`web_fetch`, which routes through `net.safe_get`). Every adapter is `MockTransport`-testable, so
the whole surface is exercised keyless.

### 2. Least privilege, drafts-only, minimal scopes

Scopes are `calendar.readonly`, `gmail.readonly`, `gmail.compose`, `drive.readonly` — each maps
1:1 to a shipped tool. `gmail.send` is **never requested**, and no send method/tool/route exists
anywhere in `src/` (a grep pin, `test_no_gmail_send.py`, fails the build if one appears).
"Prepare a reply" creates a draft the user sends themselves from Gmail. Scopes are not widened
"for the future": Phase 9B will reconnect for its own write scopes when it is planned and gated.

### 3. The permission model learns about data flow (the centerpiece)

Two `Tool` ClassVars — `egress` (sends data off-box: web search/fetch, notify, draft) and
`reads_private` (returns personal data: calendar/gmail/drive reads) — drive three structural
rules, landed **before** any connector tool (Checkpoint A):

- **Per-turn taint.** Once a `reads_private` tool runs in a turn, any `egress` tool whose gate
  verdict is ALLOW is demoted to a **non-persistable ASK** for the rest of that turn. The human
  sees it, and the "always allow" affordance is suppressed (REPL prompt + UI Gate modal, and
  `ApprovalManager` refuses to persist it even from a crafted client). The silent-read →
  silent-egress pipe is structurally closed; worst case is one extra prompt.
- **Egress is never unattended.** `UnattendedGate` demotes ALLOW→DENY for **any** `egress`-marked
  tool not explicitly opted into `scheduler.unattended_allow_tools` (a property-driven rule, not
  a hand-maintained name list), and `gmail_create_draft`/`send_notification` are HARD_DENY (no
  opt-in reopens them). The digest's deterministic delivery (host code, not a tool) is the only
  unattended egress.
- **Cross-cutting token floor.** `data/connectors/*` token files join the hard sensitive-path
  floor (a `_SENSITIVE_PATTERNS` entry, not a `_SENSITIVE_DIRS` component — the latter would also
  block reading the source package). `run_shell` denies a command naming an existing sensitive
  path; `glob_search`/`list_dir` redact sensitive entries. The token is written atomically
  (`os.replace`) with best-effort 0600 and refreshed single-flight.

Scoping note: `recall`/`query_knowledge_base` are also private-ish reads but predate this phase;
tainting them would perturb the Phase 5 eval baselines, so it is a recorded follow-up, not done
now. A per-host outbound allowlist is also deferred — taint + ASK covers the model-driven exfil
class; the allowlist is a future hardening.

### 4. Silent reads, with three compensating controls

Connector reads default ALLOW (consistent with `recall`) so the digest and "summarize my inbox"
are smooth. The compensations: (a) every read result is wrapped in the verbatim untrusted-content
framing (`_HEADER` + fenced delimiters), so mail/calendar/drive text is reference material, not
instructions; (b) every read logs `tool_call` under the turn's trace id; (c) the taint rule (3).

### 5. Egress ledger + friendly-reconnect-only errors

Every egress action logs one structured `egress` event (`log_egress`) with a category and a
coarse destination type — never a token, bot token, chat_id, full recipient, URL query, or body
(pinned canary-absent). It is the "what left the box" ledger, surfaced in the Gate audit view.
Provider auth failures surface **only** as a friendly `"<Provider> needs reconnect: run kira
connect <provider>"` (`ConnectorAuthError.user_message`); the provider's raw error body is never
carried into a tool result, UI, or API response (it can echo tokens/addresses). The
secret-absence sweep is extended with a real on-disk token file exercised through the connector
status path.

### 6. Demo mode never masks a live account

`connectors.demo: true` builds badged fake adapters (`DemoGoogleClient`/`DemoNotifier`) so Daily/
digest/Hub can be exercised without OAuth — but only when the real provider keys are **absent**,
so it can never hide a live connection. The same fakes back the adversarial eval harness.

## Consequences

- The taint rule adds at most one approval prompt in the rare read-then-egress turn; it is the
  single highest-leverage control against the exfil class and degrades gracefully.
- Connectors add **zero new runtime dependencies** (httpx was already transitive; promoted to a
  core dep).
- Write actions are deliberately absent; Phase 9B inherits this ADR's taint/egress/audit rules
  and the drafts-only spirit (explicit, previewed, reversible) when it lands.
