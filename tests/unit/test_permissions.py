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


# --- hardening: shell chaining / redirection cannot bypass an allow rule ----


def _default_shell_policy() -> Policy:
    """Mirrors the shipped config/permissions.yaml shell rules."""
    return Policy(
        tools={"run_shell": ASK},
        shell=ShellPolicy(
            rules=[
                ShellRule(prefix="git status", decision=ALLOW),
                ShellRule(prefix="git diff", decision=ALLOW),
                ShellRule(prefix="git log", decision=ALLOW),
                ShellRule(prefix="rm ", decision=ASK),
            ]
        ),
    )


def test_shell_clean_command_still_allowed(tmp_path: Path) -> None:
    g = gate(_default_shell_policy(), tmp_path)
    assert g.check("run_shell", {"command": "git status --short"}).permission is ALLOW


def test_shell_chaining_downgrades_allow_to_ask(tmp_path: Path) -> None:
    g = gate(_default_shell_policy(), tmp_path)
    # matches the 'git status' allow prefix, but the ';' would chain a second command
    assert g.check("run_shell", {"command": "git status; rm -rf x"}).permission is ASK


def test_shell_pipe_downgrades_allow_to_ask(tmp_path: Path) -> None:
    g = gate(_default_shell_policy(), tmp_path)
    assert g.check("run_shell", {"command": "git log | Out-File x.txt"}).permission is ASK


def test_shell_redirection_downgrades_allow_to_ask(tmp_path: Path) -> None:
    g = gate(_default_shell_policy(), tmp_path)
    assert g.check("run_shell", {"command": "git diff > over.txt"}).permission is ASK


def test_shell_command_substitution_downgrades_allow_to_ask(tmp_path: Path) -> None:
    g = gate(_default_shell_policy(), tmp_path)
    assert g.check("run_shell", {"command": "git log $(whoami)"}).permission is ASK


def test_shell_prefix_requires_token_boundary(tmp_path: Path) -> None:
    g = gate(_default_shell_policy(), tmp_path)
    # 'git statusfoo' must NOT inherit the 'git status' allow — falls back to base ask
    assert g.check("run_shell", {"command": "git statusfoo"}).permission is ASK


def test_shell_deny_rule_unaffected_by_metacharacter_guard(tmp_path: Path) -> None:
    policy = _default_shell_policy()
    policy.shell.rules.append(ShellRule(prefix="format ", decision=DENY))
    g = gate(policy, tmp_path)
    assert g.check("run_shell", {"command": "format C: ; echo done"}).permission is DENY


# --- hardening: sensitive-path deny for reads and writes --------------------


def test_read_env_file_denied(tmp_path: Path) -> None:
    g = gate(Policy(tools={"read_file": ALLOW}), tmp_path)
    assert g.check("read_file", {"path": ".env"}).permission is DENY


def test_read_ssh_key_denied(tmp_path: Path) -> None:
    g = gate(Policy(tools={"read_file": ALLOW}), tmp_path)
    target = str(tmp_path / ".ssh" / "id_rsa")
    assert g.check("read_file", {"path": target}).permission is DENY


def test_read_env_template_allowed(tmp_path: Path) -> None:
    g = gate(Policy(tools={"read_file": ALLOW}), tmp_path)
    assert g.check("read_file", {"path": ".env.example"}).permission is ALLOW


def test_read_ordinary_file_allowed(tmp_path: Path) -> None:
    g = gate(Policy(tools={"read_file": ALLOW}), tmp_path)
    assert g.check("read_file", {"path": "src/main.py"}).permission is ALLOW


def test_read_denylist_extra_pattern_denies(tmp_path: Path) -> None:
    policy = Policy(
        tools={"read_file": ALLOW},
        filesystem=FilesystemPolicy(read_denylist=["*/private/*"]),
    )
    g = gate(policy, tmp_path)
    target = str(tmp_path / "private" / "diary.txt")
    assert g.check("read_file", {"path": target}).permission is DENY


def test_write_to_sensitive_path_denied_even_inside_allowlist(tmp_path: Path) -> None:
    # An allow base + inside the allowlist would normally allow — sensitivity wins.
    policy = Policy(tools={"write_file": ALLOW}, filesystem=FilesystemPolicy(write_allowlist=["."]))
    g = gate(policy, tmp_path)
    target = str(tmp_path / ".ssh" / "authorized_keys")
    assert g.check("write_file", {"path": target}).permission is DENY


# --- hardening: unified path resolution (root, not CWD) ---------------------


def test_gate_resolves_relative_write_against_root(tmp_path: Path) -> None:
    policy = Policy(tools={"write_file": ALLOW}, filesystem=FilesystemPolicy(write_allowlist=["."]))
    g = gate(policy, tmp_path)
    decision = g.check("write_file", {"path": "out.txt"})
    assert decision.permission is ALLOW
    # the reason names the fully-resolved target under the project root
    assert str((tmp_path / "out.txt").resolve()) in decision.reason


# --- hardening: network tools ask by default --------------------------------


def test_network_tools_default_to_ask(tmp_path: Path) -> None:
    g = gate(Policy(), tmp_path)  # empty policy -> falls through to tool_default
    assert g.check("web_search", tool_default=ASK).permission is ASK
    assert g.check("web_fetch", tool_default=ASK).permission is ASK
