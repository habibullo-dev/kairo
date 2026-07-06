"""Gate read models for the workstation (Phase 8, Task 3): a read-only policy snapshot and
today's audit trail. Both are *views* — no route here changes anything (mutation is only the
approval resolve, which lives in the approver). The audit reader parses the same JSONL the
whole app writes, so the Gate screen and the on-disk log tell one story (ADR-0008 §3).
"""

from __future__ import annotations

import datetime as _dt
import json
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from jarvis.permissions.gate import PermissionGate

#: Audit events the Gate screen surfaces — permission decisions, tool activity, and the
#: UI's own approval lines (channel=ui). Everything else in the log is left out of this view.
AUDIT_EVENTS = frozenset(
    {
        "permission_decision",
        "permission_resolved",
        "tool_call",
        "tool_denied",
        "ui_approval_requested",
        "ui_approval_resolved",
        "ui_approval_failed_closed",
    }
)


def policy_snapshot(gate: PermissionGate) -> dict:
    """A JSON-safe, read-only view of the active policy (defaults, per-tool decisions, the
    filesystem allow/deny lists, and the persisted shell prefix rules)."""
    return {"policy": gate.policy.model_dump(mode="json")}


def read_today_audit(logs_dir: Path, *, limit: int = 200, date: str | None = None) -> list[dict]:
    """Return today's audit lines relevant to the Gate (most recent last), capped at
    ``limit``. A missing/absent log file yields ``[]`` (a fresh box has nothing yet)."""
    day = date or _dt.datetime.now().strftime("%Y-%m-%d")
    path = logs_dir / f"jarvis-{day}.jsonl"
    if not path.exists():
        return []
    out: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if rec.get("event") in AUDIT_EVENTS:
            out.append(rec)
    return out[-limit:]
