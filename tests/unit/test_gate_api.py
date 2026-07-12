"""Gate audit reader behavior, including compressed same-day log segments."""

from __future__ import annotations

import gzip
import json
from pathlib import Path

from jarvis.ui.gate_api import read_today_audit


def _write_jsonl(path: Path, *events: dict, compressed: bool = False) -> None:
    lines = "".join(f"{json.dumps(event)}\n" for event in events)
    if compressed:
        with gzip.open(path, "wt", encoding="utf-8") as handle:
            handle.write(lines)
    else:
        path.write_text(lines, encoding="utf-8")


def test_read_today_audit_merges_rotated_segments_in_chronological_order(tmp_path: Path) -> None:
    day = "2026-07-06"
    _write_jsonl(
        tmp_path / f"jarvis-{day}.2.jsonl.gz",
        {"event": "tool_call", "sequence": 1},
        compressed=True,
    )
    _write_jsonl(
        tmp_path / f"jarvis-{day}.1.jsonl.gz",
        {"event": "tool_call", "sequence": 2},
        compressed=True,
    )
    _write_jsonl(
        tmp_path / f"jarvis-{day}.jsonl",
        {"event": "ignored", "sequence": 0},
        {"event": "tool_call", "sequence": 3},
    )

    events = read_today_audit(tmp_path, date=day, limit=2)

    assert [event["sequence"] for event in events] == [2, 3]
