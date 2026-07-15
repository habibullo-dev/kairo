"""PermissionGate + policy tests: every allow/ask/deny path, and persistence."""

from __future__ import annotations

from pathlib import Path

import pytest

from kira.config import load_config
from kira.core import ToolCall
from kira.permissions import (
    PermissionGate,
    Policy,
    ShellRule,
    load_policy,
    persist_always,
    save_policy,
)
from kira.permissions.policy import FilesystemPolicy, ShellPolicy
from kira.tools.base import Permission

ALLOW, ASK, DENY = Permission.ALLOW, Permission.ASK, Permission.DENY


def gate(policy: Policy, root: Path, **kw) -> PermissionGate:
    return PermissionGate(policy, root, **kw)


# --- policy load/save ------------------------------------------------------


def test_load_missing_file_gives_safe_defaults(tmp_path: Path) -> None:
    p = load_policy(tmp_path / "nope.yaml")
    assert p.default is ASK
    assert p.tools == {}
    assert p.filesystem.write_allowlist == ["."]
    assert p.filesystem.read_allowlist == ["."]


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


# --- knowledge base (Phase 4) gate wiring ----------------------------------


def test_ingest_source_sensitive_path_denied_by_default_gate(tmp_path: Path) -> None:
    # ingest_source is in the DEFAULT read_tools, and its file param is named `path`,
    # so the sensitive-path floor fires — the whole "conversion is gated like a read"
    # story depends on this wiring.
    (tmp_path / ".env").write_text("SECRET=1", encoding="utf-8")
    g = gate(Policy(tools={"ingest_source": ALLOW}), tmp_path)
    assert g.check("ingest_source", {"path": ".env"}).permission is DENY


def test_gate_read_write_tools_have_correct_path_fields() -> None:
    # Self-consistency: every tool the gate does path-checking for must actually have a
    # `path` param, or the check silently reads None and the floor never runs. This
    # class of misconfiguration passes every functional test otherwise.
    from kira.tools import ToolContext, ToolRegistry

    reg = ToolRegistry()
    reg.discover("kira.tools.builtin", ToolContext())  # phase-1 tools always register
    g = PermissionGate(Policy(), Path("."))
    for name in g.path_tools:
        tool = reg.get(name)
        if tool is None:
            continue  # optional tools (ingest_source) may be absent without a service
        assert g.path_field in tool.Params.model_fields, (
            f"{name} is gate-path-checked but has no '{g.path_field}' param"
        )
    for name, field in g.read_path_fields.items():
        tool = reg.get(name)
        if tool is None:
            continue  # optional tools (ingest_source) may be absent without a service
        assert field in tool.Params.model_fields, (
            f"{name} is read-path-checked but has no '{field}' param"
        )


def test_write_file_denied_under_knowledge_dir(tmp_path: Path) -> None:
    # the generic write_file must not bypass wiki provenance by writing into the KB dir,
    # even though data/ is inside the '.' allowlist. write_denylist wins.
    policy = Policy(tools={"write_file": ALLOW})  # default write_denylist = data/knowledge
    g = gate(policy, tmp_path)
    decision = g.check("write_file", {"path": "data/knowledge/wiki/evil.md"})
    assert decision.permission is DENY
    assert "write_wiki_page" in decision.reason  # actionable: use the tracking tool
    # a normal write elsewhere is unaffected
    assert g.check("write_file", {"path": "notes.txt"}).permission is ALLOW


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


# --- shell sensitive-path floor (Phase 9: token custody) -------------------


def _cat_allowed() -> Policy:
    return Policy(shell=ShellPolicy(rules=[ShellRule(prefix="cat", decision=ALLOW)]))


def test_shell_command_naming_connector_token_is_denied(tmp_path: Path) -> None:
    # The cross-cutting floor: `cat data/connectors/google_token.json` must be DENY even under
    # an allowlisted `cat ` — closing the leak where the floor covered read_file but not shell.
    (tmp_path / "data" / "connectors").mkdir(parents=True)
    (tmp_path / "data" / "connectors" / "google_token.json").write_text("{}", encoding="utf-8")
    g = gate(_cat_allowed(), tmp_path)
    d = g.check("run_shell", {"command": "cat data/connectors/google_token.json"}, tool_default=ASK)
    assert d.permission is DENY
    assert "sensitive path" in d.reason


def test_shell_command_naming_dotenv_is_denied(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("SECRET=1", encoding="utf-8")
    g = gate(_cat_allowed(), tmp_path)
    assert g.check("run_shell", {"command": "cat .env"}, tool_default=ASK).permission is DENY


def test_shell_quoted_sensitive_path_still_denied(tmp_path: Path) -> None:
    # Surrounding quotes don't evade the floor (tokens are unquoted before checking).
    (tmp_path / "data" / "connectors").mkdir(parents=True)
    (tmp_path / "data" / "connectors" / "kakao_token.json").write_text("{}", encoding="utf-8")
    g = gate(_cat_allowed(), tmp_path)
    d = g.check(
        "run_shell", {"command": 'cat "data/connectors/kakao_token.json"'}, tool_default=ASK
    )
    assert d.permission is DENY


def test_shell_nonexistent_sensitive_path_not_floored(tmp_path: Path) -> None:
    # Existence-gated: naming a not-present path is harmless (cat errors), so the normal
    # allow rule applies — the floor is about actually reaching a real secret.
    g = gate(_cat_allowed(), tmp_path)
    d = g.check("run_shell", {"command": "cat data/connectors/ghost.json"}, tool_default=ASK)
    assert d.permission is ALLOW


def test_shell_ordinary_file_unaffected_by_floor(tmp_path: Path) -> None:
    (tmp_path / "notes.txt").write_text("hi", encoding="utf-8")
    g = gate(_cat_allowed(), tmp_path)
    assert g.check("run_shell", {"command": "cat notes.txt"}, tool_default=ASK).permission is ALLOW


def test_decision_persistable_defaults_true(tmp_path: Path) -> None:
    g = gate(Policy(), tmp_path)
    assert g.check("read_file", {"path": "x"}).persistable is True


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


# --- filesystem read allowlist ---------------------------------------------


@pytest.mark.parametrize(
    ("tool_name", "tool_input"),
    [
        ("read_file", {"path": "notes/todo.txt"}),
        ("list_dir", {"path": "notes"}),
        ("glob_search", {"root": "notes", "pattern": "*.txt"}),
    ],
)
def test_project_read_surfaces_keep_allow(
    tmp_path: Path, tool_name: str, tool_input: dict[str, str]
) -> None:
    g = gate(Policy(), tmp_path)
    assert g.check(tool_name, tool_input, tool_default=ALLOW).permission is ALLOW


@pytest.mark.parametrize(
    ("tool_name", "tool_input"),
    [
        ("read_file", {"path": "outside/note.txt"}),
        ("list_dir", {"path": "outside"}),
        ("glob_search", {"root": "outside", "pattern": "*.txt"}),
    ],
)
def test_external_read_surfaces_escalate_allow_to_ask(
    tmp_path: Path, tool_name: str, tool_input: dict[str, str]
) -> None:
    outside = tmp_path.parent / "outside"
    rewritten = {
        key: str(outside / value) if key in {"path", "root"} else value
        for key, value in tool_input.items()
    }
    g = gate(Policy(), tmp_path)
    decision = g.check(tool_name, rewritten, tool_default=ALLOW)
    assert decision.permission is ASK
    assert "outside allowlist" in decision.reason


def test_read_allowlist_permits_one_external_directory_not_its_parent(tmp_path: Path) -> None:
    external = tmp_path.parent / "external" / "shared"
    policy = Policy(filesystem=FilesystemPolicy(read_allowlist=[".", str(external)]))
    g = gate(policy, tmp_path)
    assert (
        g.check("read_file", {"path": str(external / "note.txt")}, tool_default=ALLOW).permission
        is ALLOW
    )
    assert (
        g.check(
            "read_file", {"path": str(external.parent / "private.txt")}, tool_default=ALLOW
        ).permission
        is ASK
    )


def test_read_scope_never_loosens_an_ask_base(tmp_path: Path) -> None:
    g = gate(Policy(tools={"read_file": ASK}), tmp_path)
    assert g.check("read_file", {"path": "notes/todo.txt"}).permission is ASK
    assert g.check("read_file", {"path": str(tmp_path.parent / "outside.txt")}).permission is ASK


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


def test_persist_read_dir(tmp_path: Path) -> None:
    src = tmp_path / "permissions.yaml"
    save_policy(Policy(), src)
    g = gate(load_policy(src), tmp_path, source_path=src)
    g.persist_read_dir("reference")
    assert "reference" in load_policy(src).filesystem.read_allowlist


def test_always_on_external_read_persists_only_the_read_directory(tmp_path: Path) -> None:
    config = load_config(root=tmp_path, env_file=None)
    external = tmp_path.parent / f"{tmp_path.name}-external" / "reference"
    external.mkdir(parents=True)
    g = gate(Policy(), tmp_path)
    saved = persist_always(
        g,
        config,
        ToolCall(id="c1", name="read_file", input={"path": str(external / "brief.md")}),
    )
    assert saved == f"read dir {external}"
    assert g.policy.tools == {}
    assert (
        g.check("read_file", {"path": str(external / "brief.md")}, tool_default=ALLOW).permission
        is ALLOW
    )
    assert (
        g.check(
            "read_file", {"path": str(external.parent / "other.md")}, tool_default=ALLOW
        ).permission
        is ASK
    )


@pytest.mark.parametrize(
    ("name", "tool_input"),
    [
        ("list_dir", {"path": "external/reference"}),
        ("glob_search", {"root": "external/reference", "pattern": "*.md"}),
    ],
)
def test_always_on_external_directory_read_persists_that_directory(
    tmp_path: Path, name: str, tool_input: dict[str, str]
) -> None:
    config = load_config(root=tmp_path, env_file=None)
    external = tmp_path.parent / f"{tmp_path.name}-external" / "reference"
    external.mkdir(parents=True)
    rewritten = {
        key: str(external) if key in {"path", "root"} else value
        for key, value in tool_input.items()
    }
    g = gate(Policy(), tmp_path)
    saved = persist_always(g, config, ToolCall(id="c1", name=name, input=rewritten))
    assert saved == f"read dir {external}"
    assert g.policy.tools == {}
    assert g.check(name, rewritten, tool_default=ALLOW).permission is ALLOW


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
