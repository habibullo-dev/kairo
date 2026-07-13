"""Shared 'always allow' persistence — one truth for every interface (Phase 8).

The narrow-persist discipline (persist a shell rule by prefix, filesystem access by its
resolved directory, refuse over-broad/sensitive dirs, and never persist a deferred-execution sink)
originated inside the REPL. The workstation UI must apply the exact same rule when a human
clicks "Always allow (narrow)", so drift here is a safety bug. Extracting it to a pure
function over ``(gate, config, call)`` gives the REPL and the UI one implementation — the
REPL's existing approval tests are the parity pin (ADR-0008 §3).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from jarvis.observability import get_logger
from jarvis.paths import is_safe_to_persist_dir, resolve_path
from jarvis.tools import Permission

if TYPE_CHECKING:
    from jarvis.config import Config
    from jarvis.core.client import ToolCall
    from jarvis.permissions.gate import PermissionGate

#: Never "always"-able, in ANY interface: a single stray "always" must not permanently open
#: a deferred-execution injection sink (schedule_task), let the model silence the user's
#: reminders (cancel_task), or open a scoped-execution channel (spawn_agent). Per-instance
#: approval only. (Mirrors the sub-agent gate's NEVER_GRANTABLE for its own tools.)
NEVER_PERSIST = frozenset({"schedule_task", "cancel_task", "spawn_agent"})


def persist_always(gate: PermissionGate, config: Config, call: ToolCall, *, log=None) -> str | None:
    """Persist an 'always allow' choice as narrowly as the tool allows.

    Returns a short human-readable description of what was persisted (for the audit line
    and the UI's confirmation), or ``None`` when nothing was persisted — a never-persist
    tool, an empty target, or an over-broad/sensitive write dir. In every ``None`` case the
    *current* action still proceeds on this one approval; we simply refuse to widen from it.

    * ``run_shell`` → the command's longest-prefix rule (so ``git status`` allows future
      ``git status …`` but not ``git push``).
    * ``write_file`` → the *resolved* parent directory (resolved against the workspace root
      exactly as the gate resolves it), unless it is a drive root / home / sensitive path.
    * ``read_file`` → its resolved parent directory; ``list_dir`` / ``glob_search`` → their
      resolved target directory. This is a read-scope grant, never a global tool allow.
    * anything else → a tool-level allow.
    """
    log = log or get_logger("jarvis.permissions.approvals")
    inp = call.input or {}
    if call.name in NEVER_PERSIST:
        log.info("always_allow_refused", tool=call.name, reason="deferred_execution_sink")
        return None
    if call.name == "run_shell":
        command = str(inp.get("command", "")).strip()
        if not command:
            return None
        gate.persist_shell_rule(command, Permission.ALLOW)
        return f"shell prefix «{command}»"
    if call.name == "write_file":
        raw = inp.get("path")
        if not raw:
            return None
        parent = resolve_path(raw, config.root).parent
        if is_safe_to_persist_dir(parent):
            gate.persist_write_dir(str(parent))
            return f"write dir {parent}"
        log.warning("always_allow_not_persisted", dir=str(parent), reason="too broad/sensitive")
        return None
    if call.name in {"read_file", "list_dir", "glob_search"}:
        field = gate.read_path_fields[call.name]
        raw = inp.get(field)
        if not raw:
            return None
        target = resolve_path(raw, config.root)
        directory = target.parent if call.name == "read_file" else target
        if is_safe_to_persist_dir(directory):
            gate.persist_read_dir(str(directory))
            return f"read dir {directory}"
        log.warning("always_allow_not_persisted", dir=str(directory), reason="too broad/sensitive")
        return None
    gate.persist_allow(call.name)
    return f"tool {call.name}"
