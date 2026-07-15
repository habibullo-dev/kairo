"""The sub-agent permission regime — the safety core of Phase 6's *second* gate.

A delegated child runs one scoped ``AgentLoop`` turn. The human already approved the
delegation *contract* (the full prompt + tool scope) at the spawn prompt — that's gate
one. :class:`SubAgentGate` is gate two: it wraps whatever gate the parent run used (the
interactive :class:`~kira.permissions.gate.PermissionGate`, or an
:class:`~kira.permissions.unattended.UnattendedGate`) and can only ever *narrow* its
decisions, never widen them. In order:

1. **Hard denies**, before any policy: ``spawn_agent`` (depth 1 — no recursion or
   swarm) plus the state-mutating meta tools (``schedule_task``/``cancel_task``/
   ``remember``/``forget``). A sub-agent cannot delegate further, schedule work, drop
   your reminders, or write memory — on any authority.
2. **Scope check**: a tool not in *this run's* allowlist is denied, regardless of
   policy. (The child's registry is already filtered to the scope; this enforces it
   again at call time — scope is checked twice, by design.)
3. **Delegate to the inner gate**: every floor survives composition — sensitive-path
   denial, write-allowlist escalation, KB write-denylist, shell-metacharacter
   escalation, prefix rules. An inner DENY is preserved; an inner ALLOW passes (the
   human approved this tool being in scope, and it's user-set policy).
4. **Run-scoped grants**: an inner ASK the human already blessed *for a matching
   pattern* this run (see :func:`SubAgentGate.grant`) is upgraded to ALLOW — so a
   research child working one docs site prompts once, not per fetch.

Grants are **pattern-scoped** (a host, or a directory prefix — never a blanket
tool-level allow for the dangerous tools), **never persisted** to ``permissions.yaml``,
and **die with the run**. ``run_shell`` and ``write_file`` are never grantable: every
shell command and file write is individually approved, always.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from kira.paths import resolve_path
from kira.permissions.gate import Decision
from kira.tools.base import Permission

if TYPE_CHECKING:
    from kira.permissions.gate import PermissionGate
    from kira.permissions.unattended import UnattendedGate

#: Tools a sub-agent may NEVER call, before any policy or scope check. Mirrors
#: :data:`kira.permissions.unattended.HARD_DENY` (they happen to coincide) but kept
#: independent so the two regimes evolve separately: unattended denies these because no
#: human is present; a sub-agent is denied ``spawn_agent`` for depth-1 and the meta
#: tools because a scoped delegate must not mutate durable state.
SUBAGENT_HARD_DENY: frozenset[str] = frozenset(
    {"spawn_agent", "schedule_task", "cancel_task", "remember", "forget"}
)

#: Tools whose per-call approval can NEVER be widened to a run-scoped grant — each
#: shell command and each file write is approved individually, every time.
NEVER_GRANTABLE: frozenset[str] = frozenset({"run_shell", "write_file"})


@dataclass(frozen=True)
class Grant:
    """A run-scoped "a-for-this-run" allowance, narrowed to a pattern.

    ``kind`` is ``'tool'`` (any call to the tool — used only where the input carries no
    exploitable target, e.g. ``web_search``/``query_knowledge_base``), ``'host'`` (a
    ``web_fetch`` limited to one URL host), or ``'dir'`` (a filesystem read limited to a
    resolved directory and everything under it)."""

    tool: str
    kind: str
    value: str  # '' for 'tool'; a host for 'host'; a resolved dir path for 'dir'

    def describe(self) -> str:
        if self.kind == "host":
            return f"{self.tool} on host {self.value} (this run)"
        if self.kind == "dir":
            return f"{self.tool} under {self.value} (this run)"
        return f"{self.tool} (this run)"


def _host_of(url: object) -> str | None:
    if not isinstance(url, str) or not url:
        return None
    host = urlparse(url).hostname
    return host.lower() if host else None


def _derive_grant(tool_name: str, tool_input: dict, project_root: Path) -> Grant | None:
    """The pattern a fresh "a-for-this-run" grants for ``tool_name``, or None if the
    tool isn't grantable (``run_shell``/``write_file``, or a URL with no host)."""
    if tool_name == "web_fetch":
        host = _host_of(tool_input.get("url"))
        return Grant(tool_name, "host", host) if host else None
    if tool_name == "read_file":
        raw = tool_input.get("path")
        if not raw:
            return None
        # path points at a file → grant its parent directory (and everything under it).
        return Grant(tool_name, "dir", str(resolve_path(raw, project_root).parent))
    if tool_name in ("list_dir", "glob_search"):
        field = "root" if tool_name == "glob_search" else "path"
        raw = tool_input.get(field) or "."
        # path/root points at a directory → grant that directory itself.
        return Grant(tool_name, "dir", str(resolve_path(raw, project_root)))
    if tool_name in ("web_search", "query_knowledge_base"):
        return Grant(tool_name, "tool", "")
    return None  # run_shell, write_file, unknown: not grantable


class SubAgentGate:
    """Wraps a parent gate, tightening its decisions for one delegated child run.

    Same ``check`` signature as the wrapped gate, so an
    :class:`~kira.core.agent.AgentLoop` uses it interchangeably. ``scope`` is the
    child's tool allowlist; ``project_root`` resolves path grants the same way the
    inner gate resolves paths. ``denied`` counts scope/hard-deny denials this run (the
    service sums it into the run's ``denied_count``)."""

    def __init__(
        self,
        inner: PermissionGate | UnattendedGate,
        *,
        scope: frozenset[str],
        project_root: Path,
    ) -> None:
        self.inner = inner
        self.scope = scope
        self.project_root = project_root.resolve()
        self._grants: list[Grant] = []
        self.denied = 0

    def check(
        self,
        tool_name: str,
        tool_input: dict | None = None,
        *,
        tool_default: Permission | None = None,
    ) -> Decision:
        tool_input = tool_input or {}

        # 1. Hard denies — before any policy or scope, absolute.
        if tool_name in SUBAGENT_HARD_DENY:
            self.denied += 1
            return Decision(
                Permission.DENY,
                f"'{tool_name}' is unavailable to sub-agents "
                "(no recursion, no scheduling/memory writes)",
            )

        # 2. Scope — a tool the spawn didn't include is denied regardless of policy.
        if tool_name not in self.scope:
            self.denied += 1
            return Decision(
                Permission.DENY, f"'{tool_name}' is outside this sub-agent's tool scope"
            )

        # 3. Inner gate — every floor and escalation survives composition.
        decision = self.inner.check(tool_name, tool_input, tool_default=tool_default)

        # 4. Run-scoped grant: only ever upgrades an ASK the human already blessed for a
        #    matching pattern. An inner DENY/ALLOW is never touched.
        if decision.permission is Permission.ASK and self._is_granted(tool_name, tool_input):
            return Decision(
                Permission.ALLOW,
                f"granted for this sub-agent run (was: {decision.reason})",
            )
        return decision

    # --- run-scoped grants (set by the approver on "a-for-this-run") -------

    def grant(self, tool_name: str, tool_input: dict | None = None) -> Grant | None:
        """Record a run-scoped pattern grant derived from a just-approved call. Returns
        the :class:`Grant` (so the caller can describe it), or None if the tool isn't
        grantable — in which case the approval stands for this one call only."""
        grant = _derive_grant(tool_name, tool_input or {}, self.project_root)
        if grant is not None:
            self._grants.append(grant)
        return grant

    def _is_granted(self, tool_name: str, tool_input: dict) -> bool:
        return any(self._matches(g, tool_name, tool_input) for g in self._grants)

    def _matches(self, grant: Grant, tool_name: str, tool_input: dict) -> bool:
        if grant.tool != tool_name:
            return False
        if grant.kind == "tool":
            return True
        if grant.kind == "host":
            return _host_of(tool_input.get("url")) == grant.value
        if grant.kind == "dir":
            raw = tool_input.get("root") if tool_name == "glob_search" else tool_input.get("path")
            if tool_name in ("list_dir", "glob_search") and not raw:
                raw = "."
            if not raw:
                return False
            target = resolve_path(raw, self.project_root)
            if tool_name == "read_file":
                target = target.parent
            base = Path(grant.value)
            return target == base or target.is_relative_to(base)
        return False
