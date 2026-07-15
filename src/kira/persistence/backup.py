"""Local Kira backup and verification primitives.

Backups cover recoverable Kira state under ``data/`` while excluding known credential stores,
configuration, logs, and sensitive filenames. User-authored content can still contain private or
secret material, so every archive remains private user data and must be protected accordingly.
SQLite is captured with its online backup API, so a running Kira process cannot produce a torn
database file. Restore is intentionally out of scope: verification never overwrites live data.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import sqlite3
import subprocess
import tempfile
import unicodedata
import uuid
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from kira import __version__
from kira.persistence.database_identity import (
    DATABASE_FILENAME,
    LEGACY_DATABASE_FILENAME,
    DatabaseIdentityError,
    select_database,
)


class BackupError(RuntimeError):
    """A safe, operator-facing backup or verification failure."""


_MANIFEST = "manifest.json"
_SCHEMA_VERSION = 2
_FORMAT = "kira-backup"
_APPLICATION = "Kira"
_LEGACY_DATABASE = LEGACY_DATABASE_FILENAME
_BACKUP_DATABASE = DATABASE_FILENAME
_INCLUDED_DIRECTORIES = ("knowledge", "artifacts")
_EXCLUDED_PATTERNS = (
    ".env",
    "data/connectors/**",
    "**/*token*",
    "**/*secret*",
    "**/*credential*",
    "logs/**",
)
_SENSITIVE_NAME_PARTS = ("token", "secret", "credential", "api_key", "apikey")
_REASON = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_COMMON_MANIFEST_KEYS = {
    "schema_version",
    "created_at",
    "reason",
    "app_version",
    "git_revision",
    "database_user_version",
    "included_roots",
    "excluded_sensitive_paths",
    "files",
}
_V1_MANIFEST_KEYS = _COMMON_MANIFEST_KEYS
_V2_MANIFEST_KEYS = _COMMON_MANIFEST_KEYS | {"format", "application"}
_FILE_KEYS = {"path", "size", "sha256"}
_WINDOWS_RESERVED_CHARS = frozenset(
    {chr(value) for value in range(32)} | {'"', "*", ":", "<", ">", "?", "|", "/", "\\"}
)
_WINDOWS_RESERVED_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"}
    | {f"COM{suffix}" for suffix in "123456789¹²³"}
    | {f"LPT{suffix}" for suffix in "123456789¹²³"}
)


def _now() -> datetime:
    return datetime.now(UTC)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _is_link_like(path: Path) -> bool:
    junction = getattr(path, "is_junction", None)
    return path.is_symlink() or bool(junction and junction())


def _is_windows_reserved(name: str) -> bool:
    """Mirror Python 3.13's Windows-name check while retaining Python 3.12 support."""
    if name[-1:] in (".", " "):
        return name not in (".", "..")
    if _WINDOWS_RESERVED_CHARS.intersection(name):
        return True
    return name.partition(".")[0].rstrip(" ").upper() in _WINDOWS_RESERVED_NAMES


def _validated_reason(reason: str) -> str:
    if not isinstance(reason, str) or len(reason) > 96 or _REASON.fullmatch(reason) is None:
        raise BackupError("Backup reason must be a lowercase, hyphen-separated label.")
    return reason


def _git_revision(root: Path) -> str | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            capture_output=True,
            check=False,
            encoding="utf-8",
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def _is_sensitive(relative: PurePosixPath) -> bool:
    for part in relative.parts:
        name = part.lower()
        if (
            name == "connectors"
            or any(marker in name for marker in _SENSITIVE_NAME_PARTS)
            or name == ".env"
            or name == ".envrc"
            or name.startswith(".env.")
        ):
            return True
    return False


def _copy_file(source: Path, destination: Path, relative: PurePosixPath, files: list[dict]) -> None:
    if _is_link_like(source):
        raise BackupError("Kira backup refused a linked source path.")
    if _is_sensitive(relative):
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)
    files.append(
        {
            "path": relative.as_posix(),
            "size": destination.stat().st_size,
            "sha256": _sha256(destination),
        }
    )


def _register_created_path(relative: PurePosixPath, portable_paths: set[str]) -> None:
    try:
        _checked, portable_key = _portable_relative(relative.as_posix())
    except BackupError as exc:
        raise BackupError("Kira backup refused a non-portable source path.") from exc
    if portable_key in portable_paths:
        raise BackupError("Kira backup refused a portable-path collision.")
    portable_paths.add(portable_key)


def _copy_tree(
    source_root: Path,
    destination_root: Path,
    *,
    prefix: str,
    files: list[dict],
    portable_paths: set[str],
) -> None:
    pending = [source_root]
    while pending:
        directory = pending.pop()
        if _is_link_like(directory) or not directory.is_dir():
            raise BackupError("Kira backup source directory changed during creation.")
        for source in sorted(directory.iterdir(), key=lambda path: path.name):
            if _is_link_like(source):
                raise BackupError("Kira backup refused a linked source path.")
            relative_source = source.relative_to(source_root)
            relative = PurePosixPath(prefix) / relative_source.as_posix()
            if _is_sensitive(relative):
                continue
            if source.is_dir():
                pending.append(source)
                continue
            if not source.is_file():
                raise BackupError("Kira backup refused a special source file.")
            _register_created_path(relative, portable_paths)
            _copy_file(source, destination_root / relative_source, relative, files)


def _copy_sqlite(source: Path, destination: Path) -> None:
    """Create a transactionally consistent database image, including a live WAL database."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    read = _connect_existing_database(source)
    try:
        write = sqlite3.connect(str(destination))
        try:
            read.backup(write)
        finally:
            write.close()
    finally:
        read.close()


def _connect_existing_database(path: Path) -> sqlite3.Connection:
    """Open without create semantics, recovering a dirty WAL only when read-only open fails."""
    uri = path.resolve().as_uri()
    try:
        return sqlite3.connect(f"{uri}?mode=ro", uri=True)
    except sqlite3.OperationalError:
        return sqlite3.connect(f"{uri}?mode=rw", uri=True)


def _validated_database_copy_version(path: Path) -> int:
    try:
        db = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro&immutable=1", uri=True)
        try:
            integrity_row = db.execute("PRAGMA integrity_check").fetchone()
            version_row = db.execute("PRAGMA user_version").fetchone()
        finally:
            db.close()
    except sqlite3.Error as exc:
        raise BackupError("Kira backup database copy is invalid.") from exc
    if not integrity_row or integrity_row[0] != "ok" or not version_row:
        raise BackupError("Kira backup database copy is invalid.")
    return int(version_row[0])


def existing_database_version(path: Path) -> int | None:
    """Return the version for a real SQLite database, otherwise ``None``.

    A zero-byte/new database is not a migration target and must not create a pointless snapshot.
    """
    if not path.is_file() or path.stat().st_size == 0:
        return None
    try:
        db = _connect_existing_database(path)
        try:
            version = int(db.execute("PRAGMA user_version").fetchone()[0])
            tables = int(
                db.execute("SELECT COUNT(*) FROM sqlite_master WHERE type = 'table'").fetchone()[0]
            )
            return version if version > 0 or tables > 0 else None
        finally:
            db.close()
    except sqlite3.Error:
        return None


def _select_live_database(data_dir: Path) -> Path:
    try:
        database = select_database(data_dir)
    except DatabaseIdentityError as exc:
        raise BackupError(str(exc)) from exc
    if not database.exists():
        raise BackupError("No existing Kira database found; nothing was backed up.")
    return database


def _create_backup(data_dir: Path, database: Path, *, reason: str) -> Path:
    data_dir = data_dir.resolve()
    reason = _validated_reason(reason)
    if _is_link_like(database):
        raise BackupError("Kira backup refused a linked database.")
    resolved_database = database.resolve()
    if resolved_database.parent != data_dir:
        raise BackupError("Kira backup database must be inside the configured data directory.")
    if not database.is_file() or database.stat().st_size == 0:
        raise BackupError("No existing Kira database found; nothing was backed up.")

    backups = data_dir / "backups"
    if _is_link_like(backups):
        raise BackupError("Kira backup refused a linked backup directory.")
    try:
        backups.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise BackupError("Kira backup directory could not be prepared.") from exc
    if not backups.is_dir() or _is_link_like(backups):
        raise BackupError("Kira backup directory is unavailable.")

    created_at = _now()
    stamp = created_at.strftime("%Y%m%dT%H%M%SZ")
    target = backups / f"kira-backup-{stamp}-{reason}-{uuid.uuid4().hex}"
    temporary: Path | None = None
    files: list[dict] = []
    portable_paths = {_BACKUP_DATABASE.casefold()}
    try:
        temporary = Path(tempfile.mkdtemp(prefix=f".{target.name}.tmp-", dir=backups))
        if temporary.parent != backups or _is_link_like(temporary):
            raise BackupError("Kira backup temporary directory is unsafe.")
        database_copy = temporary / _BACKUP_DATABASE
        _copy_sqlite(database, database_copy)
        version = _validated_database_copy_version(database_copy)
        files.append(
            {
                "path": _BACKUP_DATABASE,
                "size": database_copy.stat().st_size,
                "sha256": _sha256(database_copy),
            }
        )

        for name in _INCLUDED_DIRECTORIES:
            source = data_dir / name
            if _is_link_like(source):
                raise BackupError("Kira backup refused a linked source directory.")
            if source.exists() and not source.is_dir():
                raise BackupError("Kira backup refused a non-directory state root.")
            if source.is_dir():
                _copy_tree(
                    source,
                    temporary / name,
                    prefix=name,
                    files=files,
                    portable_paths=portable_paths,
                )
        evals = data_dir / "evals"
        if _is_link_like(evals):
            raise BackupError("Kira backup refused a linked eval history root.")
        if evals.exists() and not evals.is_dir():
            raise BackupError("Kira backup refused a non-directory eval history root.")
        history = evals / "history.jsonl"
        if _is_link_like(history):
            raise BackupError("Kira backup refused a linked eval history file.")
        if history.exists() and not history.is_file():
            raise BackupError("Kira backup refused a special eval history file.")
        if history.is_file():
            _register_created_path(PurePosixPath("evals/history.jsonl"), portable_paths)
            _copy_file(
                history,
                temporary / "evals" / "history.jsonl",
                PurePosixPath("evals/history.jsonl"),
                files,
            )

        manifest = {
            "schema_version": _SCHEMA_VERSION,
            "format": _FORMAT,
            "application": _APPLICATION,
            "created_at": created_at.isoformat(),
            "reason": reason,
            "app_version": __version__,
            "git_revision": _git_revision(data_dir.parent),
            "database_user_version": version,
            "included_roots": [
                _BACKUP_DATABASE,
                *_INCLUDED_DIRECTORIES,
                "evals/history.jsonl",
            ],
            "excluded_sensitive_paths": list(_EXCLUDED_PATTERNS),
            "files": sorted(files, key=lambda item: item["path"]),
        }
        (temporary / _MANIFEST).write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        if target.exists() or _is_link_like(target):
            raise BackupError("Kira backup target already exists.")
        temporary.rename(target)
        temporary = None
        return target
    except BackupError:
        raise
    except (OSError, sqlite3.Error) as exc:
        raise BackupError("Kira backup could not be created safely.") from exc
    finally:
        if temporary is not None and temporary.parent == backups:
            shutil.rmtree(temporary, ignore_errors=True)


def create_backup(data_dir: Path, *, reason: str = "manual") -> Path:
    """Create an atomic Kira-format backup below ``data/backups``.

    Manual creation accepts one canonical or legacy live database during the transition, but
    refuses an ambiguous two-database state. New archives always contain ``kira.db``.
    """
    data_dir = data_dir.resolve()
    return _create_backup(data_dir, _select_live_database(data_dir), reason=reason)


def create_pre_migration_snapshot(
    database: Path, *, current_version: int, target_version: int
) -> Path:
    """Fail closed before a real database is migrated; no snapshot means no migration."""
    if current_version >= target_version:
        raise BackupError("Pre-migration snapshot requested without a pending migration.")
    return _create_backup(
        database.parent.resolve(),
        database,
        reason=f"pre-migration-v{current_version}-to-v{target_version}",
    )


def _json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise BackupError("Backup manifest contains a duplicate key.")
        result[key] = value
    return result


def _json_constant(_value: str) -> None:
    raise BackupError("Backup manifest contains a non-finite number.")


def _manifest_contract(manifest: object) -> tuple[dict[str, Any], int, str]:
    if not isinstance(manifest, dict):
        raise BackupError("Backup manifest must be a JSON object.")
    schema = manifest.get("schema_version")
    if type(schema) is not int or schema not in (1, _SCHEMA_VERSION):
        raise BackupError("Backup manifest schema is unsupported.")
    expected_keys = _V1_MANIFEST_KEYS if schema == 1 else _V2_MANIFEST_KEYS
    if set(manifest) != expected_keys:
        raise BackupError(f"Backup manifest does not match schema v{schema}.")
    if schema == _SCHEMA_VERSION and (
        manifest.get("format") != _FORMAT or manifest.get("application") != _APPLICATION
    ):
        raise BackupError("Backup manifest has the wrong Kira format identity.")

    created_at = manifest.get("created_at")
    try:
        created = datetime.fromisoformat(created_at) if isinstance(created_at, str) else None
    except ValueError as exc:
        raise BackupError("Backup manifest has an invalid creation time.") from exc
    if created is None or created.tzinfo is None:
        raise BackupError("Backup manifest has an invalid creation time.")

    reason = manifest.get("reason")
    if (
        not isinstance(reason, str)
        or not reason
        or len(reason) > 256
        or any(ord(char) < 32 for char in reason)
    ):
        raise BackupError("Backup manifest has an invalid reason.")
    if schema == _SCHEMA_VERSION:
        _validated_reason(reason)
    app_version = manifest.get("app_version")
    if not isinstance(app_version, str) or not app_version:
        raise BackupError("Backup manifest has an invalid application version.")
    git_revision = manifest.get("git_revision")
    if git_revision is not None and (not isinstance(git_revision, str) or not git_revision):
        raise BackupError("Backup manifest has an invalid Git revision.")
    database_version = manifest.get("database_user_version")
    if type(database_version) is not int or database_version < 0:
        raise BackupError("Backup manifest has an invalid database version.")

    database_name = _LEGACY_DATABASE if schema == 1 else _BACKUP_DATABASE
    expected_roots = [database_name, *_INCLUDED_DIRECTORIES, "evals/history.jsonl"]
    if manifest.get("included_roots") != expected_roots:
        raise BackupError("Backup manifest has an invalid included-root contract.")
    if manifest.get("excluded_sensitive_paths") != list(_EXCLUDED_PATTERNS):
        raise BackupError("Backup manifest has an invalid exclusion contract.")
    files = manifest.get("files")
    if not isinstance(files, list) or not files or len(files) > 100_000:
        raise BackupError("Backup manifest has an invalid file list.")
    return manifest, schema, database_name


def _portable_relative(raw: object) -> tuple[PurePosixPath, str]:
    if not isinstance(raw, str) or not raw or raw != unicodedata.normalize("NFC", raw):
        raise BackupError("Backup manifest contains an unsafe file path.")
    relative = PurePosixPath(raw)
    windows = PureWindowsPath(raw)
    if (
        "\\" in raw
        or relative.is_absolute()
        or windows.is_absolute()
        or bool(windows.drive)
        or not relative.parts
        or relative.as_posix() != raw
    ):
        raise BackupError("Backup manifest contains an unsafe file path.")
    for part in relative.parts:
        if (
            part in (".", "..")
            or ":" in part
            or part.endswith((" ", "."))
            or any(ord(char) < 32 for char in part)
            or _is_windows_reserved(part)
        ):
            raise BackupError("Backup manifest contains an unsafe file path.")
    return relative, raw.casefold()


def _legacy_relative(raw: object) -> tuple[PurePosixPath, str]:
    """Preserve the path contract used by the schema-v1 writer and verifier."""
    if not isinstance(raw, str) or not raw:
        raise BackupError("Backup manifest contains an unsafe file path.")
    relative = PurePosixPath(raw)
    windows = PureWindowsPath(raw)
    if (
        relative.is_absolute()
        or windows.is_absolute()
        or ".." in relative.parts
        or ".." in windows.parts
        or not relative.parts
    ):
        raise BackupError("Backup manifest contains an unsafe file path.")
    return relative, raw


def _relative_contract(raw: object, *, schema: int) -> tuple[PurePosixPath, str]:
    return _legacy_relative(raw) if schema == 1 else _portable_relative(raw)


def _member_is_allowed(relative: PurePosixPath, database_name: str) -> bool:
    raw = relative.as_posix()
    if raw == database_name or raw == "evals/history.jsonl":
        return True
    return len(relative.parts) > 1 and relative.parts[0] in _INCLUDED_DIRECTORIES


def _directory_is_allowed(relative: PurePosixPath) -> bool:
    if not relative.parts:
        return True
    if relative.parts[0] in _INCLUDED_DIRECTORIES:
        return True
    return relative.parts == ("evals",)


def _safe_member(
    backup: Path, raw: object, *, database_name: str, schema: int
) -> tuple[Path, PurePosixPath, str]:
    relative, portable_key = _relative_contract(raw, schema=schema)
    if not _member_is_allowed(relative, database_name) or (schema != 1 and _is_sensitive(relative)):
        raise BackupError("Backup manifest contains a disallowed file path.")
    candidate = backup.joinpath(*relative.parts)
    cursor = backup
    for part in relative.parts:
        cursor /= part
        if _is_link_like(cursor):
            raise BackupError("Backup contains a linked file or directory.")
    if backup not in candidate.resolve().parents:
        raise BackupError("Backup manifest path escapes its backup directory.")
    return candidate, relative, portable_key


def _archive_inventory(backup: Path, database_name: str, *, schema: int) -> set[str]:
    inventory: set[str] = set()
    pending = [backup]
    while pending:
        directory = pending.pop()
        if _is_link_like(directory) or not directory.is_dir():
            raise BackupError("Backup directory changed during verification.")
        for candidate in sorted(directory.iterdir(), key=lambda path: path.name):
            if _is_link_like(candidate):
                raise BackupError("Backup contains a linked file or directory.")
            relative = PurePosixPath(candidate.relative_to(backup).as_posix())
            if candidate.is_dir():
                if not _directory_is_allowed(relative):
                    raise BackupError("Backup contains an unexpected directory.")
                pending.append(candidate)
                continue
            if not candidate.is_file():
                raise BackupError("Backup contains a special file.")
            if relative.as_posix() == _MANIFEST:
                continue
            checked_relative, portable_key = _relative_contract(relative.as_posix(), schema=schema)
            if not _member_is_allowed(checked_relative, database_name) or (
                schema != 1 and _is_sensitive(checked_relative)
            ):
                raise BackupError("Backup contains an unexpected file.")
            if portable_key in inventory:
                raise BackupError("Backup contains a portable-path collision.")
            inventory.add(portable_key)
    return inventory


def _stable_file_hash(path: Path, *, raw_path: str, size: int, sha256: str) -> None:
    try:
        if _is_link_like(path):
            raise BackupError("Backup contains a linked file or directory.")
        before = path.stat(follow_symlinks=False)
        digest = _sha256(path)
        after = path.stat(follow_symlinks=False)
        if _is_link_like(path):
            raise BackupError("Backup contains a linked file or directory.")
    except OSError as exc:
        raise BackupError(f"Backup file is unreadable: {raw_path}.") from exc

    def identity(value):
        return (
            value.st_dev,
            value.st_ino,
            value.st_size,
            value.st_mtime_ns,
            value.st_ctime_ns,
        )

    if identity(before) != identity(after):
        raise BackupError(f"Backup file changed during verification: {raw_path}.")
    if before.st_size != size or digest != sha256:
        raise BackupError(f"Backup verification failed for {raw_path}.")


def verify_backup(backup: Path) -> dict[str, Any]:
    """Verify one strict Kira-v2 or legacy-v1 archive without touching live state."""
    if _is_link_like(backup):
        raise BackupError("Backup directory must not be a link.")
    backup = backup.resolve()
    if not backup.is_dir():
        raise BackupError("Backup directory is missing.")
    manifest_path = backup / _MANIFEST
    if _is_link_like(manifest_path) or not manifest_path.is_file():
        raise BackupError("Backup manifest is missing or linked.")
    try:
        if manifest_path.stat().st_size > 8 * 1024 * 1024:
            raise BackupError("Backup manifest is too large.")
        manifest_object = json.loads(
            manifest_path.read_text(encoding="utf-8"),
            object_pairs_hook=_json_object,
            parse_constant=_json_constant,
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise BackupError("Backup manifest is unreadable.") from exc
    manifest, schema, database_name = _manifest_contract(manifest_object)
    files = manifest["files"]
    try:
        inventory = _archive_inventory(backup, database_name, schema=schema)
    except BackupError:
        raise
    except (OSError, RuntimeError) as exc:
        raise BackupError("Backup archive inventory is unreadable.") from exc

    seen: set[str] = set()
    raw_paths: list[str] = []
    database_item: dict[str, Any] | None = None
    database: Path | None = None
    for item in files:
        if not isinstance(item, dict) or set(item) != _FILE_KEYS:
            raise BackupError("Backup manifest has an invalid file entry.")
        raw_path = item.get("path")
        try:
            path, relative, portable_key = _safe_member(
                backup, raw_path, database_name=database_name, schema=schema
            )
        except BackupError:
            raise
        except (OSError, RuntimeError) as exc:
            raise BackupError("Backup member path could not be resolved safely.") from exc
        if portable_key in seen or not path.is_file():
            raise BackupError("Backup file is missing or listed twice.")
        size = item.get("size")
        sha256 = item.get("sha256")
        valid_sha256 = isinstance(sha256, str) and _SHA256.fullmatch(sha256) is not None
        if type(size) is not int or size < 0 or not valid_sha256:
            raise BackupError("Backup manifest has invalid file metadata.")
        raw_path = relative.as_posix()
        _stable_file_hash(path, raw_path=raw_path, size=size, sha256=sha256)
        seen.add(portable_key)
        raw_paths.append(raw_path)
        if raw_path == database_name:
            database = path
            database_item = item

    if raw_paths != sorted(raw_paths):
        raise BackupError("Backup manifest file list is not canonical.")
    if database is None or database_item is None:
        raise BackupError(f"Backup manifest does not hash-cover {database_name}.")
    if seen != inventory:
        raise BackupError("Backup manifest inventory does not match the archive.")

    try:
        with tempfile.TemporaryDirectory(prefix="kira-backup-verify-") as temporary:
            checked = Path(temporary) / database_name
            shutil.copy2(database, checked)
            _stable_file_hash(
                checked,
                raw_path=database_name,
                size=database_item["size"],
                sha256=database_item["sha256"],
            )
            db = sqlite3.connect(str(checked))
            try:
                integrity_row = db.execute("PRAGMA integrity_check").fetchone()
                version_row = db.execute("PRAGMA user_version").fetchone()
            finally:
                db.close()
    except (OSError, sqlite3.Error) as exc:
        raise BackupError("Backup database is unreadable.") from exc
    integrity = integrity_row[0] if integrity_row else None
    version = int(version_row[0]) if version_row else -1
    if integrity != "ok":
        raise BackupError("Backup database integrity check failed.")
    if version != manifest["database_user_version"]:
        raise BackupError("Backup database version does not match its manifest.")
    return {
        "backup": str(backup),
        "schema_version": schema,
        "format": "legacy" if schema == 1 else _FORMAT,
        "database": database_name,
        "database_user_version": version,
        "files": len(files),
    }
