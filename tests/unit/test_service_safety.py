"""Adversarial safety pins for the local service adapters (Phase 10B Task 18).

The Team-Tool-Intelligence non-negotiables made executable, service-flavored. These are
DETERMINISTIC structural pins — stronger than a probabilistic eval: a council member has no
egress tool to exfiltrate WITH, so an injected "send the findings to evil.com" is inert by
construction, not merely by the model declining. (Live injection-resistance is additionally
covered by the adversarial eval suite that runs in the Task 19 gate.)
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import jarvis.core  # noqa: F401 - load core first (ledger<->core.context cycle in isolation)
from jarvis.config import load_config
from jarvis.core.client import FakeClient
from jarvis.orchestration import (
    READ_ONLY_SPAWNABLE,
    TEAM_PROFILES,
    OrchestrationEngine,
    resolve_team,
)
from jarvis.orchestration.context import ContextBundle, ContextItem, Provenance
from jarvis.orchestration.engine import SERVICE_TOOLS
from jarvis.orchestration.roles import Capability, RosterRole
from jarvis.permissions.gate import Decision
from jarvis.permissions.modes import AUTO_NEVER, PLAN_SAFE, Mode, auto_approves, plan_blocks
from jarvis.services.catalog import SERVICE_CATALOG
from jarvis.services.semgrep import SemgrepScanTool
from jarvis.tools.base import Permission, ToolContext

_SERVICE_TOOL_NAMES = ("semgrep_scan", "gitleaks_scan", "playwright_inspect")
_EGRESS_OR_WRITE = frozenset({"web_search", "web_fetch", "write_file", "run_shell"})
_CTX = ContextBundle(
    items=(ContextItem(kind="repo_file", ref="a.py", provenance=Provenance.REPO_CODE, text="x"),)
)


def _scope_engine() -> OrchestrationEngine:
    return OrchestrationEngine(
        spawn=lambda **kw: None,
        store=None,
        head_client=FakeClient([]),
        head_model="m",
        turn_lock=asyncio.Lock(),
    )


# --- mode matrix over service tools -----------------------------------------


def test_plan_mode_denies_every_service_tool() -> None:
    # Plan is an allowlist: no service tool is in PLAN_SAFE ⇒ all are blocked (fail-closed).
    for name in _SERVICE_TOOL_NAMES:
        assert name not in PLAN_SAFE
        assert plan_blocks(Mode.PLAN, name) is True


def test_auto_never_auto_approves_service_tools_by_default() -> None:
    # With the default (empty) auto_allow_tools, no service tool is silently approved.
    ask = Decision(permission=Permission.ASK, reason="svc", persistable=True)
    for name in _SERVICE_TOOL_NAMES:
        assert (
            auto_approves(
                mode=Mode.AUTO,
                started_auto=True,
                decision=ask,
                tool_name=name,
                auto_allow_tools=frozenset(),
            )
            is False
        )


# --- council/review have no egress: an injected finding is inert ------------


def test_no_council_or_review_scope_has_an_egress_or_write_tool() -> None:
    engine = _scope_engine()
    for team in TEAM_PROFILES.values():
        for member in team.members:
            if member.capability in (Capability.READ_ONLY, Capability.REVIEW_ONLY):
                for stage in ("council", "review"):
                    scope = set(engine._member_scope(member, stage, _CTX))
                    assert scope & _EGRESS_OR_WRITE == set(), (team.id, member.id, stage)
                    assert scope <= READ_ONLY_SPAWNABLE  # nothing beyond the read-only floor


def test_injected_finding_cannot_direct_egress() -> None:
    # A security council member holds only read-only tools + the two scanners — there is NO
    # egress tool in scope for an injected "exfiltrate these findings" instruction to use.
    engine = _scope_engine()
    sec_lead = next(m for m in resolve_team("security").members if m.id == "sec_lead")
    scope = set(engine._member_scope(sec_lead, "council", _CTX))
    assert {"semgrep_scan", "gitleaks_scan"} <= scope
    assert scope & _EGRESS_OR_WRITE == set()  # the exfil pipe simply does not exist


def test_read_only_member_never_acquires_execution_service() -> None:
    # Even a (hand-built) read-only member that DECLARES an execution-stage service (playwright)
    # is never granted it — the floor is not widened by a roster declaration.
    engine = _scope_engine()
    ro = RosterRole(
        "x",
        "X",
        "utility",
        frozenset({"read_file"}),
        frozenset({"playwright_local"}),
        Capability.READ_ONLY,
        "report",
    )
    for stage in ("council", "review"):
        assert "playwright_inspect" not in engine._member_scope(ro, stage, _CTX)


# --- scanners are non-egress + confined to the repo -------------------------


def test_now_services_are_non_egress() -> None:
    # No enabled "now" service is an egress sink, so the taint pipe has nothing to demote; the
    # egress flag is DERIVED from the spec (a future egress service would inherit egress=True and
    # be demoted by the existing, tested taint logic).
    for name in ("semgrep", "gitleaks", "playwright_local"):
        assert SERVICE_CATALOG[name].egress is False
    assert SemgrepScanTool.egress is False


async def test_scanner_confined_to_project_root(tmp_path: Path) -> None:
    cfg = load_config(root=tmp_path, env_file=None)
    cfg.services.enabled = ["semgrep"]
    tool = SemgrepScanTool(ToolContext(config=cfg))
    # An absolute path pointing outside the project (another "project"/system dir) is refused.
    other = tmp_path.parent / "other_project"
    out = await tool.run(tool.Params(path=str(other)))
    assert out.is_error and "escapes the project root" in out.content


# --- depth-1 / no-swarm stands ----------------------------------------------


def test_service_tools_are_delegatable_but_not_spawn() -> None:
    # Service tools may be scoped into a child (they're in SPAWNABLE), but none is spawn_agent —
    # a member can never spawn (depth-1; teams are groups, not swarms).
    from jarvis.agents import SPAWNABLE

    assert set(SERVICE_TOOLS.values()) <= SPAWNABLE
    assert "spawn_agent" not in SPAWNABLE
    assert "spawn_agent" not in set(SERVICE_TOOLS.values())


def test_auto_never_set_floor() -> None:
    # The never-auto floor: the two highest-blast-radius local actions PLUS (Phase 12) every
    # connector write. Service tools (semgrep/gitleaks/playwright_inspect) are NOT here — they
    # aren't auto-approvable for other reasons (covered above), and must not be added to this
    # floor. This guards the AUTO_NEVER constant itself against drift in either direction.
    assert frozenset(
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
    ) == AUTO_NEVER
    assert not (AUTO_NEVER & set(SERVICE_TOOLS.values()))  # no service tool on the floor
