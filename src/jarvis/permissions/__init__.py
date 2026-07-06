"""Permissions: the gate every side effect passes through before it runs."""

from jarvis.permissions.approvals import NEVER_PERSIST, persist_always
from jarvis.permissions.gate import Decision, PermissionGate
from jarvis.permissions.policy import (
    FilesystemPolicy,
    Policy,
    ShellPolicy,
    ShellRule,
    load_policy,
    save_policy,
)
from jarvis.permissions.subagent import (
    NEVER_GRANTABLE,
    SUBAGENT_HARD_DENY,
    Grant,
    SubAgentGate,
)
from jarvis.permissions.unattended import (
    DEMOTE_ALLOW,
    HARD_DENY,
    HeadlessApprover,
    UnattendedGate,
)

__all__ = [
    "DEMOTE_ALLOW",
    "HARD_DENY",
    "NEVER_GRANTABLE",
    "NEVER_PERSIST",
    "SUBAGENT_HARD_DENY",
    "Decision",
    "FilesystemPolicy",
    "Grant",
    "HeadlessApprover",
    "PermissionGate",
    "Policy",
    "ShellPolicy",
    "ShellRule",
    "SubAgentGate",
    "load_policy",
    "persist_always",
    "save_policy",
    "UnattendedGate",
]
