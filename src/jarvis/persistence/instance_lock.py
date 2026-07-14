"""Cross-process ownership lock for one Kairo data root.

SQLite's WAL lock protects transactions, not the process lifecycle: an idle workstation can have
no active database lock while its scheduler, Telegram ingress, or connector workers are alive.
The CLI therefore holds this OS-level lock for the entire runtime. Offline maintenance commands
must acquire the same lock before moving or replacing any durable root.
"""

from __future__ import annotations

import os
from pathlib import Path
from types import TracebackType
from typing import BinaryIO


class InstanceAlreadyRunning(RuntimeError):
    """Another Kairo process owns the configured data root."""


def instance_lock_path(data_dir: Path) -> Path:
    """Return a stable lock beside, never inside, the movable data directory."""
    resolved = data_dir.resolve()
    return resolved.with_name(f".{resolved.name}.kairo-instance.lock")


class InstanceLock:
    """Non-blocking, cross-platform exclusive file lock."""

    def __init__(self, data_dir: Path) -> None:
        self.path = instance_lock_path(data_dir)
        self._handle: BinaryIO | None = None

    def acquire(self) -> InstanceLock:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b")
        try:
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
        except (OSError, BlockingIOError) as exc:
            handle.close()
            raise InstanceAlreadyRunning(
                "Kairo is already running for this data directory. Stop it before maintenance."
            ) from exc
        self._handle = handle
        return self

    def release(self) -> None:
        handle = self._handle
        if handle is None:
            return
        self._handle = None
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

    def __enter__(self) -> InstanceLock:
        return self.acquire()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.release()
