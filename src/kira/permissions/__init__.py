"""Permissions: the gate every side effect passes through before it runs."""

from kira.permissions.approvals import NEVER_PERSIST, persist_always
from kira.permissions.gate import Decision, PermissionGate
from kira.permissions.policy import (
    FilesystemPolicy,
    Policy,
    ShellPolicy,
    ShellRule,
    load_policy,
    save_policy,
)
from kira.permissions.subagent import (
    NEVER_GRANTABLE,
    SUBAGENT_HARD_DENY,
    Grant,
    SubAgentGate,
)
from kira.permissions.unattended import (
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
