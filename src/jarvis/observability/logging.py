"""Structured logging + per-turn trace correlation.

Every model call, tool call, permission decision, and error is emitted as one
JSON object per line to ``logs/jarvis-YYYY-MM-DD.jsonl`` — a machine-parseable
audit trail. User-facing rendering is the REPL's job (task 8); this module is the
record of what actually happened, not the UI.

A ``trace_id`` contextvar ties every event within a single user turn together.
Bind it once at the top of a turn (``bind_trace()``) and every subsequent log
line carries it automatically.
"""

from __future__ import annotations

import contextlib
import contextvars
import datetime as _dt
import logging
import uuid
from pathlib import Path
from typing import IO

import structlog

_LEVELS: dict[str, int] = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warning": logging.WARNING,
    "error": logging.ERROR,
}

_trace_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "jarvis_trace_id", default=None
)

# Held open for the process lifetime; closed/replaced on reconfigure (tests).
_log_file: IO[str] | None = None


def new_trace_id() -> str:
    """A short random id for correlating one turn's events."""
    return uuid.uuid4().hex[:16]


def bind_trace(trace_id: str | None = None) -> str:
    """Set the current turn's trace id (generating one if not given). Returns it."""
    tid = trace_id or new_trace_id()
    _trace_id_var.set(tid)
    return tid


def get_trace_id() -> str | None:
    return _trace_id_var.get()


def clear_trace() -> None:
    _trace_id_var.set(None)


def _add_trace_id(_logger: object, _method: str, event_dict: dict) -> dict:
    tid = _trace_id_var.get()
    if tid is not None:
        event_dict.setdefault("trace_id", tid)
    return event_dict


def configure_logging(
    logs_dir: Path,
    *,
    level: str = "info",
    date: str | None = None,
) -> Path:
    """Route structlog to a dated JSON-lines file under ``logs_dir``.

    Idempotent: re-calling closes the previous file and rebinds (used by tests to
    point at a temp dir). Returns the log file path.
    """
    global _log_file

    logs_dir.mkdir(parents=True, exist_ok=True)
    day = date or _dt.datetime.now().strftime("%Y-%m-%d")
    path = logs_dir / f"jarvis-{day}.jsonl"

    if _log_file is not None:
        with contextlib.suppress(OSError):
            _log_file.close()
    _log_file = path.open("a", encoding="utf-8")

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            _add_trace_id,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.dict_tracebacks,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(_LEVELS[level.lower()]),
        logger_factory=structlog.PrintLoggerFactory(file=_log_file),
        cache_logger_on_first_use=False,
    )
    return path


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """Get a structlog logger. Safe before ``configure_logging`` (defaults to stdout)."""
    return structlog.get_logger(name)
