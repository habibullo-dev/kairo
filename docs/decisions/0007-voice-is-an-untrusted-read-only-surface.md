# ADR-0007: Voice is an untrusted, read-only surface; risky actions escalate to the screen

- **Status:** Accepted
- **Date:** 2026-07-07
- **Context phase:** Phase 7 (voice)

## Context

Voice is the first interface that removes the keyboard — and with it the permission
model's load-bearing assumption that a risky action pauses on a synchronous, authenticated
human at the keys. A microphone is also an open channel to anyone and anything making
sound near the device. So voice, done naively, would simultaneously (a) add an
unauthenticated approval channel and (b) add a prompt-injection channel — the two things
the whole safety stack exists to prevent. This ADR records the decisions that keep voice
*strictly more restricted* than a typed turn.

The safety contract is fixed by a prior, non-negotiable document —
[`docs/PLAN-7-voice-permissions-checkpoint.md`](../PLAN-7-voice-permissions-checkpoint.md)
(the consent model, threat boundaries T1–T8, and acceptance tests). This ADR does not
restate it; it records the *design decisions* that implement it. Where code and the
checkpoint conflict, the checkpoint wins.

## Decision

### 1. Voice is an interface, not a new authority — one approval path

`VoiceSession` is a peer of the REPL in the interface layer. It drives the same
`AgentLoop` through the same two public seams — the event stream out, the injected
`Approver` in — and reaches nothing else (not the gate, not the tools). The safety
consequence is structural: **there is exactly one approval path**, so the escalation
below cannot be bypassed by any amount of realtime plumbing. Every existing floor (gate,
sensitive-path, write allowlist, shell-metacharacter rule, unattended `HARD_DENY`,
sub-agent double gate, reflection firewall) applies unchanged beneath a voice turn. Voice
can only *narrow*, never widen — the same property `SubAgentGate` has (ADR-0006).

### 2. Transcribed audio is untrusted input, finalized-only

A finalized (endpointed) transcript is the only thing that drives a turn; partials are
display-only. It enters the model wrapped in untrusted-content delimiters
(`voice/framing.py`, the shape of `web.py::_FETCH_HEADER`) and the voice-mode system-prompt
block states that hearing an instruction is not authorization to act on it. Speech-to-text
is treated as a fetch from a hostile source — the same class as a fetched web page.
Speaker attribution, if ever added, is a probabilistic label on untrusted input, never an
authenticator.

### 3. Read-only by default; risky actions escalate to the screen (never voice-only)

The `VoiceApprover` **is** the injected `Approver`. Read-only tools are `ALLOW` by policy,
so voice does them freely (the read-only default falls out of the existing gate). Any
`ASK` — in v1, *every* `ASK`, the checkpoint's escalate-all default — is handed to a
`ScreenApprover` for a typed/tapped confirmation; the voice channel announces *that* a
confirmation is needed and *where*, and never resolves it in audio. **No voice-only
approval** for sends, writes, deletes, payments, shell, browser actions, or scheduling.
**Voice prepares, the screen commits** (the draft/command is composed by voice; the
committing step is a keystroke). Whether any benign `ASK` becomes voice-resolvable is
deferred — escalate-all is the safe default, loosened only with eval evidence.

### 4. "Screen available" is precise and fail-closed

A screen is *available* only when a paired display has the exact action preview rendered,
an authenticated input path (physical/session access) captures the confirm, and the
surface is unlocked/attended/responsive — all positively confirmed at the moment of
escalation (checkpoint §1.3). **Uncertainty resolves to unavailable, and unavailable
resolves to deny.** With no verifiable screen the `VoiceApprover` is a `HeadlessApprover`
(deny-all) — the same fail-closed default unattended runs use. Voice never *assumes* a
screen.

### 5. The calm renderer + the TTS privacy rule

Voice output is a *safe summary*, never the tool firehose. The renderer
(`voice/render.py`) enforces the **TTS privacy rule**: it does not speak secrets, tokens,
full commands, file contents, private message bodies, or the sensitive details of a risky
action — those stay on the (private) screen; the room can hear the speaker, so the spoken
channel is treated as a broadcast. This is enforced *in the renderer* (a summary/redaction
boundary the model cannot override), not left to model discretion, and is pinned by tests.
It voices the three copy contracts — **heard / intends / needs-approval** — pointing risky
confirmations to the screen without reading the preview aloud.

### 6. Cloud STT/TTS is an explicit opt-in

Third-party engines send data off-device — raw audio to a cloud transcriber, the
assistant's spoken text to a cloud synthesizer. They are gated behind
`voice.cloud_providers` (default **off**): a cloud provider selection is *refused at config
load* unless the opt-in is set (and its key present). Local providers are the no-egress
default; quality-first is still honored (cloud is the recommended quality path) but
reaching for it is a conscious per-install privacy decision, never a default that quietly
ships audio to a third party. Audio egress, when opted in, is logged as a visible network
event.

### 7. Wake-word activation is deferred; no unattended mic

The MVP ships **push-to-talk only.** The wake contract (one-turn scope, observable,
commits-nothing on a spurious ambient trigger — T8) is *designed and acceptance-tested*
now, but wake-word activation stays off and unwired unless explicitly approved later — an
always-adjacent listening surface is not turned on by default. There is **no unattended
recording**: a background job or scheduled task can never open the microphone (the
microphone analogue of `spawn_agent` in the unattended `HARD_DENY` set). Default retention
keeps the transcript (untrusted, with provenance) and discards raw audio.

### 8. Meeting capture is a separate consented mode → untrusted KB source

A captured meeting is an explicitly-consented mode (user start/stop, observable
indicator) whose transcript flows through the gated KB ingestion path as an `unreviewed`
source — never a trusted command stream, never an auto-action; any task proposed from a
meeting's "action items" requires explicit approval (the Meetily posture).

## Consequences

- Voice ships as a strictly-narrowing interface: read-only by default, risky actions
  screen-gated, no voice-only approval, cloud egress opt-in, no unattended mic. All prior
  safety contracts (ADR-0002–0006) stay intact.
- A little more friction than a hypothetical "just say yes" voice agent — deliberately.
  The friction is the feature ("I can't approve that by voice"), loosened only with
  evidence.
- Local providers are the default, so a fresh voice setup either runs a local engine or
  opts into cloud — a conscious choice, not a silent egress.
- Wake word and streaming STT/TTS are deferred with their contracts recorded, so enabling
  them later is a config/enhancement step against an already-verified floor.

## Alternatives considered

- **Voice-resolved approval for "low-risk" actions.** Rejected for v1 — ambient,
  unauthenticated audio can't carry approval weight, and "low-risk" is exactly where
  mishears and background speech bite. Escalate-all; revisit only with eval evidence.
- **Cloud STT/TTS as the default (quality-first, unconditional).** Rejected — it would
  ship audio off-device by default. Cloud stays the recommended *opt-in* quality path.
- **Speaking full detail aloud (parity with the screen).** Rejected — the voice channel is
  a broadcast; the TTS privacy rule keeps sensitive particulars on the private screen.
