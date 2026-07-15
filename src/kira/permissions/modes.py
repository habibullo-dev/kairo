"""Run modes (Phase 10 Task 5): Plan / Approval / Auto — backend-enforced, never a bypass.

The two enforcement points live in the agent loop, co-located with the Phase 9 egress-taint
transform so ordering is provably correct (pre-mortem #1):

* **Plan** denies anything not in :data:`PLAN_SAFE` — an *allowlist*, so a future unclassified
  tool fails closed (pre-mortem #2). Applied to the *raw gate decision*, before the approver.
* **Auto** auto-approves an ASK only when the tool is in the configured allowlist AND the
  decision is still ``persistable`` — i.e. it is evaluated on the **post-taint** decision, so a
  tainted-egress ASK (``persistable=False``, from a private read this turn) is NEVER
  auto-approved. run_shell / write_file can never be auto-approved even if configured
  (:data:`AUTO_NEVER`), and neither can a tool the SubAgentGate hard-denies.

Modes apply to interactive surfaces only. The BackgroundRunner keeps its ``UnattendedGate``
(no mode provider ⇒ Approval semantics), so Auto can never leak into an unattended run; voice
sessions are pinned to Approval. These are pure predicates + a tiny state holder so the whole
matrix is unit-tested without a loop; the loop calls them against a per-turn mode snapshot.
"""

from __future__ import annotations

from enum import StrEnum

from kira.permissions.gate import Decision
from kira.tools.base import Permission


class Mode(StrEnum):
    """A run mode. Debug is deliberately NOT here — it is a UI presentation flag, never a
    gate state (it must never change what is permitted)."""

    PLAN = "plan"  # read-only analysis: only PLAN_SAFE tools; everything else denied
    APPROVAL = "approval"  # the default — every ASK stops on the human (today's behavior)
    AUTO = "auto"  # auto-approve a configured low-risk allowlist; everything else still asks


#: The ONLY tools permitted in Plan mode: read-only, non-egress, no-world-change. An explicit
#: allowlist (not "deny side-effecting") so adding a tool forces a deliberate classification —
#: a new tool is denied in Plan until someone puts it here. Pinned by a test.
PLAN_SAFE: frozenset[str] = frozenset(
    {
        "read_file",
        "list_dir",
        "glob_search",
        "query_knowledge_base",
        "lint_knowledge_base",
        "recall",
        # Connector reads (Phase 9): read-only lookups, no world change. They taint the turn,
        # but Plan denies egress anyway, so the taint is moot here.
        "calendar_list_events",
        "gmail_search",
        "gmail_read",
        "drive_search",
        "drive_fetch",
    }
)

#: Tools Auto mode may NEVER auto-approve, even if a user lists them in ``auto_allow_tools``.
#: The two highest-blast-radius local actions, plus (Phase 12) every connector WRITE — an
#: outward write to a real account is never auto-approved, so the human always sees it. Config
#: cannot widen past this (pinned; the connector-write half mirrors WRITE_TOOL_NAMES + the Gmail
#: draft tools).
AUTO_NEVER: frozenset[str] = frozenset(
    {
        "run_shell",
        "write_file",
        "calendar_create_event",
        "calendar_update_event",
        "calendar_cancel_event",
        "drive_create_doc",
        "drive_update_doc",
        "gmail_create_draft",
        "gmail_update_draft",
    }
)


class ModeState:
    """The active mode for an interactive surface (mutable; set via the UI /api/mode route
    or a REPL command). ``current`` is the callable the loop reads."""

    def __init__(self, mode: Mode = Mode.APPROVAL) -> None:
        self._mode = mode

    def current(self) -> Mode:
        return self._mode

    def set(self, mode: Mode) -> None:
        self._mode = mode


def plan_blocks(mode: Mode, tool_name: str) -> bool:
    """True if Plan mode forbids ``tool_name`` (anything outside :data:`PLAN_SAFE`)."""
    return mode is Mode.PLAN and tool_name not in PLAN_SAFE


def auto_approves(
    *,
    mode: Mode,
    started_auto: bool,
    decision: Decision,
    tool_name: str,
    auto_allow_tools: frozenset[str],
) -> bool:
    """Whether Auto mode may resolve this ASK to ALLOW without the human.

    Requires: the turn STARTED in Auto AND the mode is STILL Auto (so a mid-turn flip *into*
    Auto doesn't apply to an in-flight turn, and a flip *out of* Auto tightens immediately —
    pre-mortem #12); the decision is an ASK; it is still ``persistable`` (a tainted-egress
    demotion sets persistable=False and thus always reaches the human); and the tool is in the
    configured allowlist minus the never-auto set."""
    return (
        started_auto
        and mode is Mode.AUTO
        and decision.permission is Permission.ASK
        and decision.persistable
        and tool_name in auto_allow_tools
        and tool_name not in AUTO_NEVER
    )
