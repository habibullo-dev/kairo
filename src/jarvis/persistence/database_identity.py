"""Canonical Kira database selection and lock-required legacy filename migration."""

from __future__ import annotations

import contextlib
import ctypes
import errno
import os
import sqlite3
import stat
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path

from jarvis.persistence.instance_lock import InstanceLock

DATABASE_FILENAME = "kira.db"
LEGACY_DATABASE_FILENAME = "jarvis.db"
_SIDECAR_SUFFIXES = ("-wal", "-shm", "-journal")
_TOMBSTONE_STAGING = ".kira-legacy-database-tombstone"
_TOMBSTONE_PAYLOAD = b"KIRA_DATABASE_MOVED_TO_kira.db\n"
_PENDING_GUARD_STAGING = ".kira-fresh-database-guard"
_PENDING_GUARD_PAYLOAD = b"KIRA_DATABASE_INITIALIZING_kira.db\n"
_CUTOVER_PARKED = ".kira-legacy-database-parked"


class DatabaseIdentityError(RuntimeError):
    """The live database identity is ambiguous or cannot be migrated safely."""


@dataclass(frozen=True)
class _FileIdentity:
    device: int
    inode: int


def _is_link_like(path: Path) -> bool:
    junction = getattr(path, "is_junction", None)
    return path.is_symlink() or bool(junction and junction())


def _lstat(path: Path, *, label: str) -> os.stat_result | None:
    try:
        return path.stat(follow_symlinks=False)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise DatabaseIdentityError(f"{label} is unavailable: {path.name}") from exc


def _identity(info: os.stat_result) -> _FileIdentity:
    return _FileIdentity(device=int(info.st_dev), inode=int(info.st_ino))


def _regular_identity(
    path: Path,
    *,
    label: str,
    allowed_links: frozenset[int] = frozenset({1}),
) -> _FileIdentity | None:
    info = _lstat(path, label=label)
    if info is None:
        return None
    if _is_link_like(path) or not stat.S_ISREG(info.st_mode):
        raise DatabaseIdentityError(f"{label} is not a regular local file: {path.name}")
    if int(info.st_nlink) not in allowed_links:
        raise DatabaseIdentityError(f"{label} has unexpected filesystem links: {path.name}")
    return _identity(info)


def _assert_identity(
    path: Path,
    expected: _FileIdentity,
    *,
    label: str,
    allowed_links: frozenset[int],
) -> None:
    current = _regular_identity(path, label=label, allowed_links=allowed_links)
    if current != expected:
        raise DatabaseIdentityError(f"{label} changed during migration: {path.name}")


def _same_identity(first: _FileIdentity | None, second: _FileIdentity | None) -> bool:
    return first is not None and first == second


def _sidecar_paths(main: Path) -> tuple[Path, ...]:
    return tuple(main.with_name(f"{main.name}{suffix}") for suffix in _SIDECAR_SUFFIXES)


def _validate_sidecars(main: Path, *, main_exists: bool) -> None:
    for sidecar in _sidecar_paths(main):
        identity = _regular_identity(sidecar, label="Database sidecar")
        if identity is None:
            continue
        if not main_exists:
            raise DatabaseIdentityError(
                f"Orphan database sidecar requires operator recovery: {sidecar.name}"
            )


def _marker_identity(
    path: Path,
    payload: bytes,
    *,
    label: str,
    allowed_links: frozenset[int] = frozenset({1}),
) -> _FileIdentity | None:
    info = _lstat(path, label=label)
    if info is None:
        return None
    if _is_link_like(path) or not stat.S_ISREG(info.st_mode):
        return None
    if int(info.st_size) != len(payload):
        return None
    if int(info.st_nlink) not in allowed_links:
        raise DatabaseIdentityError(f"{label} has unexpected links.")
    descriptor: int | None = None
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        opened = os.fstat(descriptor)
        if (
            _identity(opened) != _identity(info)
            or not stat.S_ISREG(opened.st_mode)
            or int(opened.st_size) != len(payload)
            or int(opened.st_nlink) not in allowed_links
        ):
            raise DatabaseIdentityError(f"{label} changed while it was inspected.")
        actual = os.read(descriptor, len(payload) + 1)
    except DatabaseIdentityError:
        raise
    except OSError as exc:
        raise DatabaseIdentityError(f"{label} is unreadable.") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
    if actual != payload:
        return None
    return _identity(info)


def _tombstone_identity(
    path: Path, *, allowed_links: frozenset[int] = frozenset({1})
) -> _FileIdentity | None:
    return _marker_identity(
        path,
        _TOMBSTONE_PAYLOAD,
        label="Legacy database compatibility guard",
        allowed_links=allowed_links,
    )


def _pending_guard_identity(
    path: Path, *, allowed_links: frozenset[int] = frozenset({1})
) -> _FileIdentity | None:
    return _marker_identity(
        path,
        _PENDING_GUARD_PAYLOAD,
        label="Fresh database compatibility guard",
        allowed_links=allowed_links,
    )


def _identity_paths(data_dir: Path) -> tuple[Path, Path]:
    root = data_dir.resolve()
    return root / DATABASE_FILENAME, root / LEGACY_DATABASE_FILENAME


def select_database(data_dir: Path) -> Path:
    """Select one existing identity without creating, opening, or migrating it."""
    canonical, legacy = _identity_paths(data_dir)
    parked = data_dir.resolve() / _CUTOVER_PARKED
    if _lstat(parked, label="Database cutover recovery file") is not None:
        raise DatabaseIdentityError(
            "Kira database cutover was interrupted; start Kira to recover it."
        )
    canonical_identity = _regular_identity(canonical, label="Database identity")
    tombstone = _tombstone_identity(legacy)
    pending_guard = None if tombstone is not None else _pending_guard_identity(legacy)
    legacy_identity = (
        None
        if tombstone is not None or pending_guard is not None
        else _regular_identity(legacy, label="Database identity")
    )
    _validate_sidecars(canonical, main_exists=canonical_identity is not None)
    _validate_sidecars(legacy, main_exists=legacy_identity is not None)

    if tombstone is not None:
        if canonical_identity is None:
            raise DatabaseIdentityError(
                "The legacy compatibility guard exists but the Kira database is missing."
            )
        return canonical
    if pending_guard is not None:
        raise DatabaseIdentityError(
            "Fresh Kira database initialization was interrupted; start Kira to recover it."
        )
    if canonical_identity is not None and legacy_identity is not None:
        raise DatabaseIdentityError(
            "Both Kira and legacy databases exist; startup refused to choose between them."
        )
    if canonical_identity is not None:
        return canonical
    if legacy_identity is not None:
        return legacy
    return canonical


def _sqlite_uri(path: Path, *, mode: str) -> str:
    return f"{path.resolve().as_uri()}?mode={mode}"


def _prepare_single_file_database(database: Path, expected: _FileIdentity) -> None:
    _assert_identity(
        database,
        expected,
        label="Legacy database",
        allowed_links=frozenset({1, 2}),
    )
    try:
        db = sqlite3.connect(_sqlite_uri(database, mode="rw"), uri=True, timeout=5)
        try:
            _assert_identity(
                database,
                expected,
                label="Legacy database",
                allowed_links=frozenset({1, 2}),
            )
            db.execute("PRAGMA busy_timeout = 5000")
            integrity = [str(row[0]) for row in db.execute("PRAGMA integrity_check").fetchall()]
            if integrity != ["ok"]:
                raise DatabaseIdentityError("Legacy database integrity verification failed.")
            mode_row = db.execute("PRAGMA journal_mode").fetchone()
            mode = str(mode_row[0]).lower() if mode_row else ""
            if mode == "wal":
                checkpoint = db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
                if checkpoint is None or int(checkpoint[0]) != 0:
                    raise DatabaseIdentityError(
                        "Legacy database WAL is busy; stop every database client and retry."
                    )
            delete_row = db.execute("PRAGMA journal_mode = DELETE").fetchone()
            if delete_row is None or str(delete_row[0]).lower() != "delete":
                raise DatabaseIdentityError(
                    "Legacy database could not enter single-file journal mode."
                )
        finally:
            db.close()
    except DatabaseIdentityError:
        raise
    except (OSError, sqlite3.Error, TypeError, ValueError) as exc:
        raise DatabaseIdentityError("Legacy database could not be prepared safely.") from exc
    _assert_identity(
        database,
        expected,
        label="Legacy database",
        allowed_links=frozenset({1, 2}),
    )


def _remove_transient_sidecars(database: Path) -> None:
    wal = database.with_name(f"{database.name}-wal")
    journal = database.with_name(f"{database.name}-journal")
    try:
        for durable_candidate in (wal, journal):
            info = _lstat(durable_candidate, label="Database sidecar")
            if info is not None and int(info.st_size) != 0:
                raise DatabaseIdentityError(
                    f"Database still has unmerged recovery data: {durable_candidate.name}"
                )
        for sidecar in _sidecar_paths(database):
            identity = _regular_identity(sidecar, label="Database sidecar")
            if identity is not None:
                sidecar.unlink()
    except DatabaseIdentityError:
        raise
    except OSError as exc:
        raise DatabaseIdentityError(
            "Legacy database sidecars could not be cleared safely."
        ) from exc


def _sync_file(path: Path, expected: _FileIdentity) -> None:
    descriptor: int | None = None
    flags = os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
        if _identity(os.fstat(descriptor)) != expected:
            raise DatabaseIdentityError("Legacy database changed before it could be synced.")
        os.fsync(descriptor)
    except DatabaseIdentityError:
        raise
    except OSError as exc:
        raise DatabaseIdentityError("Legacy database could not be synced safely.") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _sync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor: int | None = None
    try:
        descriptor = os.open(path, os.O_RDONLY)
        os.fsync(descriptor)
    except OSError as exc:
        raise DatabaseIdentityError("Database directory could not be synced safely.") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _rename_no_replace(source: Path, destination: Path) -> None:
    """Atomically move ``source`` without ever overwriting ``destination``."""
    if os.name == "nt":
        os.rename(source, destination)
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


def _write_marker_staging(data_dir: Path, name: str, payload: bytes) -> Path:
    staging = data_dir / name
    existing = _marker_identity(
        staging,
        payload,
        label="Database compatibility staging",
        allowed_links=frozenset({1, 2}),
    )
    if existing is not None:
        prefix = f"{name}.tmp-"
        for candidate in data_dir.glob(f"{prefix}*"):
            suffix = candidate.name.removeprefix(prefix)
            if len(suffix) != 32 or any(
                character not in "0123456789abcdef" for character in suffix
            ):
                continue
            candidate_identity = _marker_identity(
                candidate,
                payload,
                label="Database compatibility temporary marker",
                allowed_links=frozenset({1, 2}),
            )
            if candidate_identity == existing:
                try:
                    candidate.unlink()
                except OSError as exc:
                    raise DatabaseIdentityError(
                        "Database compatibility temporary marker could not be cleared."
                    ) from exc
                _sync_directory(data_dir)
        return staging
    if _lstat(staging, label="Database compatibility staging") is not None:
        raise DatabaseIdentityError("Database compatibility staging is not recognized.")

    prefix = f"{name}.tmp-"
    temporary: Path | None = None
    for candidate in data_dir.glob(f"{prefix}*"):
        suffix = candidate.name.removeprefix(prefix)
        if len(suffix) != 32 or any(character not in "0123456789abcdef" for character in suffix):
            continue
        if _marker_identity(candidate, payload, label="Database compatibility temporary marker"):
            temporary = candidate
            break
    if temporary is None:
        temporary = data_dir / f"{prefix}{uuid.uuid4().hex}"

    descriptor: int | None = None
    try:
        if not temporary.exists():
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0),
                0o600,
            )
            view = memoryview(payload)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise OSError("short marker write")
                view = view[written:]
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = None
            _sync_directory(data_dir)
        temporary_identity = _marker_identity(
            temporary,
            payload,
            label="Database compatibility temporary marker",
        )
        if temporary_identity is None:
            raise DatabaseIdentityError("Database compatibility temporary marker is incomplete.")
        os.link(temporary, staging, follow_symlinks=False)
        published = _marker_identity(
            staging,
            payload,
            label="Database compatibility staging",
            allowed_links=frozenset({2}),
        )
        if published != temporary_identity:
            raise DatabaseIdentityError("Database compatibility staging changed during publish.")
        _sync_directory(data_dir)
        temporary.unlink()
        _sync_directory(data_dir)
    except DatabaseIdentityError:
        raise
    except OSError as exc:
        if descriptor is not None:
            os.close(descriptor)
            descriptor = None
        if temporary is not None:
            with contextlib.suppress(OSError):
                temporary.unlink(missing_ok=True)
        raise DatabaseIdentityError("Database compatibility guard could not be staged.") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
    if _marker_identity(staging, payload, label="Database compatibility staging") is None:
        raise DatabaseIdentityError("Database compatibility staging is incomplete.")
    return staging


def _write_tombstone_staging(data_dir: Path) -> Path:
    return _write_marker_staging(data_dir, _TOMBSTONE_STAGING, _TOMBSTONE_PAYLOAD)


def _write_pending_guard_staging(data_dir: Path) -> Path:
    return _write_marker_staging(
        data_dir,
        _PENDING_GUARD_STAGING,
        _PENDING_GUARD_PAYLOAD,
    )


def _cleanup_staging(data_dir: Path, legacy: Path) -> None:
    staging = data_dir / _TOMBSTONE_STAGING
    staging_identity = _tombstone_identity(staging, allowed_links=frozenset({1, 2}))
    if staging_identity is None:
        if _lstat(staging, label="Database compatibility staging") is not None:
            raise DatabaseIdentityError("Database compatibility staging is not recognized.")
        return
    legacy_tombstone = _tombstone_identity(legacy, allowed_links=frozenset({1, 2}))
    if legacy_tombstone is None:
        return
    try:
        staging.unlink()
    except OSError as exc:
        raise DatabaseIdentityError("Database compatibility staging could not be cleared.") from exc
    _sync_directory(data_dir)


def _install_tombstone_no_clobber(data_dir: Path, legacy: Path) -> None:
    existing = _tombstone_identity(legacy, allowed_links=frozenset({1, 2}))
    if existing is not None:
        _cleanup_staging(data_dir, legacy)
        final = _tombstone_identity(legacy)
        if final is None:
            raise DatabaseIdentityError("Legacy database compatibility guard is incomplete.")
        return
    if _lstat(legacy, label="Legacy database path") is not None:
        raise DatabaseIdentityError("Legacy database path appeared while installing its guard.")

    staging = _write_tombstone_staging(data_dir)
    if _tombstone_identity(staging) is None:
        raise DatabaseIdentityError("Database compatibility staging has unexpected links.")
    try:
        os.link(staging, legacy, follow_symlinks=False)
    except FileExistsError as exc:
        raise DatabaseIdentityError(
            "Legacy database path appeared while installing its guard."
        ) from exc
    except OSError as exc:
        raise DatabaseIdentityError(
            "Legacy database compatibility guard could not be installed."
        ) from exc
    _sync_directory(data_dir)
    _cleanup_staging(data_dir, legacy)
    if _tombstone_identity(legacy) is None:
        raise DatabaseIdentityError("Legacy database compatibility guard is incomplete.")


def _restore_parked_no_clobber(data_dir: Path, parked: Path, legacy: Path) -> None:
    parked_identity = _regular_identity(
        parked,
        label="Parked database cutover file",
        allowed_links=frozenset({1}),
    )
    if parked_identity is None:
        raise DatabaseIdentityError("Parked database cutover file disappeared.")
    try:
        os.link(parked, legacy, follow_symlinks=False)
    except FileExistsError as exc:
        raise DatabaseIdentityError(
            "Legacy database path appeared while restoring a raced cutover file; "
            f"the raced file remains at {parked.name}."
        ) from exc
    except OSError as exc:
        raise DatabaseIdentityError(
            f"A raced cutover file remains preserved at {parked.name}."
        ) from exc
    _sync_directory(data_dir)
    if _regular_identity(
        legacy,
        label="Restored legacy database file",
        allowed_links=frozenset({2}),
    ) != parked_identity:
        raise DatabaseIdentityError(
            f"A raced cutover file remains preserved at {parked.name}."
        )
    try:
        parked.unlink()
    except OSError as exc:
        raise DatabaseIdentityError(
            "The raced legacy file was restored, but its recovery link could not be cleared."
        ) from exc
    _sync_directory(data_dir)


def _remove_expected_parked(
    data_dir: Path,
    parked: Path,
    expected: _FileIdentity,
    *,
    label: str,
    parked_links: frozenset[int],
    survivor: Path,
    survivor_expected: _FileIdentity,
    survivor_links: frozenset[int],
) -> None:
    # The dual instance lock owns this internal recovery name. The no-clobber protocol
    # separately defends the public legacy path from an older, lock-unaware process.
    _assert_identity(
        survivor,
        survivor_expected,
        label="Kira database",
        allowed_links=survivor_links,
    )
    _assert_identity(
        parked,
        expected,
        label=label,
        allowed_links=parked_links,
    )
    try:
        parked.unlink()
    except OSError as exc:
        raise DatabaseIdentityError(
            "Database cutover completed, but its recovery link could not be cleared."
        ) from exc
    _sync_directory(data_dir)


def _park_and_install_tombstone(
    data_dir: Path,
    legacy: Path,
    expected: _FileIdentity,
    *,
    label: str,
    parked_links: frozenset[int],
    survivor: Path,
    survivor_expected: _FileIdentity,
    survivor_links: frozenset[int],
) -> None:
    parked = data_dir / _CUTOVER_PARKED
    try:
        _rename_no_replace(legacy, parked)
    except FileExistsError as exc:
        raise DatabaseIdentityError(
            "Database cutover recovery file already exists; retry Kira startup."
        ) from exc
    except OSError as exc:
        raise DatabaseIdentityError(
            "Legacy database guard publication was interrupted; retry Kira startup."
        ) from exc
    _sync_directory(data_dir)

    moved = _regular_identity(
        parked,
        label="Parked database cutover file",
        allowed_links=frozenset({1, 2}),
    )
    if moved != expected:
        _restore_parked_no_clobber(data_dir, parked, legacy)
        raise DatabaseIdentityError(f"{label} changed during final guard publication.")

    _install_tombstone_no_clobber(data_dir, legacy)
    _remove_expected_parked(
        data_dir,
        parked,
        expected,
        label=label,
        parked_links=parked_links,
        survivor=survivor,
        survivor_expected=survivor_expected,
        survivor_links=survivor_links,
    )


def _recover_parked_cutover(data_dir: Path, canonical: Path, legacy: Path) -> None:
    parked = data_dir / _CUTOVER_PARKED
    parked_info = _lstat(parked, label="Database cutover recovery file")
    if parked_info is None:
        return
    parked_identity = _regular_identity(
        parked,
        label="Database cutover recovery file",
        allowed_links=frozenset({1, 2}),
    )
    if parked_identity is None:
        raise DatabaseIdentityError("Database cutover recovery file disappeared.")
    canonical_identity = _regular_identity(
        canonical,
        label="Kira database",
        allowed_links=frozenset({1, 2}),
    )
    pending_identity = _pending_guard_identity(
        parked,
        allowed_links=frozenset({1, 2}),
    )
    pending_cutover = pending_identity is not None
    recognized = pending_cutover or parked_identity == canonical_identity
    if not recognized:
        if _lstat(legacy, label="Legacy database path") is None:
            _restore_parked_no_clobber(data_dir, parked, legacy)
        raise DatabaseIdentityError(
            "Unrecognized database cutover recovery file was preserved; operator recovery "
            "is required."
        )
    if canonical_identity is None:
        raise DatabaseIdentityError(
            "Database cutover recovery file exists but the Kira database is missing."
        )

    tombstone = _tombstone_identity(legacy, allowed_links=frozenset({1, 2}))
    if tombstone is None:
        if _lstat(legacy, label="Legacy database path") is not None:
            raise DatabaseIdentityError(
                "Legacy database path appeared during interrupted cutover recovery."
            )
        _install_tombstone_no_clobber(data_dir, legacy)
    else:
        _cleanup_staging(data_dir, legacy)
    _remove_expected_parked(
        data_dir,
        parked,
        parked_identity,
        label="Database cutover recovery file",
        parked_links=frozenset({1}) if pending_cutover else frozenset({2}),
        survivor=canonical,
        survivor_expected=canonical_identity,
        survivor_links=frozenset({1}) if pending_cutover else frozenset({2}),
    )
    if _regular_identity(canonical, label="Kira database") != canonical_identity:
        raise DatabaseIdentityError("Kira database identity changed during cutover recovery.")


def _cleanup_pending_guard_staging(data_dir: Path, legacy: Path) -> None:
    staging = data_dir / _PENDING_GUARD_STAGING
    staging_identity = _pending_guard_identity(staging, allowed_links=frozenset({1, 2}))
    if staging_identity is None:
        if _lstat(staging, label="Fresh database compatibility staging") is not None:
            raise DatabaseIdentityError("Fresh database compatibility staging is not recognized.")
        return
    legacy_guard = _pending_guard_identity(legacy, allowed_links=frozenset({1, 2}))
    if legacy_guard is None:
        return
    try:
        staging.unlink()
    except OSError as exc:
        raise DatabaseIdentityError(
            "Fresh database compatibility staging could not be cleared."
        ) from exc
    _sync_directory(data_dir)


def _discard_pending_guard_staging(data_dir: Path) -> None:
    staging = data_dir / _PENDING_GUARD_STAGING
    identity = _pending_guard_identity(staging)
    if identity is None:
        if _lstat(staging, label="Fresh database compatibility staging") is not None:
            raise DatabaseIdentityError("Fresh database compatibility staging is not recognized.")
        return
    try:
        staging.unlink()
    except OSError as exc:
        raise DatabaseIdentityError(
            "Fresh database compatibility staging could not be cleared."
        ) from exc
    _sync_directory(data_dir)


def _install_pending_guard_no_clobber(data_dir: Path, legacy: Path) -> None:
    existing = _pending_guard_identity(legacy, allowed_links=frozenset({1, 2}))
    if existing is not None:
        _cleanup_pending_guard_staging(data_dir, legacy)
        if _pending_guard_identity(legacy) is None:
            raise DatabaseIdentityError("Fresh database compatibility guard is incomplete.")
        return
    if _lstat(legacy, label="Legacy database path") is not None:
        raise DatabaseIdentityError("Legacy database path appeared during first-start setup.")

    staging = _write_pending_guard_staging(data_dir)
    if _pending_guard_identity(staging) is None:
        raise DatabaseIdentityError("Fresh database compatibility staging has unexpected links.")
    try:
        os.link(staging, legacy, follow_symlinks=False)
    except FileExistsError as exc:
        raise DatabaseIdentityError(
            "Legacy database path appeared during first-start setup."
        ) from exc
    except OSError as exc:
        raise DatabaseIdentityError(
            "Fresh database compatibility guard could not be installed."
        ) from exc
    _sync_directory(data_dir)
    _cleanup_pending_guard_staging(data_dir, legacy)
    if _pending_guard_identity(legacy) is None:
        raise DatabaseIdentityError("Fresh database compatibility guard is incomplete.")


def _promote_pending_guard(
    data_dir: Path,
    canonical: Path,
    canonical_expected: _FileIdentity,
    legacy: Path,
) -> None:
    _cleanup_pending_guard_staging(data_dir, legacy)
    expected = _pending_guard_identity(legacy)
    if expected is None:
        raise DatabaseIdentityError("Fresh database compatibility guard is incomplete.")
    staging = _write_tombstone_staging(data_dir)
    if _tombstone_identity(staging) is None:
        raise DatabaseIdentityError("Database compatibility staging has unexpected links.")
    _park_and_install_tombstone(
        data_dir,
        legacy,
        expected,
        label="Fresh database compatibility guard",
        parked_links=frozenset({1}),
        survivor=canonical,
        survivor_expected=canonical_expected,
        survivor_links=frozenset({1}),
    )
    if _tombstone_identity(legacy) is None:
        raise DatabaseIdentityError("Legacy database compatibility guard is incomplete.")


def _create_fresh_canonical(canonical: Path) -> _FileIdentity:
    descriptor: int | None = None
    try:
        descriptor = os.open(canonical, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
        os.fsync(descriptor)
        return _identity(os.fstat(descriptor))
    except FileExistsError as exc:
        raise DatabaseIdentityError(
            "Kira database appeared during first-start initialization."
        ) from exc
    except OSError as exc:
        raise DatabaseIdentityError("Kira database could not be initialized safely.") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _finish_legacy_cutover(
    data_dir: Path,
    canonical: Path,
    legacy: Path,
    expected: _FileIdentity,
    *,
    canonical_published: bool,
) -> Path:
    _prepare_single_file_database(legacy, expected)
    _assert_identity(
        legacy,
        expected,
        label="Legacy database",
        allowed_links=frozenset({1, 2}),
    )
    _remove_transient_sidecars(legacy)
    _assert_identity(
        legacy,
        expected,
        label="Legacy database",
        allowed_links=frozenset({1, 2}),
    )
    _sync_file(legacy, expected)
    staging = _write_tombstone_staging(data_dir)
    if _tombstone_identity(staging) is None:
        raise DatabaseIdentityError("Database compatibility staging has unexpected links.")

    if not canonical_published:
        _assert_identity(
            legacy,
            expected,
            label="Legacy database",
            allowed_links=frozenset({1}),
        )
        try:
            os.link(legacy, canonical, follow_symlinks=False)
        except FileExistsError as exc:
            raise DatabaseIdentityError(
                "Kira database appeared during migration; legacy data was left in place."
            ) from exc
        except OSError as exc:
            raise DatabaseIdentityError(
                "Legacy database publication was blocked; legacy data was left in place."
            ) from exc
        _sync_directory(data_dir)

    canonical_identity = _regular_identity(
        canonical,
        label="Kira database",
        allowed_links=frozenset({2}),
    )
    if canonical_identity != expected:
        current_legacy = _regular_identity(
            legacy,
            label="Legacy database",
            allowed_links=frozenset({1, 2}),
        )
        if canonical_identity is not None and canonical_identity == current_legacy:
            try:
                canonical.unlink()
            except OSError as exc:
                raise DatabaseIdentityError(
                    "Unexpected Kira database publication could not be rolled back."
                ) from exc
            _sync_directory(data_dir)
        raise DatabaseIdentityError("Database identity changed during publication.")
    _assert_identity(
        legacy,
        expected,
        label="Legacy database",
        allowed_links=frozenset({2}),
    )
    _park_and_install_tombstone(
        data_dir,
        legacy,
        expected,
        label="Legacy database",
        parked_links=frozenset({2}),
        survivor=canonical,
        survivor_expected=expected,
        survivor_links=frozenset({2}),
    )
    if _regular_identity(canonical, label="Kira database") != expected:
        raise DatabaseIdentityError("Kira database identity changed during cutover.")
    if _tombstone_identity(legacy) is None:
        raise DatabaseIdentityError("Legacy database compatibility guard is incomplete.")
    return canonical


def migrate_live_database(lock: InstanceLock) -> Path:
    """Publish ``kira.db`` while holding both current and legacy instance locks."""
    data_dir = lock.owned_data_dir()
    canonical, legacy = _identity_paths(data_dir)
    _recover_parked_cutover(data_dir, canonical, legacy)
    canonical_identity = _regular_identity(
        canonical,
        label="Kira database",
        allowed_links=frozenset({1, 2}),
    )
    tombstone = _tombstone_identity(legacy, allowed_links=frozenset({1, 2}))
    pending_guard = (
        None
        if tombstone is not None
        else _pending_guard_identity(legacy, allowed_links=frozenset({1, 2}))
    )
    legacy_identity = (
        None
        if tombstone is not None or pending_guard is not None
        else _regular_identity(
            legacy,
            label="Legacy database",
            allowed_links=frozenset({1, 2}),
        )
    )
    _validate_sidecars(canonical, main_exists=canonical_identity is not None)
    _validate_sidecars(legacy, main_exists=legacy_identity is not None)

    if pending_guard is None and (
        tombstone is not None or canonical_identity is not None or legacy_identity is not None
    ):
        _discard_pending_guard_staging(data_dir)

    if tombstone is not None:
        if canonical_identity is None:
            raise DatabaseIdentityError(
                "The legacy compatibility guard exists but the Kira database is missing."
            )
        if _regular_identity(canonical, label="Kira database") != canonical_identity:
            raise DatabaseIdentityError("Kira database has unexpected filesystem links.")
        _cleanup_staging(data_dir, legacy)
        if _tombstone_identity(legacy) is None:
            raise DatabaseIdentityError("Legacy database compatibility guard is incomplete.")
        return canonical

    if pending_guard is not None:
        _cleanup_pending_guard_staging(data_dir, legacy)
        if _pending_guard_identity(legacy) is None:
            raise DatabaseIdentityError("Fresh database compatibility guard is incomplete.")
        if canonical_identity is None:
            canonical_identity = _create_fresh_canonical(canonical)
            _sync_directory(data_dir)
        elif _regular_identity(canonical, label="Kira database") != canonical_identity:
            raise DatabaseIdentityError("Kira database has unexpected filesystem links.")
        _promote_pending_guard(data_dir, canonical, canonical_identity, legacy)
        if _regular_identity(canonical, label="Kira database") != canonical_identity:
            raise DatabaseIdentityError("Kira database identity changed during initialization.")
        return canonical

    if canonical_identity is not None and legacy_identity is not None:
        if not _same_identity(canonical_identity, legacy_identity):
            raise DatabaseIdentityError(
                "Both Kira and legacy databases exist; startup refused to choose between them."
            )
        if any(
            _lstat(path, label="Kira database sidecar") is not None
            for path in _sidecar_paths(canonical)
        ):
            raise DatabaseIdentityError(
                "Interrupted database cutover has canonical sidecars; "
                "operator recovery is required."
            )
        return _finish_legacy_cutover(
            data_dir,
            canonical,
            legacy,
            legacy_identity,
            canonical_published=True,
        )

    if canonical_identity is not None:
        if _regular_identity(canonical, label="Kira database") != canonical_identity:
            raise DatabaseIdentityError("Kira database has unexpected filesystem links.")
        _install_tombstone_no_clobber(data_dir, legacy)
        return canonical

    if legacy_identity is not None:
        if _regular_identity(legacy, label="Legacy database") != legacy_identity:
            raise DatabaseIdentityError("Legacy database has unexpected filesystem links.")
        return _finish_legacy_cutover(
            data_dir,
            canonical,
            legacy,
            legacy_identity,
            canonical_published=False,
        )

    _install_pending_guard_no_clobber(data_dir, legacy)
    canonical_identity = _create_fresh_canonical(canonical)
    _sync_directory(data_dir)
    _promote_pending_guard(data_dir, canonical, canonical_identity, legacy)
    if _regular_identity(canonical, label="Kira database") != canonical_identity:
        raise DatabaseIdentityError("Kira database identity changed during initialization.")
    return canonical
