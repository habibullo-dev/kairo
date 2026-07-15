"""Small cross-platform primitives for durable, no-clobber filesystem transitions."""

from __future__ import annotations

import ctypes
import errno
import os
import sys
import uuid
from contextlib import suppress
from pathlib import Path

_MOVEFILE_REPLACE_EXISTING = 0x00000001
_MOVEFILE_WRITE_THROUGH = 0x00000008


def _windows_move(source: Path, destination: Path, *, replace: bool) -> None:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    move = kernel32.MoveFileExW
    move.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint]
    move.restype = ctypes.c_int
    flags = _MOVEFILE_WRITE_THROUGH
    if replace:
        flags |= _MOVEFILE_REPLACE_EXISTING
    if not move(str(source), str(destination), flags):
        error = ctypes.get_last_error()
        exception = ctypes.WinError(error)
        exception.filename = str(destination)
        raise exception


def sync_directory(path: Path) -> None:
    """Persist directory-entry changes where the platform exposes directory ``fsync``."""
    if os.name == "nt":
        return
    descriptor: int | None = None
    try:
        descriptor = os.open(path, os.O_RDONLY)
        os.fsync(descriptor)
    finally:
        if descriptor is not None:
            os.close(descriptor)


def rename_no_replace(source: Path, destination: Path) -> None:
    """Atomically move ``source`` without ever replacing ``destination``."""
    if os.name == "nt":
        _windows_move(source, destination, replace=False)
        return

    libc = ctypes.CDLL(None, use_errno=True)
    encoded_source = os.fsencode(source)
    encoded_destination = os.fsencode(destination)
    if sys.platform.startswith("linux"):
        rename = getattr(libc, "renameat2", None)
        if rename is None:
            raise OSError(
                errno.ENOTSUP,
                "atomic no-replace rename is unavailable",
                str(destination),
            )
        rename.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        rename.restype = ctypes.c_int
        result = rename(-100, encoded_source, -100, encoded_destination, 1)
    elif sys.platform == "darwin":
        rename = getattr(libc, "renamex_np", None)
        if rename is None:
            raise OSError(
                errno.ENOTSUP,
                "atomic no-replace rename is unavailable",
                str(destination),
            )
        rename.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        rename.restype = ctypes.c_int
        result = rename(encoded_source, encoded_destination, 0x00000004)
    else:
        raise OSError(
            errno.ENOTSUP,
            "atomic no-replace rename is unavailable",
            str(destination),
        )
    if result != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), str(destination))


def durable_rename_no_replace(source: Path, destination: Path) -> None:
    """No-clobber rename followed by persistence of both affected parent directories."""
    rename_no_replace(source, destination)
    sync_directory(source.parent)
    if destination.parent != source.parent:
        sync_directory(destination.parent)


def durable_replace(source: Path, destination: Path) -> None:
    """Atomically replace one entry and make the directory transition durable."""
    if os.name == "nt":
        _windows_move(source, destination, replace=True)
        return
    os.replace(source, destination)
    sync_directory(destination.parent)


def durable_mkdir(path: Path, *, mode: int = 0o777) -> None:
    """Create a missing directory tree through durable, no-clobber publications."""
    target = Path(os.path.abspath(path))
    if os.path.lexists(target):
        raise FileExistsError(errno.EEXIST, "directory already exists", str(target))

    missing: list[Path] = []
    cursor = target
    while not os.path.lexists(cursor):
        missing.append(cursor)
        if cursor == cursor.parent:
            raise OSError(errno.ENOENT, "directory has no existing ancestor", str(target))
        cursor = cursor.parent
    if not cursor.is_dir() or cursor.is_symlink():
        raise NotADirectoryError(errno.ENOTDIR, "parent is not a local directory", str(cursor))

    for candidate in reversed(missing):
        temporary = candidate.parent / f".{candidate.name}.kira-mkdir-{uuid.uuid4().hex}"
        os.mkdir(temporary, mode=mode)
        try:
            durable_rename_no_replace(temporary, candidate)
        except BaseException:
            with suppress(OSError):
                os.rmdir(temporary)
            raise
