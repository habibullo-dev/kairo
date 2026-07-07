"""Roster roles — a team member's identity, tools, services, and capability.

The load-bearing floor: council/review members are READ-ONLY — their tools must be a subset
of :data:`READ_ONLY_SPAWNABLE` (no shell, no write, and no egress web tools; a prompt-injected
council context must not be able to exfiltrate itself — ADR-0014 §2). Only a single
``write_capable`` member per team, and it runs only in the Execution stage (enforced by the
engine, Task 13). Every member's tools are a subset of the Phase-6 ``SPAWNABLE`` set, so a
member can never hold ``spawn_agent`` (depth-1 stands).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from jarvis.agents import SPAWNABLE

#: The read-only floor for council/review roles: local reads only. NO run_shell (never
#: read-only), NO write_file, and NO web_search/web_fetch (egress). Task 16 grows it by
#: EXACTLY {semgrep_scan, gitleaks_scan} — hardened read-only scanners (no shell, no write, no
#: egress). playwright_inspect is deliberately NOT here: it is execution-stage only, so the
#: council/review floor stays exactly local-reads + the two scanners. Pinned by a test.
READ_ONLY_SPAWNABLE: frozenset[str] = frozenset(
    {
        "read_file",
        "list_dir",
        "glob_search",
        "query_knowledge_base",
        "semgrep_scan",
        "gitleaks_scan",
    }
)


class Capability(StrEnum):
    READ_ONLY = "read_only"  # council: gather + reason, no world change, no egress
    REVIEW_ONLY = "review_only"  # review: read the produced diff/report, no world change
    WRITE_CAPABLE = "write_capable"  # execution: the single writer, execution stage only


@dataclass(frozen=True)
class RosterRole:
    """One team member. ``route_role`` maps to a ModelRegistry role (planner/coder/…) for its
    model; ``tools`` ⊆ SPAWNABLE (⊆ READ_ONLY_SPAWNABLE when read/review-only); ``services`` ⊆
    the service catalog. ``max_cost_usd`` is the per-member budget cap (None = team/run cap)."""

    id: str
    title: str
    route_role: str
    tools: frozenset[str]
    services: frozenset[str]
    capability: Capability
    output: str  # report | diff_proposal | verdict | notes
    max_cost_usd: float | None = None


class RosterError(ValueError):
    """A roster role that violates a floor (illegal tool, read-only member with a writer tool)."""


def validate_role(role: RosterRole) -> None:
    """Raise :class:`RosterError` if the role breaks a tool floor. Service membership is
    validated against the enabled catalog by the engine/team resolver (it depends on runtime
    flags); the tool floors here are static and always hold."""
    illegal = role.tools - SPAWNABLE
    if illegal:
        raise RosterError(f"role {role.id!r}: tools not spawnable: {sorted(illegal)}")
    if role.capability in (Capability.READ_ONLY, Capability.REVIEW_ONLY):
        over = role.tools - READ_ONLY_SPAWNABLE
        if over:
            raise RosterError(
                f"role {role.id!r} is {role.capability.value} but holds non-read-only tools: "
                f"{sorted(over)} (council/review must be a subset of READ_ONLY_SPAWNABLE)"
            )
