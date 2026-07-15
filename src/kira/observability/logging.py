"""Structured logging + per-turn trace correlation.

Every model call, tool call, permission decision, and error is emitted as one
JSON object per line to ``logs/kira-YYYY-MM-DD.jsonl`` — a machine-parseable
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
import gzip
import logging
import re
import shutil
import threading
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import IO, Any

import structlog

_LEVELS: dict[str, int] = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warning": logging.WARNING,
    "error": logging.ERROR,
}

_trace_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "kira_trace_id", default=None
)

_SENSITIVE_KEY_PARTS = frozenset(
    {
        "token",
        "secret",
        "password",
        "credential",
        "authorization",
        "cookie",
        "apikey",
        # structlog's serialized exception frames can include arbitrary local variables.
        "locals",
    }
)
CANONICAL_LOG_PREFIX = "kira"
LEGACY_LOG_PREFIXES = ("jarvis",)
READABLE_LOG_PREFIXES = (*LEGACY_LOG_PREFIXES, CANONICAL_LOG_PREFIX)
_LOG_PREFIX_PATTERN = "|".join(re.escape(prefix) for prefix in READABLE_LOG_PREFIXES)
_LOG_FILE_PATTERN = re.compile(
    rf"(?P<prefix>{_LOG_PREFIX_PATTERN})-"
    rf"(?P<day>[0-9]{{4}}-[0-9]{{2}}-[0-9]{{2}})"
    rf"(?:\.jsonl|\.(?P<index>[1-9][0-9]*)\.jsonl\.gz)\Z"
)
_INLINE_SECRET_PATTERN = re.compile(
    r"(?ix)"
    r"(?P<label>\b(?:api[_-]?key|token|secret|password|credential|authorization|cookie)\b\s*[:=]\s*)"
    r"(?P<value>[^\s,;]+)"
    r"|(?P<bearer>\bbearer\s+)(?P<bearer_value>[A-Za-z0-9._~+/=-]+)"
    r"|(?P<provider>\bsk-[A-Za-z0-9_-]+)"
)


def _is_sensitive_key(key: object) -> bool:
    normalized = re.sub(r"[^a-z0-9]", "", str(key).lower())
    return any(part in normalized for part in _SENSITIVE_KEY_PARTS)


def _redact_inline_secret(value: str) -> str:
    def replace(match: re.Match[str]) -> str:
        if match.group("label"):
            return f"{match.group('label')}[REDACTED]"
        if match.group("bearer"):
            return f"{match.group('bearer')}[REDACTED]"
        return "[REDACTED]"

    return _INLINE_SECRET_PATTERN.sub(replace, value)


def _redact_value(value: Any, *, key: object | None = None) -> Any:
    if key is not None and _is_sensitive_key(key):
        return "[REDACTED]"
    if isinstance(value, Mapping):
        return {
            str(item_key): _redact_value(item, key=item_key) for item_key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_redact_value(item) for item in value]
    if isinstance(value, str):
        return _redact_inline_secret(value)
    return value


def _tool_input_shape(value: object) -> dict[str, object]:
    """Preserve a minimal, non-content audit shape for a raw tool input."""
    if isinstance(value, Mapping):
        keys = sorted(str(key) for key in value)[:32]
        return {"redacted": True, "keys": keys, "key_count": len(value)}
    return {"redacted": True, "type": type(value).__name__}


def _redact_sensitive_fields(_logger: object, _method: str, event_dict: dict) -> dict:
    """Remove raw secrets and tool arguments before the event reaches the JSON renderer."""
    redacted = _redact_value(event_dict)
    if event_dict.get("event") == "tool_call" and "input" in event_dict:
        redacted["input"] = _tool_input_shape(event_dict["input"])
    return redacted


def parse_log_filename(name: str) -> tuple[str, str, int | None] | None:
    """Parse an exact canonical or legacy structured-log filename."""
    match = _LOG_FILE_PATTERN.fullmatch(name)
    if match is None:
        return None
    day = match.group("day")
    try:
        _dt.date.fromisoformat(day)
    except ValueError:
        return None
    raw_index = match.group("index")
    return match.group("prefix"), day, int(raw_index) if raw_index is not None else None


class _RotatingJsonlSink:
    """Write complete JSON records, rotating before a record can exceed the active segment."""

    def __init__(
        self,
        logs_dir: Path,
        *,
        max_bytes: int,
        backup_count: int,
        retention_days: int,
        date: str | None,
    ) -> None:
        self.logs_dir = logs_dir
        self.max_bytes = max_bytes
        self.backup_count = backup_count
        self.retention_days = retention_days
        self._fixed_day = date
        self._lock = threading.RLock()
        self._file: IO[str] | None = None
        self._day = self._current_day()
        self.path = self._path_for(self._day)
        self._size = 0
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self._prune_expired()
        self._open_active()

    def _current_day(self) -> str:
        return self._fixed_day or _dt.datetime.now().strftime("%Y-%m-%d")

    def _path_for(self, day: str) -> Path:
        return self.logs_dir / f"{CANONICAL_LOG_PREFIX}-{day}.jsonl"

    def _archive_path(self, index: int) -> Path:
        return self.logs_dir / f"{CANONICAL_LOG_PREFIX}-{self._day}.{index}.jsonl.gz"

    def _open_active(self) -> None:
        self.path = self._path_for(self._day)
        self._file = self.path.open("a", encoding="utf-8", newline="\n")
        self._size = self.path.stat().st_size

    def close(self) -> None:
        if self._file is not None:
            with contextlib.suppress(OSError):
                self._file.close()
            self._file = None

    def _prune_expired(self) -> None:
        current = _dt.date.fromisoformat(self._day)
        cutoff = current - _dt.timedelta(days=self.retention_days - 1)
        for candidate in self.logs_dir.iterdir():
            parsed = parse_log_filename(candidate.name)
            if parsed is None or not candidate.is_file():
                continue
            _prefix, day, _index = parsed
            log_day = _dt.date.fromisoformat(day)
            if log_day < cutoff:
                with contextlib.suppress(OSError):
                    candidate.unlink()

    def _rotate(self) -> None:
        self.close()
        archive = self._archive_path(1)
        temporary = archive.with_name(f".{archive.name}.tmp")
        live_removed = False
        try:
            # First create a complete staging copy and prove the live segment can be retired.
            # A sharing violation (common on Windows) must leave the archive ladder untouched.
            with self.path.open("rb") as source, gzip.open(temporary, "wb") as destination:
                shutil.copyfileobj(source, destination)
            self.path.unlink()
            live_removed = True

            for index in range(self.backup_count - 1, 0, -1):
                previous = self._archive_path(index)
                if previous.exists():
                    previous.replace(self._archive_path(index + 1))
            temporary.replace(archive)
        except OSError:
            # Rotation is hygiene, never a reason to turn a logging call into an application
            # failure. Reopen the live JSONL and retry rotation on a future record. If retiring
            # the live file succeeded but an archive mutation failed, restore the staged record.
            if live_removed and temporary.exists():
                with (
                    contextlib.suppress(OSError),
                    gzip.open(temporary, "rb") as source,
                    self.path.open("wb") as destination,
                ):
                    shutil.copyfileobj(source, destination)
            pass
        finally:
            with contextlib.suppress(FileNotFoundError):
                temporary.unlink()
            if self._file is None:
                self._open_active()

    def write_record(self, message: str) -> None:
        record = f"{message}\n"
        encoded = record.encode("utf-8")
        with self._lock:
            current_day = self._current_day()
            if current_day != self._day:
                self.close()
                self._day = current_day
                self._prune_expired()
                self._open_active()
            if self._size and self._size + len(encoded) > self.max_bytes:
                self._rotate()
            if self._file is None:
                self._open_active()
            if self._file is None:
                raise OSError(f"Unable to reopen active JSONL log file {self.path}")
            self._file.write(record)
            self._file.flush()
            self._size += len(encoded)


class _RotatingJsonlLogger:
    """Structlog's tiny logger protocol, with one atomic write per rendered JSON event."""

    def __init__(self, sink: _RotatingJsonlSink) -> None:
        self._sink = sink

    def msg(self, message: str) -> None:
        self._sink.write_record(message)

    log = debug = info = warn = warning = msg
    fatal = failure = err = error = critical = exception = msg


class _RotatingJsonlLoggerFactory:
    def __init__(self, sink: _RotatingJsonlSink) -> None:
        self._sink = sink

    def __call__(self, *_args: object) -> _RotatingJsonlLogger:
        return _RotatingJsonlLogger(self._sink)


# Held open for the process lifetime; closed/replaced on reconfigure (tests).
_log_sink: _RotatingJsonlSink | None = None


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
    max_bytes: int = 10 * 1024 * 1024,
    backup_count: int = 3,
    retention_days: int = 30,
) -> Path:
    """Route structlog to a dated JSON-lines file under ``logs_dir``.

    Idempotent: re-calling closes the previous file and rebinds (used by tests to
    point at a temp dir). Returns the log file path.
    """
    if max_bytes <= 0 or backup_count <= 0 or retention_days <= 0:
        raise ValueError("max_bytes, backup_count, and retention_days must all be positive")

    global _log_sink

    if _log_sink is not None:
        _log_sink.close()
    _log_sink = _RotatingJsonlSink(
        logs_dir,
        max_bytes=max_bytes,
        backup_count=backup_count,
        retention_days=retention_days,
        date=date,
    )

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            _add_trace_id,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.dict_tracebacks,
            _redact_sensitive_fields,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(_LEVELS[level.lower()]),
        logger_factory=_RotatingJsonlLoggerFactory(_log_sink),
        cache_logger_on_first_use=False,
    )
    return _log_sink.path


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """Get a structlog logger. Safe before ``configure_logging`` (defaults to stdout)."""
    return structlog.get_logger(name)
