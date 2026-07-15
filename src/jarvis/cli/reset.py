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
from jarvis.persistence.db import connect
from jarvis.persistence.instance_lock import InstanceAlreadyRunning, InstanceLock
from jarvis.persistence.migrations import latest_version
from jarvis.ui.owner_auth import OwnerAuthService, OwnerLoginThrottledError

CONFIRMATION_PHRASE = "RESET ALL KIRA DATA"
_DATABASE = "jarvis.db"
_COUNT_TABLES = ("owner_accounts", "projects", "sessions", "tasks", "kb_sources")


class DataResetError(RuntimeError):
    """A reset was refused or rolled back without exposing sensitive details."""


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


def _planned_moves(
    config: Config, reset_id: str, *, include_external_knowledge: bool
) -> tuple[list[_RootMove], list[Path]]:
    config_root = config.root.resolve()
    data = _resolved_root(config.data_dir, must_exist=True)
    database = data / _DATABASE
    if not data.is_dir() or not database.is_file():
        raise DataResetError("No initialized Kira database was found to reset")
    if _is_link_like(database) or database.resolve().parent != data:
        raise DataResetError("Refusing a linked database outside the Kira data root")
    _validate_safe_root(data, config_root=config_root)
    manifest_root = data.parent / ".kairo-reset-manifests"
    if manifest_root == data or manifest_root.is_relative_to(data):
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
        if (
            path == manifest_root
            or path.is_relative_to(manifest_root)
            or manifest_root.is_relative_to(path)
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
        quarantine = source.with_name(f".{source.name}.kairo-quarantine-{reset_id}")
        if quarantine.exists():
            raise DataResetError(f"Reset quarantine already exists: {quarantine}")
        moves.append(_RootMove(tuple(sorted(roles)), source, quarantine))
    return moves, list(dict.fromkeys(configured.values()))


def _manifest_path(config: Config, reset_id: str) -> Path:
    return config.data_dir.resolve().parent / ".kairo-reset-manifests" / f"{reset_id}.json"


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


async def _authenticate_old_database(database: Path, password: str) -> tuple[int, dict[str, int]]:
    try:
        db = await connect(database)
    except Exception as exc:
        raise DataResetError("The existing Kira database could not be opened safely") from exc
    try:
        lock = asyncio.Lock()
        auth = OwnerAuthService(db, lock)
        if not await auth.is_enrolled():
            raise DataResetError("Owner setup is incomplete; there is no enrolled owner to verify")
        if not await auth.verify_owner_password(password):
            raise DataResetError("Owner password was not accepted")
        facts = await _database_facts(db)
        await db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        return facts
    finally:
        await db.close()


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
    config: Config, password: str, *, include_external_knowledge: bool = False
) -> DataResetResult:
    now = dt.datetime.now(dt.UTC)
    reset_id = f"{now.strftime('%Y%m%dT%H%M%SZ')}-{secrets.token_hex(4)}"
    moves, configured_roots = _planned_moves(
        config, reset_id, include_external_knowledge=include_external_knowledge
    )
    manifest_path = _manifest_path(config, reset_id)
    old_version, old_counts = await _authenticate_old_database(
        config.data_dir.resolve() / _DATABASE, password
    )
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
        fresh_version = await _bootstrap_fresh_database(config.data_dir.resolve() / _DATABASE)

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
        with InstanceLock(config.data_dir):
            return await _reset_with_lock(
                config, password, include_external_knowledge=include_external_knowledge
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
                include_external_knowledge=include_external_knowledge,
            )
        )
    except (DataResetError, OwnerLoginThrottledError) as exc:
        print(f"Reset refused: {exc}")
        return 1
    except Exception as exc:
        print(
            "Reset failed safely; no established data was deleted "
            f"({type(exc).__name__})."
        )
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
