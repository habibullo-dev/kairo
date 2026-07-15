"""Gate read models for the workstation (Phase 8, Task 3): a read-only policy snapshot and
today's audit trail. Both are *views* — no route here changes anything (mutation is only the
approval resolve, which lives in the approver). The audit reader parses the same JSONL the
whole app writes, so the Gate screen and the on-disk log tell one story (ADR-0008 §3).
"""

from __future__ import annotations

import datetime as _dt
import gzip
import json
from pathlib import Path
from typing import TYPE_CHECKING

from jarvis.observability.logging import READABLE_LOG_PREFIXES, parse_log_filename

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


def policy_snapshot(gate: PermissionGate) -> dict:
    """A JSON-safe, read-only view of the active policy (defaults, per-tool decisions, the
    filesystem allow/deny lists, and the persisted shell prefix rules)."""
    return {"policy": gate.policy.model_dump(mode="json")}


def read_today_audit(logs_dir: Path, *, limit: int = 200, date: str | None = None) -> list[dict]:
    """Return today's audit lines relevant to the Gate (most recent last), capped at
    ``limit``. Rotated gzip segments are included before the live JSONL file, so a busy
    day's evidence does not disappear from the Gate. A missing log set yields ``[]``."""
    if limit <= 0:
        return []
    day = date or _dt.datetime.now().strftime("%Y-%m-%d")
    by_prefix: dict[str, list[tuple[int | None, Path]]] = {
        prefix: [] for prefix in READABLE_LOG_PREFIXES
    }
    try:
        candidates = list(logs_dir.iterdir())
    except OSError:
        return []
    for candidate in candidates:
        parsed = parse_log_filename(candidate.name)
        if parsed is None or not candidate.is_file():
            continue
        prefix, candidate_day, index = parsed
        if candidate_day == day:
            by_prefix[prefix].append((index, candidate))

    paths: list[Path] = []
    # Legacy files were written before canonical Kira files during an in-place upgrade. Within
    # each family a larger archive index is older, followed by the live segment.
    for prefix in READABLE_LOG_PREFIXES:
        archived = [
            (index, candidate) for index, candidate in by_prefix[prefix] if index is not None
        ]
        paths.extend(path for _index, path in sorted(archived, reverse=True))
        paths.extend(candidate for index, candidate in by_prefix[prefix] if index is None)

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
