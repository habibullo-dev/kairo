"""Permission policy model + load/save for ``config/permissions.yaml``.

The policy is data, deliberately separate from the :class:`PermissionGate` that
interprets it. It carries per-tool decisions, a filesystem write allowlist, and
ordered shell prefix rules. It round-trips to YAML so an "always allow" choice at
the prompt can be persisted.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, Field

from jarvis.tools.base import Permission


class ShellRule(BaseModel):
    """A prefix -> decision rule for shell commands (longest match wins)."""

    prefix: str
    decision: Permission


class ShellPolicy(BaseModel):
    rules: list[ShellRule] = Field(default_factory=list)


class FilesystemPolicy(BaseModel):
    # Dirs (relative to project root, or absolute) where writes may be auto-allowed.
    write_allowlist: list[str] = Field(default_factory=lambda: ["."])


class Policy(BaseModel):
    """Full permission policy. Sensible-safe defaults if the file is absent."""

    default: Permission = Permission.ASK
    tools: dict[str, Permission] = Field(default_factory=dict)
    filesystem: FilesystemPolicy = Field(default_factory=FilesystemPolicy)
    shell: ShellPolicy = Field(default_factory=ShellPolicy)


def load_policy(path: Path) -> Policy:
    """Load a policy from YAML. A missing file yields safe defaults (ask-all)."""
    if not path.exists():
        return Policy()
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a YAML mapping at the top level.")
    return Policy.model_validate(data)


def save_policy(policy: Policy, path: Path) -> None:
    """Write a policy back to YAML. Comments in the original file are not preserved."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(policy.model_dump(mode="json"), sort_keys=False),
        encoding="utf-8",
    )
