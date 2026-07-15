"""Offline, owner-authenticated whole-instance data reset.

The operation never hard-deletes established data.  It acquires the same process lock as the
runtime, moves every configured durable root to a sibling quarantine, bootstraps and verifies a
fresh database, and leaves external integrations locked until individually reconnected.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import getpass
import json
import math
import os
import secrets
import shutil
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from kira.config import Config
from kira.connectors.consent import (
    LOCKED_PROVIDERS,
    lock_all_integrations,
    locked_integrations,
)
from kira.persistence.database_identity import (
    DatabaseIdentityError,
    migrate_live_database,
    select_database,
)
from kira.persistence.db import connect
from kira.persistence.durable_fs import durable_mkdir, durable_rename_no_replace
from kira.persistence.instance_lock import (
    InstanceAlreadyRunning,
    InstanceLock,
    ResetBarrier,
    ResetMaintenanceBusy,
)
from kira.persistence.migrations import latest_version
from kira.persistence.reset_recovery import (
    FAILED_FRESH_LABEL as _FAILED_FRESH_LABEL,
)
from kira.persistence.reset_recovery import (
    LEGACY_RESET_MANIFEST_DIRNAMES,
    RESET_FORMAT_VERSION,
    RESET_LOCATOR_SUFFIX,
    RESET_MANIFEST_DIRNAME,
    RESET_RETIRED_LOCATOR_SUFFIX,
    DirectoryIdentity,
    ResetRecoveryError,
    canonical_local_path,
    directory_identity,
    manifest_locator_payload,
    manifest_matches,
    manifest_roots,
    quarantine_paths,
    recover_interrupted_reset,
    retire_manifest_locator,
    write_manifest,
)
from kira.ui.owner_auth import (
    LOGIN_FAILURES_BEFORE_LOCK,
    LOGIN_MAX_LOCK_SECONDS,
    OwnerAuthService,
    OwnerLoginThrottledError,
)

CONFIRMATION_PHRASE = "RESET ALL KIRA DATA"
_COUNT_TABLES = ("owner_accounts", "projects", "sessions", "tasks", "kb_sources")


class DataResetError(RuntimeError):
    """A reset was refused or rolled back without exposing sensitive details."""


class _ResetPasswordRejected(DataResetError):
    """The reset-specific password check failed and must advance its durable throttle."""


@dataclass(frozen=True)
class DataResetResult:
    reset_id: str
    manifest: Path
    quarantines: tuple[Path, ...]


@dataclass(frozen=True)
class _RootMove:
    roles: tuple[str, ...]
    source: Path
    quarantine: Path
    source_identity: DirectoryIdentity


@dataclass(frozen=True)
class _AbsentRoot:
    roles: tuple[str, ...]
    source: Path


def _is_link_like(path: Path) -> bool:
    junction = getattr(path, "is_junction", None)
    return path.is_symlink() or bool(junction and junction())


def _resolved_root(path: Path, *, must_exist: bool = False) -> Path:
    try:
        return canonical_local_path(
            path,
            label="Reset root",
            must_exist=must_exist,
            reject_final_link=True,
        )
    except ResetRecoveryError as exc:
        raise DataResetError(str(exc)) from exc


def _resolved_config_anchor(path: Path) -> Path:
    try:
        return canonical_local_path(
            path,
            label="Kira configuration root",
            must_exist=True,
        )
    except ResetRecoveryError as exc:
        raise DataResetError(str(exc)) from exc


def _validate_safe_root(path: Path, *, config_root: Path) -> None:
    anchor = Path(path.anchor).resolve()
    home = Path.home().resolve()
    source_root = config_root.resolve()
    forbidden = {anchor, home, source_root}
    if (
        path in forbidden
        or home.is_relative_to(path)
        or source_root.is_relative_to(path)
        or len(path.parts) < 2
    ):
        raise DataResetError(f"Refusing unsafe reset root: {path}")


def _manifest_roots(data: Path, *, config_root: Path) -> tuple[Path, ...]:
    data_anchor = Path(os.path.abspath(data)).parent
    config_anchor = Path(os.path.abspath(config_root))
    bound = tuple(
        parent / name
        for parent in dict.fromkeys((config_anchor, data_anchor))
        for name in (RESET_MANIFEST_DIRNAME, *LEGACY_RESET_MANIFEST_DIRNAMES)
    )
    return tuple(dict.fromkeys((*bound, *manifest_roots(data, config_root=config_root))))


def _quarantine_paths(source: Path, reset_id: str) -> tuple[Path, ...]:
    return quarantine_paths(source, reset_id)


def _path_present(path: Path) -> bool:
    return os.path.lexists(path)


def _configured_root_paths(config: Config) -> dict[str, Path]:
    return {
        "data": _resolved_root(config.data_dir),
        "logs": _resolved_root(config.logs_dir),
        "knowledge": _resolved_root(config.knowledge_dir),
    }


def _assert_reset_binding(
    config: Config,
    *,
    config_root: Path,
    configured_roots: dict[str, Path],
    barrier: ResetBarrier,
    lock: InstanceLock,
) -> None:
    data = configured_roots["data"]
    if barrier.owned_data_dir() != data or lock.owned_data_dir() != data:
        raise DataResetError("Reset locks no longer protect the planned data root")
    if _resolved_config_anchor(config.root) != config_root:
        raise DataResetError("The Kira configuration root changed during reset")
    if _configured_root_paths(config) != configured_roots:
        raise DataResetError("Configured Kira reset roots changed during reset")


def _planned_moves(
    config: Config,
    reset_id: str,
    *,
    include_external_knowledge: bool,
    include_external_logs: bool = False,
    configured_roots: dict[str, Path] | None = None,
    config_root: Path | None = None,
) -> tuple[list[_RootMove], list[Path]]:
    config_root = _resolved_config_anchor(config.root) if config_root is None else config_root
    configured = _configured_root_paths(config) if configured_roots is None else configured_roots
    data = configured["data"]
    try:
        database = select_database(data)
    except DatabaseIdentityError as exc:
        raise DataResetError(str(exc)) from exc
    if not data.is_dir() or not database.is_file():
        raise DataResetError("No initialized Kira database was found to reset")
    if _is_link_like(database) or database.resolve().parent != data:
        raise DataResetError("Refusing a linked database outside the Kira data root")
    _validate_safe_root(data, config_root=config_root)
    manifest_roots = _manifest_roots(data, config_root=config_root)
    if any(root == data or root.is_relative_to(data) for root in manifest_roots):
        raise DataResetError("Reset manifest storage must be outside the Kira data root")

    managed_logs = config_root / "logs"
    grouped: dict[Path, set[str]] = {data: {"data"}}
    for role in ("logs", "knowledge"):
        path = configured[role]
        if path == data or path.is_relative_to(data):
            grouped[data].add(role)
            continue
        if data.is_relative_to(path):
            raise DataResetError(f"Refusing {role} root that contains the Kira data root")
        if any(
            path == root or path.is_relative_to(root) or root.is_relative_to(path)
            for root in manifest_roots
        ):
            raise DataResetError(f"Refusing {role} root that overlaps reset manifest storage")
        _validate_safe_root(path, config_root=config_root)
        external_authorized = (
            include_external_knowledge
            if role == "knowledge"
            else include_external_logs or path == managed_logs
        )
        if not external_authorized:
            raise DataResetError(
                f"The configured {role} root is outside Kira's data root; "
                "its exact path requires separate confirmation"
            )
        if path.exists():
            if not path.is_dir():
                raise DataResetError(f"Configured {role} root is not a directory: {path}")
            grouped.setdefault(path, set()).add(role)

    # Collapse an unusual nested external layout to the outer moved directory.  All roots were
    # safety-checked above, and the data root can never be contained by an external root.
    selected: dict[Path, set[str]] = {}
    for path in sorted(grouped, key=lambda item: len(item.parts)):
        parent = next((root for root in selected if path.is_relative_to(root)), None)
        if parent is not None:
            selected[parent].update(grouped[path])
        else:
            selected[path] = set(grouped[path])

    moves: list[_RootMove] = []
    for source, roles in selected.items():
        quarantine, *legacy_quarantines = _quarantine_paths(source, reset_id)
        failed_fresh = source.with_name(f".{source.name}.{_FAILED_FRESH_LABEL}-{reset_id}")
        collision = next(
            (
                path
                for path in (quarantine, *legacy_quarantines, failed_fresh)
                if _path_present(path)
            ),
            None,
        )
        if collision is not None:
            raise DataResetError(f"Reset recovery path already exists: {collision}")
        try:
            identity = directory_identity(source, label="Reset source root")
        except ResetRecoveryError as exc:
            raise DataResetError(str(exc)) from exc
        moves.append(_RootMove(tuple(sorted(roles)), source, quarantine, identity))
    return moves, list(dict.fromkeys(configured.values()))


def _planned_absent_roots(
    configured: dict[str, Path],
    reset_id: str,
    moves: list[_RootMove],
) -> list[_AbsentRoot]:
    grouped: dict[Path, set[str]] = {}
    for role, source in configured.items():
        covering_index = next(
            (
                index
                for index, move in enumerate(moves)
                if source == move.source or source.is_relative_to(move.source)
            ),
            None,
        )
        if covering_index is not None:
            move = moves[covering_index]
            if role not in move.roles:
                moves[covering_index] = _RootMove(
                    tuple(sorted((*move.roles, role))),
                    move.source,
                    move.quarantine,
                    move.source_identity,
                )
            continue
        if _path_present(source):
            raise DataResetError(f"Reset root appeared while the reset plan was prepared: {source}")
        grouped.setdefault(source, set()).add(role)
    selected: dict[Path, set[str]] = {}
    for source in sorted(grouped, key=lambda item: len(item.parts)):
        parent = next((root for root in selected if source.is_relative_to(root)), None)
        if parent is not None:
            selected[parent].update(grouped[source])
        else:
            selected[source] = set(grouped[source])

    absent: list[_AbsentRoot] = []
    for source, roles in selected.items():
        failed = source.with_name(f".{source.name}.{_FAILED_FRESH_LABEL}-{reset_id}")
        if _path_present(failed):
            raise DataResetError(f"Reset recovery archive already exists: {failed}")
        absent.append(_AbsentRoot(tuple(sorted(roles)), source))
    return absent


def _manifest_path(
    config: Config,
    reset_id: str,
    *,
    data_root: Path | None = None,
    config_root: Path | None = None,
) -> Path:
    data = (
        _resolved_root(config.data_dir) if data_root is None else Path(os.path.abspath(data_root))
    )
    config_anchor = _resolved_config_anchor(config.root) if config_root is None else config_root
    roots = _manifest_roots(
        data,
        config_root=config_anchor,
    )
    for root in roots:
        if _path_present(root) and (_is_link_like(root) or not root.is_dir()):
            raise DataResetError("Reset manifest storage is not a regular local directory")
    candidates = tuple(
        path
        for root in roots
        for path in (
            root / f"{reset_id}.json",
            root / f"{reset_id}{RESET_LOCATOR_SUFFIX}",
            root / f"{reset_id}{RESET_RETIRED_LOCATOR_SUFFIX}",
        )
    )
    collision = next((path for path in candidates if _path_present(path)), None)
    if collision is not None:
        raise DataResetError(f"Reset manifest already exists: {collision}")
    canonical = data.parent / RESET_MANIFEST_DIRNAME / f"{reset_id}.json"
    if canonical.parent not in roots:
        raise DataResetError("Reset manifest storage is not anchored to the Kira data root")
    return canonical


def _manifest_locator_path(manifest: Path, *, config_root: Path, reset_id: str) -> Path | None:
    locator_root = config_root / RESET_MANIFEST_DIRNAME
    if locator_root == manifest.parent:
        return None
    return locator_root / f"{reset_id}{RESET_LOCATOR_SUFFIX}"


def _reset_auth_path(config_or_data: Config | Path) -> Path:
    data = (
        Path(os.path.abspath(config_or_data))
        if isinstance(config_or_data, Path)
        else config_or_data.data_dir.resolve()
    )
    return data.with_name(f".{data.name}.kira-reset-auth.json")


def _reset_auth_state(config_or_data: Config | Path) -> tuple[int, dt.datetime | None]:
    path = _reset_auth_path(config_or_data)
    if not path.exists() and not _is_link_like(path):
        return 0, None
    try:
        info = path.stat(follow_symlinks=False)
        if _is_link_like(path) or not path.is_file() or int(info.st_nlink) != 1:
            raise DataResetError("Reset authentication throttle is not a regular local file")
        payload = json.loads(path.read_text(encoding="utf-8"))
        if set(payload) != {"failed_attempts", "locked_until"}:
            raise ValueError("unexpected reset authentication throttle fields")
        attempts = int(payload["failed_attempts"])
        if attempts < 0:
            raise ValueError("negative reset authentication failure count")
        locked_raw = payload["locked_until"]
        locked = None if locked_raw is None else dt.datetime.fromisoformat(str(locked_raw))
        if locked is not None:
            if locked.tzinfo is None:
                raise ValueError("naive reset authentication lock time")
            locked = locked.astimezone(dt.UTC)
        return attempts, locked
    except DataResetError:
        raise
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise DataResetError("Reset authentication throttle is unreadable") from exc


def _check_reset_auth_throttle(config_or_data: Config | Path) -> None:
    _attempts, locked = _reset_auth_state(config_or_data)
    now = dt.datetime.now(dt.UTC)
    if locked is not None and now < locked:
        raise OwnerLoginThrottledError(math.ceil((locked - now).total_seconds()))


def _record_reset_auth_failure(config_or_data: Config | Path) -> int:
    attempts, _locked = _reset_auth_state(config_or_data)
    attempts += 1
    delay = 0
    if attempts >= LOGIN_FAILURES_BEFORE_LOCK:
        delay = min(
            30 * (2 ** (attempts - LOGIN_FAILURES_BEFORE_LOCK)),
            LOGIN_MAX_LOCK_SECONDS,
        )
    locked_until = (
        (dt.datetime.now(dt.UTC) + dt.timedelta(seconds=delay)).isoformat() if delay else None
    )
    try:
        _write_manifest(
            _reset_auth_path(config_or_data),
            {"failed_attempts": attempts, "locked_until": locked_until},
        )
    except OSError as exc:
        raise DataResetError("Reset authentication throttle could not be persisted") from exc
    return delay


def _clear_reset_auth_throttle(config_or_data: Config | Path) -> None:
    path = _reset_auth_path(config_or_data)
    if not path.exists() and not _is_link_like(path):
        return
    _reset_auth_state(config_or_data)
    try:
        path.unlink()
    except OSError as exc:
        raise DataResetError("Reset authentication throttle could not be cleared") from exc


_write_manifest = write_manifest


async def _database_facts(db) -> tuple[int, dict[str, int]]:  # noqa: ANN001
    row = await (await db.execute("PRAGMA user_version")).fetchone()
    version = int(row[0]) if row is not None else 0
    tables = {
        str(item[0])
        for item in await (
            await db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        ).fetchall()
    }
    counts: dict[str, int] = {}
    for table in _COUNT_TABLES:
        if table in tables:
            count = await (await db.execute(f"SELECT COUNT(*) FROM {table}")).fetchone()
            counts[table] = int(count[0]) if count is not None else 0
    return version, counts


def _copy_authentication_snapshot(database: Path, destination: Path) -> Path:
    sources = [database]
    for suffix in ("-wal", "-shm", "-journal"):
        sidecar = database.with_name(f"{database.name}{suffix}")
        if sidecar.exists() or _is_link_like(sidecar):
            sources.append(sidecar)

    before: dict[Path, tuple[int, int, int, int]] = {}
    try:
        for source in sources:
            info = source.stat(follow_symlinks=False)
            if _is_link_like(source) or not source.is_file() or int(info.st_nlink) != 1:
                raise DataResetError("Reset authentication source is not a regular local file")
            before[source] = (
                int(info.st_dev),
                int(info.st_ino),
                int(info.st_size),
                int(info.st_mtime_ns),
            )
        snapshot = destination / database.name
        for source in sources:
            shutil.copy2(source, destination / source.name)
        for source, expected in before.items():
            info = source.stat(follow_symlinks=False)
            current = (
                int(info.st_dev),
                int(info.st_ino),
                int(info.st_size),
                int(info.st_mtime_ns),
            )
            if current != expected:
                raise DataResetError("Reset authentication source changed during snapshot")
        current_sources = {
            path.name
            for path in (
                database,
                *(database.with_name(f"{database.name}{s}") for s in ("-wal", "-shm", "-journal")),
            )
            if path.exists() or _is_link_like(path)
        }
        if current_sources != {source.name for source in sources}:
            raise DataResetError("Reset authentication sidecars changed during snapshot")
        return snapshot
    except DataResetError:
        raise
    except OSError as exc:
        raise DataResetError("The existing Kira database could not be snapshotted safely") from exc


async def _authenticate_old_database(database: Path, password: str) -> tuple[int, dict[str, int]]:
    try:
        with tempfile.TemporaryDirectory(prefix="kira-reset-auth-") as temporary:
            snapshot = _copy_authentication_snapshot(database, Path(temporary))
            db = await connect(snapshot)
            try:
                lock = asyncio.Lock()
                auth = OwnerAuthService(db, lock)
                if not await auth.is_enrolled():
                    raise DataResetError(
                        "Owner setup is incomplete; there is no enrolled owner to verify"
                    )
                # Reset owns a separate durable throttle beside the data root.  Zero only the
                # snapshot's failure counter so crossing the source login threshold cannot turn
                # a verified bad guess into a discarded snapshot-only lockout.  Preserve the
                # source locked_until value so an already-active owner lock still blocks reset.
                await db.execute("UPDATE owner_accounts SET failed_attempts = 0 WHERE id = 1")
                await db.commit()
                if not await auth.verify_owner_password(password):
                    raise _ResetPasswordRejected("Owner password was not accepted")
                facts = await _database_facts(db)
                await db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                return facts
            finally:
                await db.close()
    except (DataResetError, OwnerLoginThrottledError):
        raise
    except Exception as exc:
        raise DataResetError("The existing Kira database could not be opened safely") from exc


async def _bootstrap_fresh_database(database: Path) -> int:
    db = await connect(database)
    try:
        integrity = await (await db.execute("PRAGMA integrity_check")).fetchone()
        if integrity != ("ok",):
            raise DataResetError("Fresh database integrity verification failed")
        if await (await db.execute("PRAGMA foreign_key_check")).fetchone() is not None:
            raise DataResetError("Fresh database foreign-key verification failed")
        if await (await db.execute("SELECT 1 FROM owner_accounts LIMIT 1")).fetchone() is not None:
            raise DataResetError("Fresh database unexpectedly contains an owner")
        row = await (await db.execute("PRAGMA user_version")).fetchone()
        version = int(row[0]) if row is not None else 0
        if version != latest_version():
            raise DataResetError("Fresh database schema verification failed")
        return version
    finally:
        await db.close()


async def _reset_with_lock(
    config: Config,
    password: str,
    barrier: ResetBarrier,
    lock: InstanceLock,
    *,
    include_external_knowledge: bool = False,
    include_external_logs: bool = False,
    confirmed_external_roots: dict[str, Path] | None = None,
) -> DataResetResult:
    bound_data = lock.owned_data_dir()
    if barrier.owned_data_dir() != bound_data:
        raise DataResetError("Reset locks do not protect the configured data directory")
    bound_config_root = _resolved_config_anchor(config.root)
    configured_roots = _configured_root_paths(config)
    if configured_roots["data"] != bound_data:
        raise DataResetError("The configured data root changed before reset planning")
    for role, confirmed in (confirmed_external_roots or {}).items():
        if role not in {"logs", "knowledge"} or configured_roots[role] != confirmed:
            raise DataResetError("An exact external reset confirmation no longer matches")
    _assert_reset_binding(
        config,
        config_root=bound_config_root,
        configured_roots=configured_roots,
        barrier=barrier,
        lock=lock,
    )
    now = dt.datetime.now(dt.UTC)
    reset_id = f"{now.strftime('%Y%m%dT%H%M%SZ')}-{secrets.token_hex(4)}"
    try:
        source_database = select_database(bound_data)
    except DatabaseIdentityError as exc:
        raise DataResetError(str(exc)) from exc
    moves, _configured_root_list = _planned_moves(
        config,
        reset_id,
        include_external_knowledge=include_external_knowledge,
        include_external_logs=include_external_logs,
        configured_roots=configured_roots,
        config_root=bound_config_root,
    )
    absent_roots = _planned_absent_roots(configured_roots, reset_id, moves)
    manifest_path = _manifest_path(
        config,
        reset_id,
        data_root=bound_data,
        config_root=bound_config_root,
    )
    locator_path = _manifest_locator_path(
        manifest_path,
        config_root=bound_config_root,
        reset_id=reset_id,
    )
    locator: dict[str, Any] | None = None
    _assert_reset_binding(
        config,
        config_root=bound_config_root,
        configured_roots=configured_roots,
        barrier=barrier,
        lock=lock,
    )
    _check_reset_auth_throttle(bound_data)
    try:
        old_version, old_counts = await _authenticate_old_database(source_database, password)
    except _ResetPasswordRejected:
        delay = _record_reset_auth_failure(bound_data)
        if delay:
            raise OwnerLoginThrottledError(delay) from None
        raise
    _assert_reset_binding(
        config,
        config_root=bound_config_root,
        configured_roots=configured_roots,
        barrier=barrier,
        lock=lock,
    )
    _clear_reset_auth_throttle(bound_data)
    manifest: dict[str, Any] = {
        "format_version": RESET_FORMAT_VERSION,
        "reset_id": reset_id,
        "created_at": now.isoformat(),
        "status": "in_progress",
        "config_root": str(bound_config_root),
        "old_schema_version": old_version,
        "old_counts": old_counts,
        "roots": [
            {
                "roles": list(move.roles),
                "source": str(move.source),
                "quarantine": str(move.quarantine),
                "source_identity": move.source_identity.payload(),
            }
            for move in moves
        ],
        "absent_roots": [
            {"roles": list(root.roles), "source": str(root.source)} for root in absent_roots
        ],
        "preserved": [str(bound_config_root / ".env"), str(bound_config_root / "config")],
        "locked_integrations": sorted(LOCKED_PROVIDERS),
    }
    if locator_path is not None:
        locator = manifest_locator_payload(
            reset_id=reset_id,
            manifest=manifest_path,
            config_root=bound_config_root,
            data_root=bound_data,
            manifest_payload=manifest,
        )
    _assert_reset_binding(
        config,
        config_root=bound_config_root,
        configured_roots=configured_roots,
        barrier=barrier,
        lock=lock,
    )
    try:
        _write_manifest(manifest_path, manifest)
        if not manifest_matches(manifest_path, manifest):
            raise DataResetError("The reset manifest could not be verified; no data was moved")
        if locator_path is not None and locator is not None:
            _write_manifest(locator_path, locator)
            if not manifest_matches(locator_path, locator):
                raise DataResetError(
                    "The reset manifest locator could not be verified; no data was moved"
                )
    except (OSError, ResetRecoveryError) as exc:
        raise DataResetError("The reset manifest could not be written; no data was moved") from exc

    try:
        _assert_reset_binding(
            config,
            config_root=bound_config_root,
            configured_roots=configured_roots,
            barrier=barrier,
            lock=lock,
        )
        for root in absent_roots:
            if _path_present(root.source):
                raise DataResetError(
                    f"Originally absent reset root appeared before quarantine: {root.source}"
                )
        for move in moves:
            current = directory_identity(move.source, label="Reset source root")
            if current != move.source_identity:
                raise DataResetError("Reset source root changed before quarantine")
            durable_rename_no_replace(move.source, move.quarantine)
            if (
                directory_identity(move.quarantine, label="Reset quarantine")
                != move.source_identity
            ):
                raise DataResetError("Reset source identity changed during quarantine")

        _assert_reset_binding(
            config,
            config_root=bound_config_root,
            configured_roots=configured_roots,
            barrier=barrier,
            lock=lock,
        )
        top_level_roots = {move.source for move in moves} | {root.source for root in absent_roots}
        fresh_root_identities: dict[Path, DirectoryIdentity] = {}
        for root in sorted(top_level_roots, key=lambda item: len(item.parts)):
            durable_mkdir(root)
            fresh_root_identities[root] = directory_identity(root, label="Fresh reset root")
        for root in sorted(
            set(configured_roots.values()) - top_level_roots,
            key=lambda item: len(item.parts),
        ):
            durable_mkdir(root)
            fresh_root_identities[root] = directory_identity(root, label="Fresh reset root")
        _assert_reset_binding(
            config,
            config_root=bound_config_root,
            configured_roots=configured_roots,
            barrier=barrier,
            lock=lock,
        )
        lock_all_integrations(bound_data)
        fresh_database = migrate_live_database(lock)
        fresh_version = await _bootstrap_fresh_database(fresh_database)
        for root, expected in fresh_root_identities.items():
            if directory_identity(root, label="Fresh reset root") != expected:
                raise DataResetError("Fresh reset root changed before completion")
        if locked_integrations(bound_data) != LOCKED_PROVIDERS:
            raise DataResetError("Fresh integration-consent lock verification failed")
        _assert_reset_binding(
            config,
            config_root=bound_config_root,
            configured_roots=configured_roots,
            barrier=barrier,
            lock=lock,
        )

        manifest.update(
            status="completed",
            completed_at=dt.datetime.now(dt.UTC).isoformat(),
            fresh_schema_version=fresh_version,
            integrity_check="ok",
        )
        _write_manifest(manifest_path, manifest)
        if not manifest_matches(manifest_path, manifest):
            raise DataResetError("The completed reset manifest could not be verified")
        if locator_path is not None and locator is not None:
            retire_manifest_locator(locator_path, locator)
    except BaseException as exc:
        if manifest.get("status") == "completed":
            try:
                completed = manifest_matches(manifest_path, manifest)
            except ResetRecoveryError:
                completed = False
            if completed:
                try:
                    if locator_path is not None and locator is not None:
                        retire_manifest_locator(locator_path, locator)
                except ResetRecoveryError as retirement_exc:
                    raise DataResetError(
                        "Reset completed, but its recovery locator could not be retired"
                    ) from retirement_exc
                return DataResetResult(
                    reset_id=reset_id,
                    manifest=manifest_path,
                    quarantines=tuple(move.quarantine for move in moves),
                )
        try:
            if not recover_interrupted_reset(config, barrier, lock):
                raise ResetRecoveryError("The published reset manifest is no longer discoverable")
        except ResetRecoveryError as recovery_exc:
            raise DataResetError(
                "Reset failed and lossless automatic recovery was blocked; no recovery path "
                "was overwritten or deleted"
            ) from recovery_exc
        if isinstance(exc, (DataResetError, OwnerLoginThrottledError)):
            raise
        raise DataResetError("Reset failed; the original Kira data was restored") from exc

    return DataResetResult(
        reset_id=reset_id,
        manifest=manifest_path,
        quarantines=tuple(move.quarantine for move in moves),
    )


async def reset_all_data(
    config: Config,
    password: str,
    *,
    include_external_knowledge: bool = False,
    include_external_logs: bool = False,
) -> DataResetResult:
    """Programmatic reset entry point; always enforces exclusive runtime ownership."""
    try:
        with (
            ResetBarrier(config.data_dir) as barrier,
            InstanceLock(config.data_dir) as lock,
        ):
            recover_interrupted_reset(config, barrier, lock)
            return await _reset_with_lock(
                config,
                password,
                barrier,
                lock,
                include_external_knowledge=include_external_knowledge,
                include_external_logs=include_external_logs,
            )
    except (InstanceAlreadyRunning, ResetMaintenanceBusy, ResetRecoveryError) as exc:
        raise DataResetError(str(exc)) from exc


def reset_cli(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="kira reset", description="Offline, quarantine-first Kira data reset."
    )
    parser.add_argument("target", choices=["data"], help="Reset all Kira runtime data.")
    parser.parse_args(argv)

    if not sys.stdin.isatty() or not sys.stdout.isatty():
        print("Reset refused: run this command interactively in an attended terminal.")
        return 1

    from kira.config import ConfigError, load_config

    try:
        config = load_config()
    except ConfigError as exc:
        print(f"Configuration error: {exc}")
        return 1

    barrier = ResetBarrier(config.data_dir)
    lock = InstanceLock(config.data_dir)
    try:
        barrier.acquire()
        lock.acquire()
        recover_interrupted_reset(config, barrier, lock)
    except (InstanceAlreadyRunning, ResetMaintenanceBusy, ResetRecoveryError) as exc:
        lock.release()
        barrier.release()
        print(f"Reset refused: {exc}")
        return 1
    try:
        print("This archives all Kira chats, projects, tasks, knowledge, tokens, and logs.")
        print("Your source checkout, .env, and config are preserved. Nothing is hard-deleted.")
        if input(f"Type {CONFIRMATION_PHRASE} to continue: ") != CONFIRMATION_PHRASE:
            print("Reset cancelled: confirmation phrase did not match.")
            return 1
        data_root = _resolved_root(config.data_dir, must_exist=True)
        logs_root = _resolved_root(config.logs_dir)
        knowledge_root = _resolved_root(config.knowledge_dir)
        include_external_logs = False
        include_external_knowledge = False
        confirmed: set[Path] = set()
        confirmed_external_roots: dict[str, Path] = {}
        for role, root in (("logs", logs_root), ("knowledge", knowledge_root)):
            if root.is_relative_to(data_root):
                continue
            if role == "logs" and root == config.root.resolve() / "logs":
                continue
            print(f"External {role} root: {root}")
            if root not in confirmed:
                if input(f"Type that exact path to quarantine this {role} root: ") != str(root):
                    print(f"Reset cancelled: external {role} path did not match.")
                    return 1
                confirmed.add(root)
            if role == "logs":
                include_external_logs = True
            else:
                include_external_knowledge = True
            confirmed_external_roots[role] = root
        password = getpass.getpass("Current owner password: ")
        result = asyncio.run(
            _reset_with_lock(
                config,
                password,
                barrier,
                lock,
                include_external_knowledge=include_external_knowledge,
                include_external_logs=include_external_logs,
                confirmed_external_roots=confirmed_external_roots,
            )
        )
    except (DataResetError, OwnerLoginThrottledError) as exc:
        print(f"Reset refused: {exc}")
        return 1
    except Exception as exc:
        print(f"Reset failed safely; no established data was deleted ({type(exc).__name__}).")
        return 1
    except KeyboardInterrupt:
        print("\nReset cancelled.")
        return 130
    finally:
        lock.release()
        barrier.release()

    print(f"Kira data reset complete. Manifest: {result.manifest}")
    for quarantine in result.quarantines:
        print(f"Quarantine: {quarantine}")
    print("Start Kira, create the new owner login, then reconnect each integration explicitly.")
    return 0
