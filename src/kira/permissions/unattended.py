"""The unattended permission regime — the safety core of Phase 3.

A background job runs with no human present, so the interactive safety story
("anything risky prompts the user") is gone. The naive replacement — deny every
ASK — is necessary but *not sufficient*, because the gate resolves many calls to
ALLOW *before* an approver is ever consulted: persisted ``tools: {x: allow}``
entries, shell prefix rules, write-allowlist dirs. Every "always allow" the user
granted while watching an interactive stream would otherwise apply, unwatched, at
3am — a poisoned web page in a research job could ride an allowlisted ``git``
prefix straight to execution.

So unattended runs wrap the normal gate in :class:`UnattendedGate`, which:

* **hard-denies the state-mutating meta tools** (``schedule_task``, ``cancel_task``,
  ``remember``, ``forget``) regardless of policy — closing self-replication (a job
  scheduling more jobs) and unattended memory writes even if an allow was persisted;
* **demotes ALLOW→DENY for side-effecting tools** (``run_shell``, ``write_file``)
  unless the tool is in an explicit opt-in set (``scheduler.unattended_allow_tools``)
  — an interactive grant is not an unattended grant;
* **delegates everything else** to the wrapped gate (read-only tools stay allowed;
  web tools follow policy — ask-by-default becomes a headless deny).

There is deliberately **no per-task permission grant**: the only way to widen what
background jobs may do is ``permissions.yaml`` via the opt-in set, a place the user
edits consciously. This is ADR-0003.

ASK decisions normally reach the :class:`HeadlessApprover`, which denies every one
(the model reads the resulting ``is_error`` result and adapts).  A separately wired
:class:`ParkingApprover` is the narrow opt-in for durable approval parking: it stops
the loop before any batch tool executes and lets a host persist the exact requested
call.  It does not grant permission itself.  Both the gate's demotions and the
ordinary headless approver's denials are counted, so the runner can report how
constrained a run was.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from kira.permissions.gate import Decision, PermissionGate
from kira.tools.base import Permission

if TYPE_CHECKING:
    from kira.core.client import ToolCall

#: State-mutating meta tools: never available unattended, no matter what policy says.
#: A background run must not be able to schedule work, cancel your reminders, or
#: write to long-term memory on its own authority. ``spawn_agent`` is here too
#: (Phase 6): an unattended job must not fan out into sub-agents — no unsupervised
#: swarm (ADR-0006; delegation is interactive-only in this phase). ``gmail_create_draft``
#: and ``send_notification`` join it (Phase 9): egress-with-agency is never unattended, and
#: HARD_DENY means no ``unattended_allow_tools`` opt-in can reopen them — the digest's
#: deterministic delivery path (host code, not a tool) is the only unattended egress.
HARD_DENY: frozenset[str] = frozenset(
    {
        "schedule_task",
        "cancel_task",
        "remember",
        "forget",
        "spawn_agent",
        "gmail_create_draft",
        "send_notification",
        # Phase 12 connector writes: an unattended run may neither PROPOSE nor execute an
        # outward write (calendar/Drive/Docs) or a draft edit. No opt-in reopens these — the
        # write queue is interactive-human-only in Phase 12 (unattended-propose is Phase 16).
        "calendar_create_event",
        "calendar_update_event",
        "calendar_cancel_event",
        "drive_create_doc",
        "drive_update_doc",
        "gmail_update_draft",
    }
)

#: Side-effecting tools whose *ALLOW* is demoted to DENY unless explicitly opted in.
#: (An ASK for these is left alone — the headless approver denies it either way.)
#: ingest_source/write_wiki_page persist retrievable content, so an interactive
#: "always allow" must not silently let a 3am research job feed the knowledge base;
#: opting a background pipeline in requires scheduler.unattended_allow_tools + the
#: content lands quarantined 'unreviewed' anyway (ADR-0004). Phase 9 adds a *property*-driven
#: rule on top of this name set (see ``egress_tools``): any tool marked ``egress`` is demoted
#: the same way, so a persisted `tools: {web_fetch: allow}` can't send at 3am either.
DEMOTE_ALLOW: frozenset[str] = frozenset(
    {"run_shell", "write_file", "ingest_source", "write_wiki_page"}
)


class ApprovalParked(RuntimeError):
    """A deliberate, fail-closed stop at one exact ``ASK`` tool call.

    This neutral permission-layer control object carries the immutable model call and, once
    :class:`~kira.core.agent.AgentLoop` propagates it, the transcript prefix containing that
    exact ``tool_use`` block.  It is not an approval: a host must durably park it, obtain a
    separate one-time resolution, and explicitly resume only the verified call.

    It lives here rather than in ``core.agent`` to avoid a permissions-package import cycle:
    permission policy may be imported before the agent loop itself.
    """

    def __init__(self, call: ToolCall, decision: Decision) -> None:
        super().__init__(f"approval parked for {call.name}")
        self.call = call
        self.decision = decision
        self.messages: list[dict] | None = None

    def bind_messages(self, messages: list[dict]) -> None:
        """Attach the completed prefix exactly once before propagating to a host."""
        if self.messages is None:
            # ``run_turn`` owns this list and immediately exits after binding it.  The shallow
            # copy prevents a host from replacing history entries while preserving provider
            # blocks (including the original tool-use id/input) byte-for-byte.
            self.messages = list(messages)


class UnattendedGate:
    """Wraps a :class:`PermissionGate`, tightening its decisions for headless runs.

    Same ``check`` signature as the wrapped gate, so an :class:`~kira.core.agent.AgentLoop`
    uses it interchangeably. ``demoted`` counts ALLOW→DENY demotions this run (the
    runner sums it with the approver's denials for the run's ``denied_count``).

    ``egress_tools`` is the set of tool names whose :attr:`Tool.egress` is True, computed from
    the registry at construction (jobs.py). Any egress tool's ALLOW is demoted to DENY unless
    opted into ``allow_tools`` — this is the property-driven half of rule 2, so a new egress
    connector is covered automatically without editing :data:`DEMOTE_ALLOW`.
    """

    def __init__(
        self,
        inner: PermissionGate,
        *,
        allow_tools: frozenset[str] = frozenset(),
        egress_tools: frozenset[str] = frozenset(),
        demote_to_ask: bool = False,
    ) -> None:
        self.inner = inner
        self.allow_tools = allow_tools
        self.egress_tools = egress_tools
        # Remote Operator runs have a real but asynchronous owner channel.  A standing ALLOW
        # therefore becomes an exact-call ASK instead of inheriting unattended authority.  The
        # ordinary scheduler keeps the historical DENY behavior by default.
        self.demote_to_ask = demote_to_ask
        self.demoted = 0

    def check(
        self,
        tool_name: str,
        tool_input: dict | None = None,
        *,
        tool_default: Permission | None = None,
    ) -> Decision:
        # 1. Meta tools are denied before any policy is consulted — a persisted
        #    `tools: {schedule_task: allow}` must not reopen this.
        if tool_name in HARD_DENY:
            return Decision(
                Permission.DENY,
                f"'{tool_name}' is unavailable to unattended runs (meta tool)",
            )

        decision = self.inner.check(tool_name, tool_input, tool_default=tool_default)

        # 2. A side-effecting or egress ALLOW granted interactively does not extend here.
        demotable = tool_name in DEMOTE_ALLOW or tool_name in self.egress_tools
        if (
            decision.permission is Permission.ALLOW
            and demotable
            and tool_name not in self.allow_tools
        ):
            if self.demote_to_ask:
                return Decision(
                    Permission.ASK,
                    f"'{tool_name}' standing allow requires one exact remote approval; "
                    f"was: {decision.reason}",
                    persistable=False,
                )
            self.demoted += 1
            return Decision(
                Permission.DENY,
                f"'{tool_name}' allow does not extend to unattended runs "
                f"(add to scheduler.unattended_allow_tools to opt in); was: {decision.reason}",
            )

        # 3. Everything else (read-only allows, inner denies, any ASK) passes through.
        return decision


class HeadlessApprover:
    """The approver for unattended runs: denies every ASK, and counts them.

    Provably never touches stdin — there is no human to prompt. The denial becomes
    an ``is_error`` tool result the model reads ("I couldn't do that unattended")
    and works around, rather than a crash or a hang waiting on input that never comes.
    """

    def __init__(self) -> None:
        self.denied = 0

    async def __call__(self, call: ToolCall, decision: Decision) -> Permission:
        self.denied += 1
        return Permission.DENY


class ParkingApprover:
    """Stop an unattended turn at an ``ASK`` without converting it into ALLOW.

    The scheduler host must catch :class:`~kira.core.agent.ApprovalParked`, store
    its exact call/transcript using the durable task-run continuation seam, and later
    perform a one-time explicit resolution.  This class intentionally has no callback,
    persistence, transport, or policy-bypass behavior: a forgotten host integration
    fails closed by ending the run rather than executing anything.
    """

    async def __call__(self, call: ToolCall, decision: Decision) -> Permission:
        raise ApprovalParked(call, decision)
