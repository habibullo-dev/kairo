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

from jarvis.config import Config
from jarvis.connectors.consent import LOCKED_PROVIDERS, lock_all_integrations
from jarvis.persistence.database_identity import (
    DatabaseIdentityError,
    migrate_live_database,
    select_database,
)
from jarvis.persistence.db import connect
from jarvis.persistence.instance_lock import InstanceAlreadyRunning, InstanceLock
from jarvis.persistence.migrations import latest_version
from jarvis.ui.owner_auth import (
    LOGIN_FAILURES_BEFORE_LOCK,
    LOGIN_MAX_LOCK_SECONDS,
    OwnerAuthService,
    OwnerLoginThrottledError,
)

CONFIRMATION_PHRASE = "RESET ALL KIRA DATA"
_COUNT_TABLES = ("owner_accounts", "projects", "sessions", "tasks", "kb_sources")
_RESET_MANIFEST_DIRNAME = ".kira-reset-manifests"
_LEGACY_RESET_MANIFEST_DIRNAMES = (".kairo-reset-manifests",)
_QUARANTINE_LABEL = "kira-quarantine"
_LEGACY_QUARANTINE_LABELS = ("kairo-quarantine",)


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


def _is_link_like(path: Path) -> bool:
    junction = getattr(path, "is_junction", None)
    return path.is_symlink() or bool(junction and junction())


def _resolved_root(path: Path, *, must_exist: bool = False) -> Path:
    absolute = Path(os.path.abspath(path))
    if absolute.exists() and _is_link_like(absolute):
        raise DataResetError(f"Refusing linked or junction-backed reset root: {absolute}")
    try:
        return absolute.resolve(strict=must_exist)
    except OSError as exc:
        raise DataResetError(f"Reset root is unavailable: {absolute}") from exc


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


def _manifest_roots(data: Path) -> tuple[Path, ...]:
    parent = data.resolve().parent
    return (
        parent / _RESET_MANIFEST_DIRNAME,
        *(parent / name for name in _LEGACY_RESET_MANIFEST_DIRNAMES),
    )


def _quarantine_paths(source: Path, reset_id: str) -> tuple[Path, ...]:
    return (
        source.with_name(f".{source.name}.{_QUARANTINE_LABEL}-{reset_id}"),
        *(
            source.with_name(f".{source.name}.{label}-{reset_id}")
            for label in _LEGACY_QUARANTINE_LABELS
        ),
    )


def _path_present(path: Path) -> bool:
    return os.path.lexists(path)


def _planned_moves(
    config: Config, reset_id: str, *, include_external_knowledge: bool
) -> tuple[list[_RootMove], list[Path]]:
    config_root = config.root.resolve()
    data = _resolved_root(config.data_dir, must_exist=True)
    try:
        database = select_database(data)
    except DatabaseIdentityError as exc:
        raise DataResetError(str(exc)) from exc
    if not data.is_dir() or not database.is_file():
        raise DataResetError("No initialized Kira database was found to reset")
    if _is_link_like(database) or database.resolve().parent != data:
        raise DataResetError("Refusing a linked database outside the Kira data root")
    _validate_safe_root(data, config_root=config_root)
    manifest_roots = _manifest_roots(data)
    if any(root == data or root.is_relative_to(data) for root in manifest_roots):
        raise DataResetError("Reset manifest storage must be outside the Kira data root")

    configured = {
        "data": data,
        "logs": _resolved_root(config.logs_dir),
        "knowledge": _resolved_root(config.knowledge_dir),
    }
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
        if path.exists():
            if not path.is_dir():
                raise DataResetError(f"Configured {role} root is not a directory: {path}")
            if role == "knowledge" and not include_external_knowledge:
                raise DataResetError(
                    "The configured knowledge vault is outside Kira's data root; "
                    "its exact path requires separate confirmation"
                )
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
        collision = next(
            (path for path in (quarantine, *legacy_quarantines) if _path_present(path)),
            None,
        )
        if collision is not None:
            raise DataResetError(f"Reset quarantine already exists: {collision}")
        moves.append(_RootMove(tuple(sorted(roles)), source, quarantine))
    return moves, list(dict.fromkeys(configured.values()))


def _manifest_path(config: Config, reset_id: str) -> Path:
    roots = _manifest_roots(config.data_dir)
    for root in roots:
        if _path_present(root) and (_is_link_like(root) or not root.is_dir()):
            raise DataResetError("Reset manifest storage is not a regular local directory")
    candidates = tuple(root / f"{reset_id}.json" for root in roots)
    collision = next((path for path in candidates if _path_present(path)), None)
    if collision is not None:
        raise DataResetError(f"Reset manifest already exists: {collision}")
    return candidates[0]


def _reset_auth_path(config: Config) -> Path:
    data = config.data_dir.resolve()
    return data.with_name(f".{data.name}.kira-reset-auth.json")


def _reset_auth_state(config: Config) -> tuple[int, dt.datetime | None]:
    path = _reset_auth_path(config)
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


def _check_reset_auth_throttle(config: Config) -> None:
    _attempts, locked = _reset_auth_state(config)
    now = dt.datetime.now(dt.UTC)
    if locked is not None and now < locked:
        raise OwnerLoginThrottledError(math.ceil((locked - now).total_seconds()))


def _record_reset_auth_failure(config: Config) -> int:
    attempts, _locked = _reset_auth_state(config)
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
            _reset_auth_path(config),
            {"failed_attempts": attempts, "locked_until": locked_until},
        )
    except OSError as exc:
        raise DataResetError("Reset authentication throttle could not be persisted") from exc
    return delay


def _clear_reset_auth_throttle(config: Config) -> None:
    path = _reset_auth_path(config)
    if not path.exists() and not _is_link_like(path):
        return
    _reset_auth_state(config)
    try:
        path.unlink()
    except OSError as exc:
        raise DataResetError("Reset authentication throttle could not be cleared") from exc


def _write_manifest(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise


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


def _remove_fresh_root(path: Path) -> None:
    if not path.exists():
        return
    if path.is_dir() and not _is_link_like(path):
        shutil.rmtree(path)
    else:
        path.unlink()


async def _reset_with_lock(
    config: Config,
    password: str,
    lock: InstanceLock,
    *,
    include_external_knowledge: bool = False,
) -> DataResetResult:
    if lock.owned_data_dir() != config.data_dir.resolve():
        raise DataResetError("Reset lock does not protect the configured data directory")
    now = dt.datetime.now(dt.UTC)
    reset_id = f"{now.strftime('%Y%m%dT%H%M%SZ')}-{secrets.token_hex(4)}"
    try:
        source_database = select_database(config.data_dir)
    except DatabaseIdentityError as exc:
        raise DataResetError(str(exc)) from exc
    moves, configured_roots = _planned_moves(
        config, reset_id, include_external_knowledge=include_external_knowledge
    )
    manifest_path = _manifest_path(config, reset_id)
    _check_reset_auth_throttle(config)
    try:
        old_version, old_counts = await _authenticate_old_database(source_database, password)
    except _ResetPasswordRejected:
        delay = _record_reset_auth_failure(config)
        if delay:
            raise OwnerLoginThrottledError(delay) from None
        raise
    _clear_reset_auth_throttle(config)
    manifest: dict[str, Any] = {
        "reset_id": reset_id,
        "created_at": now.isoformat(),
        "status": "in_progress",
        "old_schema_version": old_version,
        "old_counts": old_counts,
        "roots": [
            {
                "roles": list(move.roles),
                "source": str(move.source),
                "quarantine": str(move.quarantine),
            }
            for move in moves
        ],
        "preserved": [str(config.root.resolve() / ".env"), str(config.root.resolve() / "config")],
        "locked_integrations": sorted(LOCKED_PROVIDERS),
    }
    try:
        _write_manifest(manifest_path, manifest)
    except OSError as exc:
        raise DataResetError("The reset manifest could not be written; no data was moved") from exc

    moved: list[_RootMove] = []
    created_roots: set[Path] = set()
    try:
        for move in moves:
            move.source.rename(move.quarantine)
            moved.append(move)

        for root in configured_roots:
            if not root.exists():
                root.mkdir(parents=True, exist_ok=False)
                created_roots.add(root)
        lock_all_integrations(config.data_dir.resolve())
        fresh_database = migrate_live_database(lock)
        fresh_version = await _bootstrap_fresh_database(fresh_database)

        manifest.update(
            status="completed",
            completed_at=dt.datetime.now(dt.UTC).isoformat(),
            fresh_schema_version=fresh_version,
            integrity_check="ok",
        )
        _write_manifest(manifest_path, manifest)
    except BaseException as exc:
        # Delete only paths created by this attempt, then restore established roots in reverse.
        cleanup = set(created_roots)
        cleanup.update(move.source for move in moved)
        for path in sorted(cleanup, key=lambda item: len(item.parts), reverse=True):
            _remove_fresh_root(path)
        for move in reversed(moved):
            if move.source.exists() or not move.quarantine.exists():
                raise DataResetError(
                    "Reset failed and automatic quarantine restore was blocked"
                ) from exc
            move.quarantine.rename(move.source)
        manifest.update(
            status="rolled_back",
            rolled_back_at=dt.datetime.now(dt.UTC).isoformat(),
            error_type=type(exc).__name__,
        )
        _write_manifest(manifest_path, manifest)
        if isinstance(exc, (DataResetError, OwnerLoginThrottledError)):
            raise
        raise DataResetError("Reset failed; the original Kira data was restored") from exc

    return DataResetResult(
        reset_id=reset_id,
        manifest=manifest_path,
        quarantines=tuple(move.quarantine for move in moves),
    )


async def reset_all_data(
    config: Config, password: str, *, include_external_knowledge: bool = False
) -> DataResetResult:
    """Programmatic reset entry point; always enforces exclusive runtime ownership."""
    try:
        with InstanceLock(config.data_dir) as lock:
            return await _reset_with_lock(
                config,
                password,
                lock,
                include_external_knowledge=include_external_knowledge,
            )
    except InstanceAlreadyRunning as exc:
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

    from jarvis.config import ConfigError, load_config

    try:
        config = load_config()
    except ConfigError as exc:
        print(f"Configuration error: {exc}")
        return 1

    lock = InstanceLock(config.data_dir)
    try:
        lock.acquire()
    except InstanceAlreadyRunning as exc:
        print(f"Reset refused: {exc}")
        return 1
    try:
        print("This archives all Kira chats, projects, tasks, knowledge, tokens, and logs.")
        print("Your source checkout, .env, and config are preserved. Nothing is hard-deleted.")
        if input(f"Type {CONFIRMATION_PHRASE} to continue: ") != CONFIRMATION_PHRASE:
            print("Reset cancelled: confirmation phrase did not match.")
            return 1
        data_root = _resolved_root(config.data_dir, must_exist=True)
        knowledge_root = _resolved_root(config.knowledge_dir)
        include_external_knowledge = False
        if knowledge_root.exists() and not knowledge_root.is_relative_to(data_root):
            print(f"External knowledge vault: {knowledge_root}")
            if input("Type that exact path to quarantine this vault: ") != str(knowledge_root):
                print("Reset cancelled: external knowledge path did not match.")
                return 1
            include_external_knowledge = True
        password = getpass.getpass("Current owner password: ")
        result = asyncio.run(
            _reset_with_lock(
                config,
                password,
                lock,
                include_external_knowledge=include_external_knowledge,
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

    print(f"Kira data reset complete. Manifest: {result.manifest}")
    for quarantine in result.quarantines:
        print(f"Quarantine: {quarantine}")
    print("Start Kira, create the new owner login, then reconnect each integration explicitly.")
    return 0
