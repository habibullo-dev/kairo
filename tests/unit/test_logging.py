"""Logging tests: JSON lines land on disk and carry the turn's trace id."""

from __future__ import annotations

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


def test_module_reexports_match() -> None:
    # __init__ re-exports the same callables as the submodule.
    assert obs_logging.get_logger is get_logger
