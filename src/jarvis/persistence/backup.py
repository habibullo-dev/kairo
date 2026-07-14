"""Local, secret-excluding backup and verification primitives.

Backups deliberately cover only recoverable Kairo state under ``data/``. OAuth bearer material,
environment files, configuration, logs, and unknown data roots are never copied. The database does
contain a non-replayable Argon2 owner verifier and digest-only session records, so a backup remains
private user data and must be protected accordingly. SQLite is captured with its online backup API,
so a running Kairo process cannot produce a torn database file. Restore is intentionally out of
scope for this MVP: verification never overwrites live data.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import subprocess
import tempfile
import uuid
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from jarvis import __version__


class BackupError(RuntimeError):
    """A safe, operator-facing backup or verification failure."""


_MANIFEST = "manifest.json"
_DATABASE = "jarvis.db"
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


def _now() -> datetime:
    return datetime.now(UTC)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


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
    name = relative.name.lower()
    return any(part.lower() == "connectors" for part in relative.parts) or any(
        marker in name for marker in _SENSITIVE_NAME_PARTS
    ) or name == ".env" or name == ".envrc" or name.startswith(".env.")


def _copy_file(source: Path, destination: Path, relative: PurePosixPath, files: list[dict]) -> None:
    if source.is_symlink() or _is_sensitive(relative):
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


def _copy_tree(
    source_root: Path, destination_root: Path, *, prefix: str, files: list[dict]
) -> None:
    for source in sorted(source_root.rglob("*")):
        if not source.is_file():
            continue
        relative = PurePosixPath(prefix) / source.relative_to(source_root).as_posix()
        _copy_file(source, destination_root / source.relative_to(source_root), relative, files)


def _copy_sqlite(source: Path, destination: Path) -> None:
    """Create a transactionally consistent database image, including a live WAL database."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    read = sqlite3.connect(str(source))
    write = sqlite3.connect(str(destination))
    try:
        read.backup(write)
    finally:
        write.close()
        read.close()


def existing_database_version(path: Path) -> int | None:
    """Return the version for a real SQLite database, otherwise ``None``.

    A zero-byte/new database is not a migration target and must not create a pointless snapshot.
    """
    if not path.is_file() or path.stat().st_size == 0:
        return None
    try:
        db = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
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


def create_backup(data_dir: Path, *, reason: str = "manual") -> Path:
    """Create an atomic, timestamped state backup below ``data/backups``.

    Only the primary database, knowledge, generated artifacts, and eval history are candidates.
    The returned directory is complete only after the manifest has been written and the temporary
    directory has been atomically renamed into place.
    """
    data_dir = data_dir.resolve()
    database = data_dir / _DATABASE
    version = existing_database_version(database)
    if version is None:
        raise BackupError("No existing Kairo database found; nothing was backed up.")

    backups = data_dir / "backups"
    backups.mkdir(parents=True, exist_ok=True)
    stamp = _now().strftime("%Y%m%dT%H%M%SZ")
    target = backups / f"{stamp}-{reason}-{uuid.uuid4().hex[:8]}"
    temporary = backups / f".{target.name}.tmp"
    files: list[dict] = []
    try:
        temporary.mkdir()
        database_copy = temporary / _DATABASE
        _copy_sqlite(database, database_copy)
        files.append(
            {
                "path": _DATABASE,
                "size": database_copy.stat().st_size,
                "sha256": _sha256(database_copy),
            }
        )

        for name in _INCLUDED_DIRECTORIES:
            source = data_dir / name
            if source.is_dir() and not source.is_symlink():
                _copy_tree(source, temporary / name, prefix=name, files=files)
        history = data_dir / "evals" / "history.jsonl"
        if history.is_file():
            _copy_file(
                history,
                temporary / "evals" / "history.jsonl",
                PurePosixPath("evals/history.jsonl"),
                files,
            )

        manifest = {
            "schema_version": 1,
            "created_at": _now().isoformat(),
            "reason": reason,
            "app_version": __version__,
            "git_revision": _git_revision(data_dir.parent),
            "database_user_version": version,
            "included_roots": [_DATABASE, *_INCLUDED_DIRECTORIES, "evals/history.jsonl"],
            "excluded_sensitive_paths": list(_EXCLUDED_PATTERNS),
            "files": sorted(files, key=lambda item: item["path"]),
        }
        (temporary / _MANIFEST).write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        temporary.replace(target)
        return target
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise


def create_pre_migration_snapshot(
    database: Path, *, current_version: int, target_version: int
) -> Path:
    """Fail closed before a real database is migrated; no snapshot means no migration."""
    if current_version >= target_version:
        raise BackupError("Pre-migration snapshot requested without a pending migration.")
    return create_backup(
        database.parent, reason=f"pre-migration-v{current_version}-to-v{target_version}"
    )


def _safe_member(backup: Path, raw: object) -> Path:
    if not isinstance(raw, str):
        raise BackupError("Backup manifest has an invalid file entry.")
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
    candidate = backup.joinpath(*relative.parts)
    if backup not in candidate.resolve().parents:
        raise BackupError("Backup manifest path escapes its backup directory.")
    return candidate


def verify_backup(backup: Path) -> dict[str, Any]:
    """Verify hashes and open a temporary database copy without touching live state."""
    backup = backup.resolve()
    manifest_path = backup / _MANIFEST
    if not manifest_path.is_file():
        raise BackupError("Backup manifest is missing.")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BackupError("Backup manifest is unreadable.") from exc
    files = manifest.get("files")
    if not isinstance(files, list):
        raise BackupError("Backup manifest has no file list.")
    seen: set[str] = set()
    for item in files:
        if not isinstance(item, dict):
            raise BackupError("Backup manifest has an invalid file entry.")
        path = _safe_member(backup, item.get("path"))
        raw_path = str(item["path"])
        if raw_path in seen or not path.is_file():
            raise BackupError("Backup file is missing or listed twice.")
        seen.add(raw_path)
        if item.get("sha256") != _sha256(path) or item.get("size") != path.stat().st_size:
            raise BackupError(f"Backup verification failed for {raw_path}.")
    database = _safe_member(backup, _DATABASE)
    if not database.is_file():
        raise BackupError("Backup does not contain jarvis.db.")
    with tempfile.TemporaryDirectory(prefix="kairo-backup-verify-") as temporary:
        checked = Path(temporary) / _DATABASE
        shutil.copy2(database, checked)
        db = sqlite3.connect(str(checked))
        try:
            integrity = db.execute("PRAGMA integrity_check").fetchone()[0]
            version = int(db.execute("PRAGMA user_version").fetchone()[0])
        finally:
            db.close()
    if integrity != "ok":
        raise BackupError("Backup database integrity check failed.")
    if version != manifest.get("database_user_version"):
        raise BackupError("Backup database version does not match its manifest.")
    return {"backup": str(backup), "database_user_version": version, "files": len(files)}
