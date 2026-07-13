"""PermissionGate: decide allow / ask / deny for a tool call before it runs.

Precedence for the base decision: an explicit per-tool entry in the policy, else
the tool's own ``permission_default``, else the policy default. Several families
then refine that base:

* **Sensitive paths** — reads or writes of secrets/credentials (``.env``, SSH
  keys, cloud creds, …) are denied outright, regardless of policy. This floor
  lives in :func:`jarvis.paths.is_sensitive_path` and cannot be loosened by
  editing ``permissions.yaml``.
* **Filesystem writes** — a write whose target falls outside the allowlist is
  never silently allowed; an ``allow`` base is escalated to ``ask``.
* **Filesystem reads** — additionally denied if they match the policy's
  ``read_denylist`` (on top of the sensitive-path floor), and an otherwise-allowed
  read outside ``read_allowlist`` is escalated to ``ask``.
* **Shell commands** — the longest matching prefix rule wins and overrides the
  base, *unless* the tool is denied outright (a tool-level ``deny`` is absolute).
  An ``allow`` never survives shell metacharacters (``;``, ``|``, redirection,
  command substitution): those escalate to ``ask`` so an allowlisted prefix like
  ``git status`` can't smuggle a chained ``; rm -rf`` past the gate.

All relative paths are resolved against the project root via
:func:`jarvis.paths.resolve_path` — the *same* resolution the filesystem tools
use — so the gate's decision and the tool's action always refer to one file.

The gate only decides. Actually prompting the human, and running the tool, happen
elsewhere — this stays a pure, table-testable function.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from jarvis.paths import is_sensitive_path, matches_any, resolve_path
from jarvis.permissions.policy import Policy, ShellRule, save_policy
from jarvis.tools.base import Permission

# Metacharacters that let one command become several, redirect I/O, or substitute
# command output. If any appear, an "allow" shell decision is downgraded to "ask".
# ``&&`` / ``||`` are covered by ``&`` / ``|``; newlines cover multi-statement input.
_SHELL_METACHARACTERS: tuple[str, ...] = (";", "|", "&", "`", "$(", "${", ">", "<", "\n", "\r")

# The gate must inspect the exact filesystem target for every built-in read surface.
# ``glob_search`` is the one exception to the usual ``path`` parameter name.
_DEFAULT_READ_PATH_FIELDS: dict[str, str] = {
    "read_file": "path",
    "ingest_source": "path",
    "list_dir": "path",
    "glob_search": "root",
}


def _has_shell_metacharacters(command: str) -> bool:
    return any(meta in command for meta in _SHELL_METACHARACTERS)


def _shell_path_tokens(command: str) -> list[str]:
    """Whitespace-split argument tokens that could name a file (best-effort).

    Flags (``-x``) are skipped; surrounding quotes are stripped so ``cat "…/token.json"``
    is still inspected. This is a coarse belt over the metachar rule, not a shell parser —
    the real credential protection is the read floor + on-disk permissions."""
    tokens: list[str] = []
    for raw in command.split():
        tok = raw.strip("'\"")
        if tok and not tok.startswith("-"):
            tokens.append(tok)
    return tokens


def _prefix_matches(command: str, prefix: str) -> bool:
    """True if ``command`` begins with ``prefix`` at a *token boundary*.

    A rule for ``git status`` must match ``git status`` and ``git status --short``
    but not ``git statusfoo`` — otherwise a look-alike command could inherit an
    allow. A prefix that already ends in whitespace (e.g. ``"rm "``) is treated as
    matching anything that follows.
    """
    if not command.startswith(prefix):
        return False
    rest = command[len(prefix) :]
    return rest == "" or rest[0].isspace() or prefix.endswith(" ")


@dataclass(frozen=True)
class Decision:
    """A gate outcome plus a human-readable reason (for the prompt + audit log).

    ``persistable`` is normally True; it is set False by a caller (the AgentLoop's egress
    taint rule, Phase 9) when this particular ASK must never be turned into an "always allow"
    — the approvers suppress the "always" affordance and refuse to persist it. It does not
    change the allow/ask/deny outcome, only whether a wider grant may be minted from it."""

    permission: Permission
    reason: str
    persistable: bool = True


class PermissionGate:
    def __init__(
        self,
        policy: Policy,
        project_root: Path,
        *,
        source_path: Path | None = None,
        path_tools: frozenset[str] = frozenset({"write_file"}),
        # ``read_tools`` remains as a compatibility override for callers that use the
        # legacy one-field shape. New built-in read surfaces use ``read_path_fields``.
        read_tools: frozenset[str] | None = None,
        read_path_fields: dict[str, str] | None = None,
        path_field: str = "path",
        shell_tools: frozenset[str] = frozenset({"run_shell"}),
        command_field: str = "command",
    ) -> None:
        self.policy = policy
        self.project_root = project_root.resolve()
        self.source_path = source_path
        self.path_tools = path_tools
        if read_path_fields is not None:
            self.read_path_fields = dict(read_path_fields)
        elif read_tools is not None:
            self.read_path_fields = {name: path_field for name in read_tools}
        else:
            self.read_path_fields = dict(_DEFAULT_READ_PATH_FIELDS)
        self.read_tools = frozenset(self.read_path_fields)
        self.path_field = path_field
        self.shell_tools = shell_tools
        self.command_field = command_field

    # --- decision ----------------------------------------------------------

    def check(
        self,
        tool_name: str,
        tool_input: dict | None = None,
        *,
        tool_default: Permission | None = None,
    ) -> Decision:
        tool_input = tool_input or {}
        base = self._base(tool_name, tool_default)

        if base is Permission.DENY:
            return Decision(Permission.DENY, f"'{tool_name}' is denied by policy.")

        if tool_name in self.shell_tools:
            return self._check_shell(tool_name, tool_input, base)
        if tool_name in self.path_tools:
            return self._check_path(tool_name, tool_input, base)
        if tool_name in self.read_path_fields:
            return self._check_read_path(tool_name, tool_input, base)
        return Decision(base, f"policy for '{tool_name}': {base}")

    def _base(self, tool_name: str, tool_default: Permission | None) -> Permission:
        if tool_name in self.policy.tools:
            return self.policy.tools[tool_name]
        if tool_default is not None:
            return tool_default
        return self.policy.default

    def _check_shell(self, tool_name: str, tool_input: dict, base: Permission) -> Decision:
        command = str(tool_input.get(self.command_field, "") or "").strip()

        # Absolute floor (Phase 9): a command that names an EXISTING sensitive path — a
        # connector token, a .env, an SSH key — is denied outright, even under an allowlisted
        # prefix (`cat data/connectors/google_token.json` must not ride an allowed `cat `).
        # This closes the leak where the sensitive-path floor covered read_file but not the
        # shell. Existence-gated so merely mentioning a pattern-ish string is harmless.
        sensitive = self._sensitive_command_target(command)
        if sensitive is not None:
            return Decision(
                Permission.DENY, f"shell command references a sensitive path: {sensitive}"
            )

        match: ShellRule | None = None
        for rule in self.policy.shell.rules:
            if _prefix_matches(command, rule.prefix) and (
                match is None or len(rule.prefix) > len(match.prefix)
            ):
                match = rule
        if match is not None:
            decision, reason = match.decision, f"shell rule {match.prefix!r} -> {match.decision}"
        else:
            decision, reason = base, f"shell default for '{tool_name}': {base}"

        # An allow never survives chaining/redirection/substitution — the allowlist
        # covers simple commands only; anything fancier goes back to the human.
        if decision is Permission.ALLOW and _has_shell_metacharacters(command):
            return Decision(Permission.ASK, f"{reason}; escalated to ask (shell metacharacters)")
        return Decision(decision, reason)

    def _check_path(self, tool_name: str, tool_input: dict, base: Permission) -> Decision:
        raw = tool_input.get(self.path_field)
        if not raw:
            return Decision(base, f"policy for '{tool_name}' (no path given): {base}")
        target = resolve_path(raw, self.project_root)

        if is_sensitive_path(target):
            return Decision(Permission.DENY, f"write to sensitive path denied: {target}")
        if self._within_denylist(target):
            return Decision(
                Permission.DENY,
                f"write to a provenance-managed dir denied: {target} — use write_wiki_page / "
                "ingest_source so the write is tracked",
            )
        if self._within_allowlist(target):
            return Decision(base, f"write within allowlist: {target}")
        if base is Permission.ALLOW:
            return Decision(Permission.ASK, f"write outside allowlist, escalated to ask: {target}")
        return Decision(base, f"write outside allowlist: {target}")

    def _sensitive_command_target(self, command: str) -> Path | None:
        """The first shell token that resolves to an existing sensitive file, or None."""
        for token in _shell_path_tokens(command):
            target = resolve_path(token, self.project_root)
            try:
                exists = target.exists()
            except OSError:
                exists = False
            if exists and is_sensitive_path(target):
                return target
        return None

    def _check_read_path(self, tool_name: str, tool_input: dict, base: Permission) -> Decision:
        field = self.read_path_fields[tool_name]
        raw = tool_input.get(field)
        if not raw:
            return Decision(base, f"policy for '{tool_name}' (no path given): {base}")
        target = resolve_path(raw, self.project_root)

        if is_sensitive_path(target) or matches_any(target, self.policy.filesystem.read_denylist):
            return Decision(Permission.DENY, f"read of sensitive path denied: {target}")
        if self._within_read_allowlist(target):
            return Decision(base, f"read within allowlist: {target}")
        if base is Permission.ALLOW:
            return Decision(Permission.ASK, f"read outside allowlist, escalated to ask: {target}")
        return Decision(base, f"read of '{target}': {base}")

    def _within_allowlist(self, target: Path) -> bool:
        for entry in self.policy.filesystem.write_allowlist:
            base_dir = resolve_path(entry, self.project_root)
            if target == base_dir or target.is_relative_to(base_dir):
                return True
        return False

    def _within_denylist(self, target: Path) -> bool:
        """A write under a provenance-managed dir (the knowledge base) must go through
        the tracking tools, not the generic write_file — even though it's inside the
        allowlisted project root. Denylist wins over the allowlist."""
        for entry in self.policy.filesystem.write_denylist:
            base_dir = resolve_path(entry, self.project_root)
            if target == base_dir or target.is_relative_to(base_dir):
                return True
        return False

    def _within_read_allowlist(self, target: Path) -> bool:
        for entry in self.policy.filesystem.read_allowlist:
            base_dir = resolve_path(entry, self.project_root)
            if target == base_dir or target.is_relative_to(base_dir):
                return True
        return False

    # --- persistence ("always allow") --------------------------------------

    def persist_allow(self, tool_name: str) -> None:
        """Persist a tool-level allow (coarse: allows every call to this tool)."""
        self.policy.tools[tool_name] = Permission.ALLOW
        self._save()

    def persist_shell_rule(self, prefix: str, decision: Permission = Permission.ALLOW) -> None:
        """Persist (or replace) a shell prefix rule — the granular 'always allow this
        command' path for run_shell."""
        self.policy.shell.rules = [r for r in self.policy.shell.rules if r.prefix != prefix] + [
            ShellRule(prefix=prefix, decision=decision)
        ]
        self._save()

    def persist_write_dir(self, directory: str) -> None:
        """Persist a directory into the write allowlist."""
        if directory not in self.policy.filesystem.write_allowlist:
            self.policy.filesystem.write_allowlist.append(directory)
        self._save()

    def persist_read_dir(self, directory: str) -> None:
        """Persist one external directory as a scoped silent-read area."""
        if directory not in self.policy.filesystem.read_allowlist:
            self.policy.filesystem.read_allowlist.append(directory)
        self._save()

    def _save(self) -> None:
        if self.source_path is not None:
            save_policy(self.policy, self.source_path)
