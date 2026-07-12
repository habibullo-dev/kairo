"""Logging tests: JSON lines land on disk and carry the turn's trace id."""

from __future__ import annotations

import gzip
import json
from pathlib import Path

import pytest

from jarvis.observability import (
    bind_trace,
    clear_trace,
    configure_logging,
    get_logger,
    get_trace_id,
)
from jarvis.observability import logging as obs_logging


@pytest.fixture(autouse=True)
def _reset_trace() -> None:
    clear_trace()
    yield
    clear_trace()


def _read_events(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _read_gzip_events(path: Path) -> list[dict]:
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def test_configure_returns_dated_path(tmp_path: Path) -> None:
    path = configure_logging(tmp_path / "logs", date="2026-07-06")
    assert path.name == "jarvis-2026-07-06.jsonl"
    assert path.parent.is_dir()


def test_event_is_json_with_standard_fields(tmp_path: Path) -> None:
    path = configure_logging(tmp_path / "logs", date="2026-07-06")
    get_logger("test").info("tool_call", tool="read_file", ok=True)

    events = _read_events(path)
    assert len(events) == 1
    ev = events[0]
    assert ev["event"] == "tool_call"
    assert ev["tool"] == "read_file"
    assert ev["ok"] is True
    assert ev["level"] == "info"
    assert "timestamp" in ev


def test_trace_id_bound_and_cleared(tmp_path: Path) -> None:
    path = configure_logging(tmp_path / "logs", date="2026-07-06")
    log = get_logger("test")

    tid = bind_trace()
    assert get_trace_id() == tid
    log.info("turn_start")

    clear_trace()
    log.info("after_clear")

    events = _read_events(path)
    assert events[0]["trace_id"] == tid
    assert "trace_id" not in events[1]


def test_explicit_trace_id_is_used(tmp_path: Path) -> None:
    configure_logging(tmp_path / "logs", date="2026-07-06")
    returned = bind_trace("abc123")
    assert returned == "abc123"
    assert get_trace_id() == "abc123"


def test_level_filtering(tmp_path: Path) -> None:
    path = configure_logging(tmp_path / "logs", level="warning", date="2026-07-06")
    log = get_logger("test")
    log.info("filtered_out")
    log.warning("kept")

    events = _read_events(path)
    assert [e["event"] for e in events] == ["kept"]


def test_reconfigure_switches_file(tmp_path: Path) -> None:
    first = configure_logging(tmp_path / "a", date="2026-07-06")
    get_logger().info("one")
    second = configure_logging(tmp_path / "b", date="2026-07-06")
    get_logger().info("two")

    assert [e["event"] for e in _read_events(first)] == ["one"]
    assert [e["event"] for e in _read_events(second)] == ["two"]


def test_rotation_keeps_complete_json_lines_and_caps_compressed_backups(tmp_path: Path) -> None:
    path = configure_logging(
        tmp_path / "logs",
        date="2026-07-06",
        max_bytes=320,
        backup_count=2,
    )
    for sequence in range(4):
        get_logger("test").info("audit", sequence=sequence, payload="x" * 256)

    oldest_surviving = path.with_name("jarvis-2026-07-06.2.jsonl.gz")
    newer_archive = path.with_name("jarvis-2026-07-06.1.jsonl.gz")
    assert oldest_surviving.is_file()
    assert newer_archive.is_file()
    assert not path.with_name("jarvis-2026-07-06.3.jsonl.gz").exists()
    events = (
        _read_gzip_events(oldest_surviving) + _read_gzip_events(newer_archive) + _read_events(path)
    )
    assert [event["sequence"] for event in events] == [1, 2, 3]


def test_rotation_failure_reopens_the_live_log_and_keeps_writing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = configure_logging(tmp_path / "logs", date="2026-07-06", max_bytes=320)
    get_logger("test").info("before_lock", payload="x" * 256)
    prior_archive = path.with_name("jarvis-2026-07-06.1.jsonl.gz")
    with gzip.open(prior_archive, "wt", encoding="utf-8") as handle:
        handle.write('{"event":"preserved_archive"}\n')
    original_unlink = Path.unlink

    def refuse_live_log_delete(candidate: Path, *args: object, **kwargs: object) -> None:
        if candidate == path:
            raise PermissionError("simulated live-file lock")
        original_unlink(candidate, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", refuse_live_log_delete)
    get_logger("test").info("after_lock", payload="x" * 256)

    assert [event["event"] for event in _read_events(path)] == ["before_lock", "after_lock"]
    assert [event["event"] for event in _read_gzip_events(prior_archive)] == ["preserved_archive"]


def test_date_rollover_reopens_a_new_active_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = configure_logging(tmp_path / "logs", date="2026-07-06")
    sink = obs_logging._log_sink
    assert sink is not None
    monkeypatch.setattr(sink, "_current_day", lambda: "2026-07-07")

    get_logger("test").info("next_day")

    second = first.with_name("jarvis-2026-07-07.jsonl")
    assert second.is_file()
    assert [event["event"] for event in _read_events(second)] == ["next_day"]


def test_retention_removes_only_expired_recognized_jarvis_logs(tmp_path: Path) -> None:
    logs = tmp_path / "logs"
    logs.mkdir()
    expired = logs / "jarvis-2026-07-07.jsonl"
    expired.write_text('{"event":"old"}\n', encoding="utf-8")
    expired_compressed = logs / "jarvis-2026-07-08.1.jsonl.gz"
    with gzip.open(expired_compressed, "wt", encoding="utf-8") as handle:
        handle.write('{"event":"old"}\n')
    retained = logs / "jarvis-2026-07-09.jsonl"
    retained.write_text('{"event":"recent"}\n', encoding="utf-8")
    unrelated = logs / "keep-me.txt"
    unrelated.write_text("not a Jarvis log", encoding="utf-8")

    configure_logging(logs, date="2026-07-10", retention_days=2)

    assert not expired.exists()
    assert not expired_compressed.exists()
    assert retained.exists()
    assert unrelated.exists()


def test_tool_inputs_and_secrets_are_redacted_from_production_jsonl(tmp_path: Path) -> None:
    path = configure_logging(tmp_path / "logs", date="2026-07-06")
    canary = "CANARY-PRIVATE-SECRET"
    get_logger("test").error(
        "tool_call",
        tool="run_shell",
        input={"command": f"echo {canary}", "password": canary},
        authorization=f"Bearer {canary}",
        error=f"token={canary}",
        exception=f"Bearer {canary}",
    )
    try:
        raise RuntimeError(f"Bearer {canary}")
    except RuntimeError:
        get_logger("test").exception("failed tool")

    raw = path.read_text(encoding="utf-8")
    assert canary not in raw
    event = _read_events(path)[0]
    assert event["input"] == {"redacted": True, "keys": ["command", "password"], "key_count": 2}
    assert event["authorization"] == "[REDACTED]"
    assert event["error"] == "token=[REDACTED]"
    assert event["exception"] == "Bearer [REDACTED]"


def test_module_reexports_match() -> None:
    # __init__ re-exports the same callables as the submodule.
    assert obs_logging.get_logger is get_logger
