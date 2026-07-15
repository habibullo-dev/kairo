"""``kira doctor`` — a local, read-only first-run diagnostic.

It intentionally checks only configuration presence, installed Python extras, an existing
SQLite database, and local disk headroom.  It never creates directories, migrates a database,
constructs a client, contacts a provider, tests a connector, or prints a secret value.
"""

from __future__ import annotations

import argparse
import importlib.util
import shutil
import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING

import yaml

from jarvis.config import ConfigError, Secrets, load_config
from jarvis.persistence.database_identity import (
    DATABASE_FILENAME,
    LEGACY_DATABASE_FILENAME,
    DatabaseIdentityError,
    select_database,
)
from jarvis.persistence.migrations import latest_version

if TYPE_CHECKING:
    from jarvis.config import Config


_EXTRAS: dict[str, tuple[str, ...]] = {
    "UI": ("fastapi", "uvicorn", "multipart"),
    "Voice": ("openai", "elevenlabs", "sounddevice"),
    "Browser": ("playwright",),
    "Docling": ("docling",),
}
_SECRET_FIELDS = tuple(Secrets.model_fields)


def _format_bytes(value: int) -> str:
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{value} B"
        value /= 1024
    raise AssertionError("unreachable")


def _existing_ancestor(path: Path) -> Path:
    """Find a disk-usage target without creating the requested data directory."""
    candidate = path
    while not candidate.exists() and candidate != candidate.parent:
        candidate = candidate.parent
    return candidate


def _report_credentials(config: Config, *, emit=print) -> bool:
    """Print secret *names* plus presence; return whether the ordinary REPL key is present."""
    emit("Credentials (presence only):")
    for field in _SECRET_FIELDS:
        env_name = field.upper()
        state = "present" if getattr(config.secrets, field) else "missing"
        emit(f"  {env_name}: {state}")
    return bool(config.secrets.anthropic_api_key)


def _report_extras(*, emit=print) -> None:
    emit("Optional Python extras (reported only):")
    for label, modules in _EXTRAS.items():
        missing = [module for module in modules if importlib.util.find_spec(module) is None]
        if missing:
            emit(f"  {label}: missing {', '.join(missing)}")
        else:
            emit(f"  {label}: installed")


def _report_database(path: Path, *, emit=print) -> bool:
    """Inspect immutable main-file state without creating SQLite WAL/SHM sidecars."""
    if not path.is_file():
        state = "not created" if not path.exists() else "not a regular file"
        emit(f"Database: {state} ({path})")
        return False
    try:
        for suffix in ("-wal", "-journal"):
            recovery = path.with_name(f"{path.name}{suffix}")
            if recovery.exists() and recovery.stat().st_size > 0:
                emit(
                    "Database: active or pending recovery state; stop Kira before the "
                    "no-change integrity check"
                )
                return False
        db = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro&immutable=1", uri=True)
        try:
            version = int(db.execute("PRAGMA user_version").fetchone()[0])
            integrity = [row[0] for row in db.execute("PRAGMA integrity_check").fetchall()]
        finally:
            db.close()
    except (OSError, sqlite3.Error):
        emit("Database: unreadable or corrupt (no change made)")
        return False

    target = latest_version()
    if version == target:
        schema = f"schema v{version} (current)"
    elif version < target:
        schema = f"schema v{version} (needs migration to v{target})"
    else:
        schema = f"schema v{version} (newer than this build's v{target})"
    integrity_ok = integrity == ["ok"]
    emit(f"Database: {schema}; integrity {'ok' if integrity_ok else 'failed'}")
    return version == target and integrity_ok


def _report_disk(path: Path, *, emit=print) -> bool:
    ancestor = _existing_ancestor(path)
    try:
        usage = shutil.disk_usage(ancestor)
    except OSError:
        emit("Disk headroom: unavailable")
        return False
    emit(
        f"Disk headroom ({ancestor}): {_format_bytes(usage.free)} free of "
        f"{_format_bytes(usage.total)}"
    )
    return True


def doctor_cli(argv: list[str], *, root: Path | None = None, emit=print) -> int:
    """Run the safe local diagnostic. ``1`` means setup/action is needed; ``2`` is bad config."""
    parser = argparse.ArgumentParser(
        prog="kira doctor", description="Read-only local configuration and health diagnostic."
    )
    parser.parse_args(argv)
    try:
        config = load_config(root=root)
    except (ConfigError, yaml.YAMLError) as exc:
        kind = "invalid YAML" if isinstance(exc, yaml.YAMLError) else "invalid configuration"
        # Parser/validation text can echo an offending setting value.  Keep this status command's
        # no-secret-value promise by naming the file and error class, never the raw exception.
        emit(f"Configuration error: {kind} in config/settings.yaml (details redacted).")
        return 2

    emit("Kira doctor (read-only; no network requests or local changes):")
    credentials_ready = _report_credentials(config, emit=emit)
    _report_extras(emit=emit)
    try:
        database = select_database(config.data_dir)
    except DatabaseIdentityError as exc:
        emit(f"Database identity: blocked ({exc})")
        database_ready = False
    else:
        if database.name == LEGACY_DATABASE_FILENAME:
            emit(
                f"Database identity: legacy {LEGACY_DATABASE_FILENAME} "
                f"(migrates to {DATABASE_FILENAME} on exclusive startup)"
            )
        elif database.exists():
            emit(f"Database identity: canonical {DATABASE_FILENAME}")
        else:
            emit(f"Database identity: not created (next database: {DATABASE_FILENAME})")
        database_ready = _report_database(database, emit=emit)
    disk_ready = _report_disk(config.data_dir, emit=emit)
    if credentials_ready and database_ready and disk_ready:
        emit("Doctor: ready.")
        return 0
    emit("Doctor: setup or repair is needed; no changes were made.")
    return 1
