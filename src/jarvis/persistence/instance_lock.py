"""Cross-process ownership locks for one Kira data root.

SQLite's WAL lock protects transactions, not the process lifecycle: an idle workstation can have
no active database lock while its scheduler, Telegram ingress, or connector workers are alive.
The CLI therefore holds this OS-level lock for the entire runtime. Offline maintenance commands
must acquire the same locks before moving or replacing any durable root.

Kira acquires the legacy Kairo lock first and the canonical Kira lock second. Holding both keeps
old and new executables mutually exclusive throughout the rename; the legacy lock cannot be
removed until every supported executable understands the canonical path.
"""

from __future__ import annotations

import contextlib
import os
from pathlib import Path
from types import TracebackType
from typing import BinaryIO


class InstanceAlreadyRunning(RuntimeError):
    """Another Kira or legacy-compatible process owns the configured data root."""


def legacy_instance_lock_path(data_dir: Path) -> Path:
    """Return the exact pre-Kira lock path used by already-running older processes."""
    resolved = data_dir.resolve()
    return resolved.with_name(f".{resolved.name}.kairo-instance.lock")


def instance_lock_path(data_dir: Path) -> Path:
    """Return the canonical lock beside, never inside, the movable data directory."""
    resolved = data_dir.resolve()
    return resolved.with_name(f".{resolved.name}.kira-instance.lock")


def instance_lock_paths(data_dir: Path) -> tuple[Path, Path]:
    """Return locks in the compatibility-safe acquisition order: legacy, then canonical."""
    resolved = data_dir.resolve()
    return (
        resolved.with_name(f".{resolved.name}.kairo-instance.lock"),
        resolved.with_name(f".{resolved.name}.kira-instance.lock"),
    )


def _acquire_handle(path: Path) -> BinaryIO:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle: BinaryIO | None = None
    try:
        handle = path.open("a+b")
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BaseException:
        if handle is not None:
            with contextlib.suppress(BaseException):
                handle.close()
        raise
    assert handle is not None
    return handle


def _release_handle(handle: BinaryIO) -> None:
    try:
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


class InstanceLock:
    """Non-blocking, cross-platform dual lock for Kira and legacy processes."""

    def __init__(self, data_dir: Path) -> None:
        self.paths = instance_lock_paths(data_dir)
        self.path = self.paths[1]
        self._handles: tuple[BinaryIO, ...] = ()

    def acquire(self) -> InstanceLock:
        if self._handles:
            raise RuntimeError("InstanceLock is already acquired")
        acquired: list[BinaryIO] = []
        try:
            for path in self.paths:
                acquired.append(_acquire_handle(path))
        except BaseException as exc:
            for handle in reversed(acquired):
                with contextlib.suppress(BaseException):
                    _release_handle(handle)
            if isinstance(exc, OSError):
                raise InstanceAlreadyRunning(
                    "Kira may already be running for this data directory, or its instance lock "
                    "is unavailable. Stop it before maintenance and verify directory access."
                ) from exc
            raise
        self._handles = tuple(acquired)
        return self

    def release(self) -> None:
        handles = self._handles
        if not handles:
            return
        self._handles = ()
        first_error: BaseException | None = None
        for handle in reversed(handles):
            try:
                _release_handle(handle)
            except BaseException as exc:
                first_error = first_error or exc
        if first_error is not None:
            raise first_error

    def __enter__(self) -> InstanceLock:
        return self.acquire()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.release()
