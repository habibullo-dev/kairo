"""UnattendedGate + HeadlessApprover: the Phase 3 safety contract.

These tests are the reason unattended runs are safe rather than merely intended to
be: no background run may inherit an interactive shell/write/meta-tool grant by
accident. Written and committed before any BackgroundRunner code exists.
"""

from __future__ import annotations

import builtins
from pathlib import Path

import pytest

from jarvis.permissions import (
    HeadlessApprover,
    PermissionGate,
    Policy,
    ShellRule,
    UnattendedGate,
)
from jarvis.permissions.gate import Decision
from jarvis.permissions.policy import FilesystemPolicy, ShellPolicy
from jarvis.tools.base import Permission

ALLOW, ASK, DENY = Permission.ALLOW, Permission.ASK, Permission.DENY


def _inner(policy: Policy, root: Path) -> PermissionGate:
    return PermissionGate(policy, root)


def _unattended(policy: Policy, root: Path, *, allow_tools: frozenset[str] = frozenset()):
    return UnattendedGate(_inner(policy, root), allow_tools=allow_tools)


# --- hard-deny meta tools ----------------------------------------------------


def test_meta_tools_are_hard_denied_even_when_policy_allows(tmp_path: Path) -> None:
    # The escalation the review flagged: a persisted `tools: {schedule_task: allow}`
    # (one "always" keystroke) must NOT let a background job schedule more jobs.
    policy = Policy(
        tools={
            "schedule_task": ALLOW,
            "cancel_task": ALLOW,
            "remember": ALLOW,
            "forget": ALLOW,
        }
    )
    gate = _unattended(policy, tmp_path)
    for tool in ("schedule_task", "cancel_task", "remember", "forget"):
        decision = gate.check(tool, {}, tool_default=ALLOW)
        assert decision.permission is DENY, tool
        assert "meta tool" in decision.reason


def test_meta_tool_hard_deny_ignores_opt_in(tmp_path: Path) -> None:
    # unattended_allow_tools cannot re-enable a hard-denied meta tool.
    gate = _unattended(
        Policy(tools={"schedule_task": ALLOW}),
        tmp_path,
        allow_tools=frozenset({"schedule_task"}),
    )
    assert gate.check("schedule_task", {}, tool_default=ALLOW).permission is DENY


# --- demote side-effecting ALLOWs -------------------------------------------


def test_persisted_tool_allow_for_shell_is_demoted(tmp_path: Path) -> None:
    # `tools: {run_shell: allow}` persisted interactively must not run unattended.
    gate = _unattended(Policy(tools={"run_shell": ALLOW}), tmp_path)
    decision = gate.check("run_shell", {"command": "ls"}, tool_default=ASK)
    assert decision.permission is DENY
    assert "does not extend to unattended runs" in decision.reason
    assert gate.demoted == 1


def test_persisted_shell_prefix_rule_is_demoted(tmp_path: Path) -> None:
    # A granular "always allow this command" shell rule is also demoted.
    policy = Policy(shell=ShellPolicy(rules=[ShellRule(prefix="git", decision=ALLOW)]))
    gate = _unattended(policy, tmp_path)
    decision = gate.check("run_shell", {"command": "git status"}, tool_default=ASK)
    assert decision.permission is DENY
    assert gate.demoted == 1


def test_write_allowlist_allow_is_demoted(tmp_path: Path) -> None:
    # A write that the inner gate would auto-allow (tool allow + within allowlist)
    # is demoted unattended.
    policy = Policy(
        tools={"write_file": ALLOW},
        filesystem=FilesystemPolicy(write_allowlist=["."]),
    )
    gate = _unattended(policy, tmp_path)
    decision = gate.check("write_file", {"path": "out.txt"}, tool_default=ASK)
    assert decision.permission is DENY
    assert gate.demoted == 1


def test_opt_in_restores_exactly_the_named_tool(tmp_path: Path) -> None:
    # scheduler.unattended_allow_tools = [run_shell] preserves run_shell's ALLOW
    # but does NOT spill over to write_file.
    policy = Policy(tools={"run_shell": ALLOW, "write_file": ALLOW})
    gate = _unattended(policy, tmp_path, allow_tools=frozenset({"run_shell"}))
    assert gate.check("run_shell", {"command": "ls"}, tool_default=ASK).permission is ALLOW
    assert gate.check("write_file", {"path": "out.txt"}, tool_default=ASK).permission is DENY
    assert gate.demoted == 1  # only the write was demoted


def test_knowledge_ingest_and_write_demoted_but_query_passes(tmp_path: Path) -> None:
    # An interactive "always allow" for ingest/write_wiki must not extend to a 3am
    # research job; read-only query/lint pass through so scheduled research still works.
    policy = Policy(
        tools={"ingest_source": ALLOW, "write_wiki_page": ALLOW, "query_knowledge_base": ALLOW}
    )
    gate = _unattended(policy, tmp_path)
    assert gate.check("ingest_source", {"path": "a.txt"}, tool_default=ASK).permission is DENY
    assert gate.check("write_wiki_page", {"page": "p.md"}, tool_default=ASK).permission is DENY
    assert (
        gate.check("query_knowledge_base", {"query": "x"}, tool_default=ALLOW).permission is ALLOW
    )
    assert gate.demoted == 2  # ingest + write demoted; query untouched


def test_knowledge_ingest_opt_in_restores_it(tmp_path: Path) -> None:
    gate = _unattended(
        Policy(tools={"ingest_source": ALLOW}),
        tmp_path,
        allow_tools=frozenset({"ingest_source"}),
    )
    assert gate.check("ingest_source", {"path": "a.txt"}, tool_default=ASK).permission is ALLOW


# --- passthrough -------------------------------------------------------------


def test_read_only_allow_passes_through(tmp_path: Path) -> None:
    gate = _unattended(Policy(tools={"read_file": ALLOW}), tmp_path)
    decision = gate.check("read_file", {"path": "notes.txt"}, tool_default=ASK)
    assert decision.permission is ALLOW
    assert gate.demoted == 0


def test_inner_deny_passes_through(tmp_path: Path) -> None:
    # A sensitive-path deny from the inner gate is preserved (never softened).
    gate = _unattended(Policy(tools={"read_file": ALLOW}), tmp_path)
    decision = gate.check("read_file", {"path": ".env"}, tool_default=ASK)
    assert decision.permission is DENY


def test_ask_passes_through_for_the_approver_to_deny(tmp_path: Path) -> None:
    # Demotion only touches ALLOW; a side-effecting ASK is left for the approver.
    gate = _unattended(Policy(), tmp_path)  # default ask, no rules
    decision = gate.check("run_shell", {"command": "ls"}, tool_default=ASK)
    assert decision.permission is ASK
    assert gate.demoted == 0


def test_web_tool_follows_policy(tmp_path: Path) -> None:
    # Not in the demote set: a user who allowed web_fetch keeps research jobs working.
    allowed = _unattended(Policy(tools={"web_fetch": ALLOW}), tmp_path)
    assert allowed.check("web_fetch", {"url": "https://x"}, tool_default=ASK).permission is ALLOW
    # ask-by-default (the shipped web default) becomes a headless deny via the approver
    asked = _unattended(Policy(), tmp_path)
    assert asked.check("web_fetch", {"url": "https://x"}, tool_default=ASK).permission is ASK


# --- headless approver -------------------------------------------------------


async def test_headless_approver_denies_and_counts() -> None:
    approver = HeadlessApprover()
    decision = Decision(ASK, "needs a human")
    assert await approver(None, decision) is DENY
    assert await approver(None, decision) is DENY
    assert approver.denied == 2


async def test_headless_approver_never_reads_stdin(monkeypatch: pytest.MonkeyPatch) -> None:
    # There is no human to prompt: the approver must not touch stdin (which would
    # hang forever with no TTY). Any input() call fails this test.
    def _boom(*_a, **_kw):
        raise AssertionError("unattended approver must never read stdin")

    monkeypatch.setattr(builtins, "input", _boom)
    approver = HeadlessApprover()
    assert await approver(None, Decision(ASK, "x")) is DENY
