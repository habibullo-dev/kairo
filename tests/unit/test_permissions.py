"""PermissionGate + policy tests: every allow/ask/deny path, and persistence."""

from __future__ import annotations

from pathlib import Path

import pytest

from jarvis.permissions import (
    PermissionGate,
    Policy,
    ShellRule,
    load_policy,
    save_policy,
)
from jarvis.permissions.policy import FilesystemPolicy, ShellPolicy
from jarvis.tools.base import Permission

ALLOW, ASK, DENY = Permission.ALLOW, Permission.ASK, Permission.DENY


def gate(policy: Policy, root: Path, **kw) -> PermissionGate:
    return PermissionGate(policy, root, **kw)


# --- policy load/save ------------------------------------------------------


def test_load_missing_file_gives_safe_defaults(tmp_path: Path) -> None:
    p = load_policy(tmp_path / "nope.yaml")
    assert p.default is ASK
    assert p.tools == {}
    assert p.filesystem.write_allowlist == ["."]


def test_load_and_roundtrip(tmp_path: Path) -> None:
    src = tmp_path / "permissions.yaml"
    src.write_text(
        "default: ask\n"
        "tools:\n  read_file: allow\n  run_shell: deny\n"
        "shell:\n  rules:\n    - prefix: 'git status'\n      decision: allow\n",
        encoding="utf-8",
    )
    policy = load_policy(src)
    assert policy.tools["read_file"] is ALLOW
    assert policy.tools["run_shell"] is DENY
    assert policy.shell.rules[0].prefix == "git status"

    # Save then reload -> identical decisions.
    out = tmp_path / "out.yaml"
    save_policy(policy, out)
    assert load_policy(out).tools["run_shell"] is DENY


def test_non_mapping_policy_raises(tmp_path: Path) -> None:
    src = tmp_path / "bad.yaml"
    src.write_text("- a\n- b\n", encoding="utf-8")
    with pytest.raises(ValueError, match="mapping"):
        load_policy(src)


# --- base decision precedence ----------------------------------------------


def test_per_tool_entry_wins(tmp_path: Path) -> None:
    g = gate(Policy(tools={"read_file": ALLOW}), tmp_path)
    assert g.check("read_file").permission is ALLOW


def test_tool_default_used_when_no_policy_entry(tmp_path: Path) -> None:
    g = gate(Policy(default=DENY), tmp_path)
    # tool_default (from the tool class) beats the policy default
    assert g.check("some_tool", tool_default=ALLOW).permission is ALLOW


def test_policy_default_is_last_resort(tmp_path: Path) -> None:
    g = gate(Policy(default=ASK), tmp_path)
    assert g.check("unknown_tool").permission is ASK


def test_decision_has_reason(tmp_path: Path) -> None:
    g = gate(Policy(tools={"read_file": ALLOW}), tmp_path)
    assert g.check("read_file").reason


# --- shell rules -----------------------------------------------------------


def _shell_policy() -> Policy:
    return Policy(
        tools={"run_shell": ASK},
        shell=ShellPolicy(
            rules=[
                ShellRule(prefix="git", decision=ASK),
                ShellRule(prefix="git status", decision=ALLOW),
                ShellRule(prefix="rm ", decision=DENY),
            ]
        ),
    )


def test_shell_longest_prefix_wins(tmp_path: Path) -> None:
    g = gate(_shell_policy(), tmp_path)
    # "git status ." matches both "git" (ask) and "git status" (allow) -> longest wins
    assert g.check("run_shell", {"command": "git status ."}).permission is ALLOW


def test_shell_rule_overrides_ask_base(tmp_path: Path) -> None:
    g = gate(_shell_policy(), tmp_path)
    assert g.check("run_shell", {"command": "rm -rf build"}).permission is DENY


def test_shell_no_match_falls_back_to_base(tmp_path: Path) -> None:
    g = gate(_shell_policy(), tmp_path)
    assert g.check("run_shell", {"command": "echo hi"}).permission is ASK


def test_tool_level_deny_is_absolute_over_shell_rules(tmp_path: Path) -> None:
    policy = _shell_policy()
    policy.tools["run_shell"] = DENY
    g = gate(policy, tmp_path)
    # even though "git status" has an allow rule, the tool is denied outright
    assert g.check("run_shell", {"command": "git status"}).permission is DENY


# --- filesystem write allowlist --------------------------------------------


def test_write_inside_allowlist_keeps_allow(tmp_path: Path) -> None:
    policy = Policy(tools={"write_file": ALLOW}, filesystem=FilesystemPolicy(write_allowlist=["."]))
    g = gate(policy, tmp_path)
    inside = tmp_path / "sub" / "out.txt"
    assert g.check("write_file", {"path": str(inside)}).permission is ALLOW


def test_write_outside_allowlist_escalates_allow_to_ask(tmp_path: Path) -> None:
    policy = Policy(tools={"write_file": ALLOW}, filesystem=FilesystemPolicy(write_allowlist=["."]))
    g = gate(policy, tmp_path)
    outside = tmp_path.parent / "elsewhere.txt"
    assert g.check("write_file", {"path": str(outside)}).permission is ASK


def test_write_relative_path_resolved_under_root(tmp_path: Path) -> None:
    policy = Policy(tools={"write_file": ALLOW}, filesystem=FilesystemPolicy(write_allowlist=["."]))
    g = gate(policy, tmp_path)
    assert g.check("write_file", {"path": "notes/todo.txt"}).permission is ALLOW


def test_write_specific_subdir_allowlist(tmp_path: Path) -> None:
    policy = Policy(
        tools={"write_file": ALLOW}, filesystem=FilesystemPolicy(write_allowlist=["allowed"])
    )
    g = gate(policy, tmp_path)
    assert g.check("write_file", {"path": "allowed/x.txt"}).permission is ALLOW
    assert g.check("write_file", {"path": "other/x.txt"}).permission is ASK


def test_write_ask_base_stays_ask_even_inside_allowlist(tmp_path: Path) -> None:
    policy = Policy(tools={"write_file": ASK})
    g = gate(policy, tmp_path)
    # allowlist can only tighten, never loosen: an ask base stays ask
    assert g.check("write_file", {"path": "x.txt"}).permission is ASK


# --- persistence -----------------------------------------------------------


def test_persist_allow_writes_and_reloads(tmp_path: Path) -> None:
    src = tmp_path / "permissions.yaml"
    save_policy(Policy(), src)
    g = gate(load_policy(src), tmp_path, source_path=src)
    g.persist_allow("write_file")
    assert load_policy(src).tools["write_file"] is ALLOW


def test_persist_shell_rule_writes_and_reloads(tmp_path: Path) -> None:
    src = tmp_path / "permissions.yaml"
    save_policy(Policy(), src)
    g = gate(load_policy(src), tmp_path, source_path=src)
    g.persist_shell_rule("npm test", ALLOW)
    reloaded = load_policy(src)
    assert any(r.prefix == "npm test" and r.decision is ALLOW for r in reloaded.shell.rules)


def test_persist_shell_rule_replaces_duplicate_prefix(tmp_path: Path) -> None:
    src = tmp_path / "permissions.yaml"
    save_policy(Policy(shell=ShellPolicy(rules=[ShellRule(prefix="npm test", decision=DENY)])), src)
    g = gate(load_policy(src), tmp_path, source_path=src)
    g.persist_shell_rule("npm test", ALLOW)
    rules = load_policy(src).shell.rules
    assert len([r for r in rules if r.prefix == "npm test"]) == 1
    assert rules[-1].decision is ALLOW


def test_persist_without_source_is_noop(tmp_path: Path) -> None:
    g = gate(Policy(), tmp_path)  # no source_path
    g.persist_allow("write_file")  # should not raise
    assert g.policy.tools["write_file"] is ALLOW  # in-memory only


def test_persist_write_dir(tmp_path: Path) -> None:
    src = tmp_path / "permissions.yaml"
    save_policy(Policy(), src)
    g = gate(load_policy(src), tmp_path, source_path=src)
    g.persist_write_dir("exports")
    assert "exports" in load_policy(src).filesystem.write_allowlist
