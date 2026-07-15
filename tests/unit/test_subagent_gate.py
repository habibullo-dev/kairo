"""SubAgentGate: Phase 6's second gate — the child tool-call contract.

The property under test: SubAgentGate can only ever *narrow* the parent gate's
decisions. Hard denies (recursion + meta tools) and scope come first; every inner
floor survives composition (over PermissionGate *and* UnattendedGate); run-scoped
grants only upgrade an ASK the human blessed, are pattern-scoped, and never touch
run_shell/write_file. Written before any SubAgentService exists. Keyless.
"""

from __future__ import annotations

from pathlib import Path

from kira.permissions import PermissionGate, Policy, ShellRule, UnattendedGate
from kira.permissions.policy import FilesystemPolicy, ShellPolicy
from kira.permissions.subagent import (
    NEVER_GRANTABLE,
    SUBAGENT_HARD_DENY,
    SubAgentGate,
)
from kira.tools.base import Permission

ALLOW, ASK, DENY = Permission.ALLOW, Permission.ASK, Permission.DENY

# A scope wide enough for most tests; individual tests pass a narrower one when needed.
_WIDE = frozenset(
    {"read_file", "list_dir", "glob_search", "run_shell", "write_file", "web_search", "web_fetch"}
)


def _gate(policy: Policy, root: Path, *, scope: frozenset[str] = _WIDE) -> SubAgentGate:
    return SubAgentGate(PermissionGate(policy, root), scope=scope, project_root=root)


# --- hard denies (before scope, before policy) -------------------------------


def test_hard_deny_covers_spawn_and_meta_tools(tmp_path: Path) -> None:
    # Even with the tool in scope AND policy allowing it, these are denied.
    scope = _WIDE | SUBAGENT_HARD_DENY
    policy = Policy(tools=dict.fromkeys(SUBAGENT_HARD_DENY, ALLOW))
    gate = _gate(policy, tmp_path, scope=scope)
    for tool in SUBAGENT_HARD_DENY:
        decision = gate.check(tool, {}, tool_default=ALLOW)
        assert decision.permission is DENY, tool
    assert "spawn_agent" in SUBAGENT_HARD_DENY  # depth-1: no recursion
    assert gate.denied == len(SUBAGENT_HARD_DENY)


# --- scope --------------------------------------------------------------------


def test_out_of_scope_tool_is_denied(tmp_path: Path) -> None:
    gate = _gate(Policy(tools={"read_file": ALLOW}), tmp_path, scope=frozenset({"read_file"}))
    # read_file is in scope -> passes to inner (allowed)
    assert gate.check("read_file", {"path": "a.txt"}, tool_default=ALLOW).permission is ALLOW
    # web_fetch is NOT in scope -> denied regardless of policy
    d = gate.check("web_fetch", {"url": "https://x"}, tool_default=ALLOW)
    assert d.permission is DENY
    assert "outside this sub-agent's tool scope" in d.reason
    assert gate.denied == 1


# --- composition over PermissionGate: every floor survives -------------------


def test_sensitive_path_floor_survives(tmp_path: Path) -> None:
    gate = _gate(Policy(tools={"read_file": ALLOW}), tmp_path)
    assert gate.check("read_file", {"path": ".env"}, tool_default=ALLOW).permission is DENY


def test_write_allowlist_escalation_survives(tmp_path: Path) -> None:
    # An ALLOW write outside the allowlist is escalated to ASK by the inner gate.
    # Empty allowlist => every write is "outside" => escalated.
    policy = Policy(tools={"write_file": ALLOW}, filesystem=FilesystemPolicy(write_allowlist=[]))
    gate = _gate(policy, tmp_path)
    assert gate.check("write_file", {"path": "out.txt"}, tool_default=ALLOW).permission is ASK


def test_shell_metacharacter_escalation_survives(tmp_path: Path) -> None:
    policy = Policy(shell=ShellPolicy(rules=[ShellRule(prefix="git", decision=ALLOW)]))
    gate = _gate(policy, tmp_path)
    # a chained command can't ride the allowlisted 'git' prefix — escalated to ASK
    d = gate.check("run_shell", {"command": "git status; rm -rf /"}, tool_default=ASK)
    assert d.permission is ASK


def test_inner_allow_passes_through(tmp_path: Path) -> None:
    gate = _gate(Policy(tools={"read_file": ALLOW}), tmp_path)
    d = gate.check("read_file", {"path": "notes.txt"}, tool_default=ALLOW)
    assert d.permission is ALLOW


# --- composition over UnattendedGate -----------------------------------------


def test_composition_over_unattended_gate(tmp_path: Path) -> None:
    # Unattended spawning is denied in v1, but the composition must still be correct:
    # the UnattendedGate's demotion fires underneath the SubAgentGate.
    inner = UnattendedGate(PermissionGate(Policy(tools={"run_shell": ALLOW}), tmp_path))
    gate = SubAgentGate(inner, scope=_WIDE, project_root=tmp_path)
    d = gate.check("run_shell", {"command": "ls"}, tool_default=ASK)
    assert d.permission is DENY  # interactive ALLOW does not extend to unattended
    assert inner.demoted == 1


# --- run-scoped pattern grants -----------------------------------------------


def test_web_fetch_grant_is_host_scoped(tmp_path: Path) -> None:
    gate = _gate(Policy(), tmp_path)  # web_fetch defaults to ASK
    # before granting: ASK
    before = gate.check("web_fetch", {"url": "https://docs.py/x"}, tool_default=ASK)
    assert before.permission is ASK
    grant = gate.grant("web_fetch", {"url": "https://docs.py/x"})
    assert grant is not None and grant.kind == "host" and grant.value == "docs.py"
    # same host, different path -> granted
    d = gate.check("web_fetch", {"url": "https://docs.py/other"}, tool_default=ASK)
    assert d.permission is ALLOW
    assert "granted for this sub-agent run" in d.reason
    # different host (the poisoned-redirect case) -> re-asks
    other = gate.check("web_fetch", {"url": "https://attacker.example/x"}, tool_default=ASK)
    assert other.permission is ASK


def test_read_file_grant_is_directory_scoped(tmp_path: Path) -> None:
    gate = _gate(Policy(), tmp_path, scope=frozenset({"read_file"}))
    # read_file defaults to ALLOW normally; force ASK so the grant path is exercised.
    assert gate.check("read_file", {"path": "docs/a.txt"}, tool_default=ASK).permission is ASK
    gate.grant("read_file", {"path": "docs/a.txt"})
    assert gate.check("read_file", {"path": "docs/b.txt"}, tool_default=ASK).permission is ALLOW
    assert gate.check("read_file", {"path": "docs/sub/c.txt"}, tool_default=ASK).permission is ALLOW
    # a sibling directory re-asks
    assert gate.check("read_file", {"path": "other/d.txt"}, tool_default=ASK).permission is ASK


def test_list_dir_grant_scopes_to_the_directory(tmp_path: Path) -> None:
    gate = _gate(Policy(), tmp_path, scope=frozenset({"list_dir"}))
    gate.grant("list_dir", {"path": "docs"})
    assert gate.check("list_dir", {"path": "docs"}, tool_default=ASK).permission is ALLOW
    assert gate.check("list_dir", {"path": "docs/sub"}, tool_default=ASK).permission is ALLOW
    assert gate.check("list_dir", {"path": "elsewhere"}, tool_default=ASK).permission is ASK


def test_search_grant_is_tool_level(tmp_path: Path) -> None:
    gate = _gate(Policy(), tmp_path)
    g = gate.grant("web_search", {"query": "anything"})
    assert g is not None and g.kind == "tool"
    # any subsequent query is covered (the query varies; the backend is the fixed surface)
    d = gate.check("web_search", {"query": "totally different"}, tool_default=ASK)
    assert d.permission is ALLOW


def test_run_shell_and_write_file_are_never_grantable(tmp_path: Path) -> None:
    gate = _gate(Policy(), tmp_path)
    assert gate.grant("run_shell", {"command": "ls"}) is None
    assert gate.grant("write_file", {"path": "a.txt"}) is None
    assert "run_shell" in NEVER_GRANTABLE
    assert "write_file" in NEVER_GRANTABLE
    # and check() never upgrades them: run_shell stays ASK even after a grant attempt
    assert gate.check("run_shell", {"command": "ls"}, tool_default=ASK).permission is ASK


def test_grant_only_upgrades_ask_never_deny_or_allow(tmp_path: Path) -> None:
    # A grant must not turn an inner DENY into ALLOW. read_file on a sensitive path is a
    # floor DENY; even a directory grant covering it can't rescue it.
    gate = _gate(Policy(tools={"read_file": ALLOW}), tmp_path, scope=frozenset({"read_file"}))
    gate.grant("read_file", {"path": "a.txt"})  # grants the project-root dir
    assert gate.check("read_file", {"path": ".env"}, tool_default=ALLOW).permission is DENY


def test_grants_are_per_instance(tmp_path: Path) -> None:
    gate1 = _gate(Policy(), tmp_path)
    gate1.grant("web_fetch", {"url": "https://docs.py/x"})
    # a fresh gate (a different run) shares no grants
    gate2 = _gate(Policy(), tmp_path)
    assert (
        gate2.check("web_fetch", {"url": "https://docs.py/x"}, tool_default=ASK).permission is ASK
    )
