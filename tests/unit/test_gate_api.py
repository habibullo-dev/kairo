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


def test_read_today_audit_keeps_legacy_history_before_canonical_kira_logs(
    tmp_path: Path,
) -> None:
    day = "2026-07-06"
    for prefix, index, sequence in (
        ("jarvis", 2, 1),
        ("jarvis", 1, 2),
        ("kira", 2, 4),
        ("kira", 1, 5),
    ):
        _write_jsonl(
            tmp_path / f"{prefix}-{day}.{index}.jsonl.gz",
            {"event": "tool_call", "sequence": sequence},
            compressed=True,
        )
    _write_jsonl(
        tmp_path / f"jarvis-{day}.jsonl",
        {"event": "tool_call", "sequence": 3},
    )
    _write_jsonl(
        tmp_path / f"kira-{day}.jsonl",
        {"event": "tool_call", "sequence": 6},
    )
    _write_jsonl(
        tmp_path / f"not-kira-{day}.jsonl",
        {"event": "tool_call", "sequence": 999},
    )

    events = read_today_audit(tmp_path, date=day)

    assert [event["sequence"] for event in events] == [1, 2, 3, 4, 5, 6]
    assert [event["sequence"] for event in read_today_audit(tmp_path, date=day, limit=4)] == [
        3,
        4,
        5,
        6,
    ]


def test_read_today_audit_rejects_nonpositive_limits_and_missing_directories(
    tmp_path: Path,
) -> None:
    day = "2026-07-06"
    _write_jsonl(
        tmp_path / f"kira-{day}.jsonl",
        {"event": "tool_call", "sequence": 1},
    )

    assert read_today_audit(tmp_path, date=day, limit=0) == []
    assert read_today_audit(tmp_path, date=day, limit=-1) == []
    assert read_today_audit(tmp_path / "missing", date=day) == []


def test_read_today_audit_ignores_log_filename_lookalikes(tmp_path: Path) -> None:
    day = "2026-07-06"
    for name in (
        f"kira-{day}.0.jsonl.gz",
        f"kira-{day}.1.jsonl",
        f"kira-{day}.jsonl.gz",
        "kira-2026-02-30.jsonl",
    ):
        _write_jsonl(
            tmp_path / name,
            {"event": "tool_call", "sequence": 999},
            compressed=name.endswith(".gz"),
        )
    _write_jsonl(
        tmp_path / f"kira-{day}.jsonl",
        {"event": "tool_call", "sequence": 1},
    )

    assert [event["sequence"] for event in read_today_audit(tmp_path, date=day)] == [1]
