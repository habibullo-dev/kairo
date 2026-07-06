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

ASK decisions still reach the :class:`HeadlessApprover`, which denies every one
(the model reads the resulting ``is_error`` result and adapts). Both the gate's
demotions and the approver's denials are counted, so the runner can report how
constrained a run was.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from jarvis.permissions.gate import Decision, PermissionGate
from jarvis.tools.base import Permission

if TYPE_CHECKING:
    from jarvis.core.client import ToolCall

#: State-mutating meta tools: never available unattended, no matter what policy says.
#: A background run must not be able to schedule work, cancel your reminders, or
#: write to long-term memory on its own authority. ``spawn_agent`` is here too
#: (Phase 6): an unattended job must not fan out into sub-agents — no unsupervised
#: swarm (ADR-0006; delegation is interactive-only in this phase).
HARD_DENY: frozenset[str] = frozenset(
    {"schedule_task", "cancel_task", "remember", "forget", "spawn_agent"}
)

#: Side-effecting tools whose *ALLOW* is demoted to DENY unless explicitly opted in.
#: (An ASK for these is left alone — the headless approver denies it either way.)
#: ingest_source/write_wiki_page persist retrievable content, so an interactive
#: "always allow" must not silently let a 3am research job feed the knowledge base;
#: opting a background pipeline in requires scheduler.unattended_allow_tools + the
#: content lands quarantined 'unreviewed' anyway (ADR-0004).
DEMOTE_ALLOW: frozenset[str] = frozenset(
    {"run_shell", "write_file", "ingest_source", "write_wiki_page"}
)


class UnattendedGate:
    """Wraps a :class:`PermissionGate`, tightening its decisions for headless runs.

    Same ``check`` signature as the wrapped gate, so an :class:`~jarvis.core.agent.AgentLoop`
    uses it interchangeably. ``demoted`` counts ALLOW→DENY demotions this run (the
    runner sums it with the approver's denials for the run's ``denied_count``).
    """

    def __init__(self, inner: PermissionGate, *, allow_tools: frozenset[str] = frozenset()) -> None:
        self.inner = inner
        self.allow_tools = allow_tools
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

        # 2. A side-effecting ALLOW granted interactively does not extend here.
        if (
            decision.permission is Permission.ALLOW
            and tool_name in DEMOTE_ALLOW
            and tool_name not in self.allow_tools
        ):
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
