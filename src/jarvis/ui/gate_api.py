"""Gate read models for the workstation (Phase 8, Task 3): a read-only policy snapshot and
today's audit trail. Both are *views* — no route here changes anything (mutation is only the
approval resolve, which lives in the approver). The audit reader parses the same JSONL the
whole app writes, so the Gate screen and the on-disk log tell one story (ADR-0008 §3).
"""

from __future__ import annotations

import datetime as _dt
import gzip
import json
import re
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
        "egress",  # the "what left the box" ledger (Phase 9, amendment A5)
        "egress_taint_demotion",  # an egress ALLOW became a non-persistable ASK this turn
        "ui_approval_requested",
        "ui_approval_resolved",
        "ui_approval_failed_closed",
        "mode_changed",  # Phase 10: run mode flipped (plan|approval|auto)
        "mode_auto_approved",  # Phase 10: Auto mode resolved an allowlisted ASK without a human
    }
)

_ROTATED_LOG_PATTERN = re.compile(r"jarvis-(?P<day>\d{4}-\d{2}-\d{2})\.(?P<index>\d+)\.jsonl\.gz$")


def policy_snapshot(gate: PermissionGate) -> dict:
    """A JSON-safe, read-only view of the active policy (defaults, per-tool decisions, the
    filesystem allow/deny lists, and the persisted shell prefix rules)."""
    return {"policy": gate.policy.model_dump(mode="json")}


def read_today_audit(logs_dir: Path, *, limit: int = 200, date: str | None = None) -> list[dict]:
    """Return today's audit lines relevant to the Gate (most recent last), capped at
    ``limit``. Rotated gzip segments are included before the live JSONL file, so a busy
    day's evidence does not disappear from the Gate. A missing log set yields ``[]``."""
    day = date or _dt.datetime.now().strftime("%Y-%m-%d")
    archived: list[tuple[int, Path]] = []
    for candidate in logs_dir.glob(f"jarvis-{day}.*.jsonl.gz"):
        match = _ROTATED_LOG_PATTERN.fullmatch(candidate.name)
        if match is not None and match.group("day") == day:
            archived.append((int(match.group("index")), candidate))
    paths = [path for _index, path in sorted(archived, reverse=True)]
    live = logs_dir / f"jarvis-{day}.jsonl"
    if live.exists():
        paths.append(live)

    out: list[dict] = []
    for path in paths:
        try:
            if path.suffix == ".gz":
                with gzip.open(path, "rt", encoding="utf-8") as handle:
                    lines = handle.readlines()
            else:
                lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
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
