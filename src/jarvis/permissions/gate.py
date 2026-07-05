"""PermissionGate: decide allow / ask / deny for a tool call before it runs.

Precedence for the base decision: an explicit per-tool entry in the policy, else
the tool's own ``permission_default``, else the policy default. Two families then
refine that base:

* **Filesystem writes** — a write whose target falls outside the allowlist is
  never silently allowed; an ``allow`` base is escalated to ``ask``.
* **Shell commands** — the longest matching prefix rule wins and overrides the
  base, *unless* the tool is denied outright (a tool-level ``deny`` is absolute).

The gate only decides. Actually prompting the human, and running the tool, happen
elsewhere — this stays a pure, table-testable function.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from jarvis.permissions.policy import Policy, ShellRule, save_policy
from jarvis.tools.base import Permission


@dataclass(frozen=True)
class Decision:
    """A gate outcome plus a human-readable reason (for the prompt + audit log)."""

    permission: Permission
    reason: str


class PermissionGate:
    def __init__(
        self,
        policy: Policy,
        project_root: Path,
        *,
        source_path: Path | None = None,
        path_tools: frozenset[str] = frozenset({"write_file"}),
        path_field: str = "path",
        shell_tools: frozenset[str] = frozenset({"run_shell"}),
        command_field: str = "command",
    ) -> None:
        self.policy = policy
        self.project_root = project_root.resolve()
        self.source_path = source_path
        self.path_tools = path_tools
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
        return Decision(base, f"policy for '{tool_name}': {base}")

    def _base(self, tool_name: str, tool_default: Permission | None) -> Permission:
        if tool_name in self.policy.tools:
            return self.policy.tools[tool_name]
        if tool_default is not None:
            return tool_default
        return self.policy.default

    def _check_shell(self, tool_name: str, tool_input: dict, base: Permission) -> Decision:
        command = str(tool_input.get(self.command_field, "") or "")
        match: ShellRule | None = None
        for rule in self.policy.shell.rules:
            if command.startswith(rule.prefix) and (
                match is None or len(rule.prefix) > len(match.prefix)
            ):
                match = rule
        if match is not None:
            return Decision(match.decision, f"shell rule {match.prefix!r} -> {match.decision}")
        return Decision(base, f"shell default for '{tool_name}': {base}")

    def _check_path(self, tool_name: str, tool_input: dict, base: Permission) -> Decision:
        raw = tool_input.get(self.path_field)
        if not raw:
            return Decision(base, f"policy for '{tool_name}' (no path given): {base}")
        target = Path(str(raw))
        if not target.is_absolute():
            target = self.project_root / target
        target = target.resolve()

        if self._within_allowlist(target):
            return Decision(base, f"write within allowlist: {target}")
        if base is Permission.ALLOW:
            return Decision(Permission.ASK, f"write outside allowlist, escalated to ask: {target}")
        return Decision(base, f"write outside allowlist: {target}")

    def _within_allowlist(self, target: Path) -> bool:
        for entry in self.policy.filesystem.write_allowlist:
            base_dir = Path(entry)
            if not base_dir.is_absolute():
                base_dir = self.project_root / base_dir
            base_dir = base_dir.resolve()
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

    def _save(self) -> None:
        if self.source_path is not None:
            save_policy(self.policy, self.source_path)
