"""Local backup MVP: consistent DB image, secret exclusion, tamper detection, migration guard."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

import kira.config as config_module
import kira.persistence.backup as backup_module
from kira.cli.backup import backup_cli
from kira.config import ConfigError
from kira.persistence.backup import BackupError, create_backup, verify_backup
from kira.persistence.db import connect
from kira.persistence.instance_lock import ResetBarrier
from kira.persistence.migrations import latest_version

EXCLUDED_PATTERNS = [
    ".env",
    "data/connectors/**",
    "**/*token*",
    "**/*secret*",
    "**/*credential*",
    "logs/**",
]


def _database(path: Path, *, version: int | None = None) -> None:
    db = sqlite3.connect(path)
    try:
        db.execute("CREATE TABLE notes (text TEXT)")
        db.execute("INSERT INTO notes VALUES ('recoverable')")
        if version is not None:
            db.execute(f"PRAGMA user_version = {version}")
        db.commit()
    finally:
        db.close()


def _legacy_v1_backup(root: Path, *, version: int | None = None) -> Path:
    backup = root / "legacy-backup-v1"
    backup.mkdir()
    database = backup / "jarvis.db"
    db_version = latest_version() if version is None else version
    _database(database, version=db_version)
    payload = database.read_bytes()
    manifest = {
        "schema_version": 1,
        "created_at": "2026-07-11T09:25:26+00:00",
        "reason": "manual",
        "app_version": "0.1.0",
        "git_revision": None,
        "database_user_version": db_version,
        "included_roots": ["jarvis.db", "knowledge", "artifacts", "evals/history.jsonl"],
        "excluded_sensitive_paths": EXCLUDED_PATTERNS,
        "files": [
            {
                "path": "jarvis.db",
                "size": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
            }
        ],
    }
    (backup / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return backup


def _manifest(backup: Path) -> dict[str, Any]:
    return json.loads((backup / "manifest.json").read_text(encoding="utf-8"))


def _write_manifest(backup: Path, manifest: object) -> None:
    (backup / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def _snapshot(backup: Path) -> dict[str, tuple[bytes, int]]:
    return {
        path.relative_to(backup).as_posix(): (path.read_bytes(), path.stat().st_mtime_ns)
        for path in backup.rglob("*")
        if path.is_file()
    }


def _symlink_or_skip(link: Path, target: Path, *, directory: bool = False) -> None:
    try:
        link.symlink_to(target, target_is_directory=directory)
    except OSError as exc:
        pytest.skip(f"Symbolic links are unavailable in this environment: {exc}")


def test_legacy_live_database_creates_consistent_kira_v2_backup(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    _database(data / "jarvis.db", version=latest_version())
    (data / "knowledge" / "raw").mkdir(parents=True)
    (data / "knowledge" / "raw" / "note.txt").write_text("vault", encoding="utf-8")
    (data / "artifacts").mkdir()
    (data / "artifacts" / "answer.md").write_text("artifact", encoding="utf-8")
    (data / "evals").mkdir()
    (data / "evals" / "history.jsonl").write_text('{"ok":true}\n', encoding="utf-8")

    backup = create_backup(data)
    manifest = json.loads((backup / "manifest.json").read_text(encoding="utf-8"))

    assert backup.name.startswith("kira-backup-")
    assert manifest["schema_version"] == 2
    assert manifest["format"] == "kira-backup" and manifest["application"] == "Kira"
    assert manifest["database_user_version"] == latest_version()
    assert {item["path"] for item in manifest["files"]} == {
        "kira.db",
        "knowledge/raw/note.txt",
        "artifacts/answer.md",
        "evals/history.jsonl",
    }
    result = verify_backup(backup)
    assert result["schema_version"] == 2 and result["database"] == "kira.db"
    assert result["database_user_version"] == latest_version()
    assert not (backup / "jarvis.db").exists()
    copied_path = (backup / "kira.db").resolve()
    copied = sqlite3.connect(f"{copied_path.as_uri()}?mode=ro&immutable=1", uri=True)
    try:
        assert copied.execute("SELECT text FROM notes").fetchone()[0] == "recoverable"
    finally:
        copied.close()


def test_backup_accepts_canonical_live_database(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    _database(data / "kira.db", version=latest_version())

    backup = create_backup(data)

    assert (backup / "kira.db").is_file()
    assert verify_backup(backup)["database"] == "kira.db"


def test_backup_refuses_ambiguous_live_databases(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    _database(data / "kira.db", version=latest_version())
    _database(data / "jarvis.db", version=latest_version())

    with pytest.raises(BackupError, match="Both Kira and legacy databases exist"):
        create_backup(data)

    assert not (data / "backups").exists()


def test_backup_refuses_orphan_sidecars_from_the_other_database_identity(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    _database(data / "kira.db", version=latest_version())
    orphan = data / "jarvis.db-wal"
    orphan.write_bytes(b"possible-uncheckpointed-legacy-state")

    with pytest.raises(BackupError, match="Orphan database sidecar"):
        create_backup(data)

    assert orphan.read_bytes() == b"possible-uncheckpointed-legacy-state"
    assert not (data / "backups").exists()


@pytest.mark.parametrize(
    "reason",
    ["../escape", "UPPER", "two words", "trailing-", "-leading", "", "a" * 97],
)
def test_backup_refuses_unsafe_reason_without_partial_output(tmp_path: Path, reason: str) -> None:
    data = tmp_path / "data"
    data.mkdir()
    _database(data / "kira.db", version=latest_version())

    with pytest.raises(BackupError, match="reason"):
        create_backup(data, reason=reason)

    assert not (data / "backups").exists()


def test_backup_failure_cleans_owned_temporary_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = tmp_path / "data"
    data.mkdir()
    _database(data / "kira.db", version=latest_version())

    def fail_copy(_source: Path, _destination: Path) -> None:
        raise sqlite3.OperationalError("injected copy failure")

    monkeypatch.setattr(backup_module, "_copy_sqlite", fail_copy)

    with pytest.raises(BackupError, match="could not be created safely"):
        create_backup(data)

    assert (data / "backups").is_dir()
    assert list((data / "backups").iterdir()) == []


def test_backup_does_not_recreate_database_that_vanishes_before_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = tmp_path / "data"
    data.mkdir()
    database = data / "kira.db"
    _database(database, version=latest_version())
    original_copy = backup_module._copy_sqlite

    def remove_then_copy(source: Path, destination: Path) -> None:
        source.unlink()
        original_copy(source, destination)

    monkeypatch.setattr(backup_module, "_copy_sqlite", remove_then_copy)

    with pytest.raises(BackupError, match="could not be created safely"):
        create_backup(data)

    assert not database.exists()
    assert list((data / "backups").iterdir()) == []


def test_backup_records_version_from_copied_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = tmp_path / "data"
    data.mkdir()
    database = data / "kira.db"
    _database(database, version=latest_version())
    copied_version = latest_version() + 1
    original_copy = backup_module._copy_sqlite

    def migrate_then_copy(source: Path, destination: Path) -> None:
        db = sqlite3.connect(source)
        try:
            db.execute(f"PRAGMA user_version = {copied_version}")
            db.commit()
        finally:
            db.close()
        original_copy(source, destination)

    monkeypatch.setattr(backup_module, "_copy_sqlite", migrate_then_copy)

    backup = create_backup(data)

    assert _manifest(backup)["database_user_version"] == copied_version
    assert verify_backup(backup)["database_user_version"] == copied_version


def test_backup_refuses_source_name_its_verifier_cannot_accept(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    _database(data / "kira.db", version=latest_version())
    (data / "artifacts").mkdir()
    (data / "artifacts" / "cafe\u0301.txt").write_text("NFD name", encoding="utf-8")

    with pytest.raises(BackupError, match="non-portable source path"):
        create_backup(data)

    assert list((data / "backups").iterdir()) == []


async def test_backup_captures_a_committed_wal_database(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    live = await connect(data / "kira.db")
    try:
        await live.execute("CREATE TABLE backup_probe (value TEXT NOT NULL)")
        await live.execute("INSERT INTO backup_probe (value) VALUES ('live-wal-row')")
        await live.commit()

        source_paths = [data / "kira.db", data / "kira.db-wal"]
        assert all(path.is_file() for path in source_paths)
        source_before = {
            path.name: (path.read_bytes(), path.stat().st_mtime_ns) for path in source_paths
        }
        backup = create_backup(data)
        source_after = {
            path.name: (path.read_bytes(), path.stat().st_mtime_ns) for path in source_paths
        }
    finally:
        await live.close()

    assert source_after == source_before
    copied_path = (backup / "kira.db").resolve()
    copied = sqlite3.connect(f"{copied_path.as_uri()}?mode=ro&immutable=1", uri=True)
    try:
        assert copied.execute("SELECT value FROM backup_probe").fetchone()[0] == "live-wal-row"
    finally:
        copied.close()
    assert verify_backup(backup)["database"] == "kira.db"
    assert {path.name for path in backup.iterdir()} == {"kira.db", "manifest.json"}


def test_backup_recovers_a_database_left_with_a_dirty_wal(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    database = data / "jarvis.db"
    script = "\n".join(
        [
            "import os, sqlite3, sys",
            "db = sqlite3.connect(sys.argv[1])",
            "db.execute('PRAGMA journal_mode = WAL')",
            "db.execute('PRAGMA wal_autocheckpoint = 0')",
            "db.execute('CREATE TABLE crash_probe (value TEXT NOT NULL)')",
            "db.execute(\"INSERT INTO crash_probe VALUES ('dirty-wal-row')\")",
            "db.execute(f'PRAGMA user_version = {sys.argv[2]}')",
            "db.commit()",
            "os._exit(0)",
        ]
    )
    subprocess.run([sys.executable, "-c", script, str(database), str(latest_version())], check=True)
    wal = data / "jarvis.db-wal"
    assert wal.is_file() and wal.stat().st_size > 0
    (data / "jarvis.db-shm").unlink(missing_ok=True)

    backup = create_backup(data)

    assert verify_backup(backup)["database_user_version"] == latest_version()
    copied = sqlite3.connect(
        f"{(backup / 'kira.db').resolve().as_uri()}?mode=ro&immutable=1", uri=True
    )
    try:
        assert copied.execute("SELECT value FROM crash_probe").fetchone()[0] == "dirty-wal-row"
    finally:
        copied.close()


def test_backup_excludes_connectors_and_sensitive_names(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    _database(data / "kira.db", version=latest_version())
    (data / "connectors").mkdir()
    (data / "connectors" / "google_token.json").write_text("TOKEN-CANARY", encoding="utf-8")
    (data / "artifacts").mkdir()
    (data / "artifacts" / "report.txt").write_text("safe", encoding="utf-8")
    (data / "artifacts" / "api_token.txt").write_text("TOKEN-CANARY", encoding="utf-8")
    (data / "artifacts" / "api_token" / "value.txt").parent.mkdir()
    (data / "artifacts" / "api_token" / "value.txt").write_text(
        "TOKEN-DIRECTORY-CANARY", encoding="utf-8"
    )
    (data / "artifacts" / "credentials" / "auth.json").parent.mkdir()
    (data / "artifacts" / "credentials" / "auth.json").write_text(
        "CREDENTIAL-DIRECTORY-CANARY", encoding="utf-8"
    )
    (data / "knowledge").mkdir()
    (data / "knowledge" / ".env.local").write_text("LOCAL-ENV-CANARY", encoding="utf-8")
    (data / "knowledge" / ".env.production").write_text("PRODUCTION-ENV-CANARY", encoding="utf-8")
    (data / "knowledge" / ".envrc").write_text("ENVRC-CANARY", encoding="utf-8")
    (tmp_path / ".env").write_text("ENV-CANARY=never-copy", encoding="utf-8")

    backup = create_backup(data)

    assert not (backup / "connectors").exists()
    assert not (backup / "artifacts" / "api_token.txt").exists()
    backup_text = "\n".join(
        path.read_text(encoding="utf-8", errors="ignore")
        for path in backup.rglob("*")
        if path.is_file() and path.name != "kira.db"
    )
    assert "TOKEN-CANARY" not in backup_text
    assert "TOKEN-DIRECTORY-CANARY" not in backup_text
    assert "CREDENTIAL-DIRECTORY-CANARY" not in backup_text
    assert "ENV-CANARY=never-copy" not in backup_text
    assert "LOCAL-ENV-CANARY" not in backup_text
    assert "PRODUCTION-ENV-CANARY" not in backup_text
    assert "ENVRC-CANARY" not in backup_text
    assert not (backup / ".env").exists()
    assert not (backup / "knowledge" / ".env.local").exists()
    assert not (backup / "knowledge" / ".env.production").exists()
    assert not (backup / "knowledge" / ".envrc").exists()
    assert not (backup / "artifacts" / "api_token").exists()
    assert not (backup / "artifacts" / "credentials").exists()


def test_verify_detects_tampered_backup_file(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    _database(data / "kira.db", version=latest_version())
    (data / "artifacts").mkdir()
    (data / "artifacts" / "answer.txt").write_text("original", encoding="utf-8")
    backup = create_backup(data)
    (backup / "artifacts" / "answer.txt").write_text("tampered", encoding="utf-8")

    with pytest.raises(BackupError, match="verification failed"):
        verify_backup(backup)


def test_legacy_v1_backup_verifies_read_only_and_is_labeled(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    backup = _legacy_v1_backup(tmp_path)
    before = _snapshot(backup)

    result = verify_backup(backup)
    assert result["schema_version"] == 1
    assert result["format"] == "legacy"
    assert result["database"] == "jarvis.db"
    assert backup_cli(["verify", str(backup)]) == 0

    assert "legacy backup format v1" in capsys.readouterr().out
    assert _snapshot(backup) == before


def test_legacy_v1_preserves_its_historical_filename_contract(tmp_path: Path) -> None:
    backup = _legacy_v1_backup(tmp_path)
    relative = "artifacts/api_token/cafe\u0301.txt"
    historical = backup / relative
    historical.parent.mkdir(parents=True)
    historical.write_text("historical sensitive-directory payload", encoding="utf-8")
    payload = historical.read_bytes()
    manifest = _manifest(backup)
    manifest["files"].append(
        {
            "path": relative,
            "size": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
        }
    )
    manifest["files"].sort(key=lambda item: item["path"])
    _write_manifest(backup, manifest)

    result = verify_backup(backup)

    assert result["schema_version"] == 1
    assert result["files"] == 2


@pytest.mark.parametrize("schema", [None, True, "2", 2.0, 0, 3])
def test_verify_rejects_missing_malformed_or_unknown_schema(tmp_path: Path, schema: object) -> None:
    backup = _legacy_v1_backup(tmp_path)
    manifest = _manifest(backup)
    if schema is None:
        manifest.pop("schema_version")
    else:
        manifest["schema_version"] = schema
    _write_manifest(backup, manifest)

    with pytest.raises(BackupError, match="schema"):
        verify_backup(backup)


def test_verify_rejects_non_object_manifest(tmp_path: Path) -> None:
    backup = _legacy_v1_backup(tmp_path)
    _write_manifest(backup, [])

    with pytest.raises(BackupError, match="JSON object"):
        verify_backup(backup)


def test_verify_rejects_duplicate_manifest_key(tmp_path: Path) -> None:
    backup = _legacy_v1_backup(tmp_path)
    manifest_path = backup / "manifest.json"
    original = manifest_path.read_text(encoding="utf-8")
    manifest_path.write_text(original.replace("{", '{"schema_version": 1,', 1), encoding="utf-8")

    with pytest.raises(BackupError, match="duplicate key"):
        verify_backup(backup)


def test_verify_rejects_non_finite_manifest_number(tmp_path: Path) -> None:
    backup = _legacy_v1_backup(tmp_path)
    manifest_path = backup / "manifest.json"
    original = manifest_path.read_text(encoding="utf-8")
    manifest_path.write_text(
        original.replace(
            f'"database_user_version": {latest_version()}', '"database_user_version": NaN'
        ),
        encoding="utf-8",
    )

    with pytest.raises(BackupError, match="non-finite number"):
        verify_backup(backup)


@pytest.mark.parametrize(
    ("field", "value"),
    [("format", "other-backup"), ("application", "Kairo")],
)
def test_verify_rejects_wrong_kira_identity(tmp_path: Path, field: str, value: str) -> None:
    data = tmp_path / "data"
    data.mkdir()
    _database(data / "kira.db", version=latest_version())
    backup = create_backup(data)
    manifest = _manifest(backup)
    manifest[field] = value
    _write_manifest(backup, manifest)

    with pytest.raises(BackupError, match="format identity"):
        verify_backup(backup)


def test_verify_rejects_kira_fields_mixed_into_legacy_schema(tmp_path: Path) -> None:
    backup = _legacy_v1_backup(tmp_path)
    manifest = _manifest(backup)
    manifest.update({"format": "kira-backup", "application": "Kira"})
    _write_manifest(backup, manifest)

    with pytest.raises(BackupError, match="does not match schema v1"):
        verify_backup(backup)


def test_verify_requires_database_to_be_hash_covered(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    _database(data / "kira.db", version=latest_version())
    (data / "artifacts").mkdir()
    (data / "artifacts" / "answer.txt").write_text("keeps-list-nonempty", encoding="utf-8")
    backup = create_backup(data)
    manifest = _manifest(backup)
    manifest["files"] = [item for item in manifest["files"] if item["path"] != "kira.db"]
    _write_manifest(backup, manifest)

    with pytest.raises(BackupError, match="hash-cover kira.db"):
        verify_backup(backup)


def test_verify_rejects_hash_covered_corrupt_database(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    _database(data / "kira.db", version=latest_version())
    backup = create_backup(data)
    database = backup / "kira.db"
    database.write_bytes(b"not a SQLite database")
    manifest = _manifest(backup)
    database_item = next(item for item in manifest["files"] if item["path"] == "kira.db")
    database_item["size"] = database.stat().st_size
    database_item["sha256"] = hashlib.sha256(database.read_bytes()).hexdigest()
    _write_manifest(backup, manifest)

    with pytest.raises(BackupError, match="database is unreadable"):
        verify_backup(backup)


def test_verify_rejects_unlisted_sensitive_file(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    _database(data / "kira.db", version=latest_version())
    backup = create_backup(data)
    (backup / "artifacts").mkdir()
    (backup / "artifacts" / "api_token.txt").write_text("secret", encoding="utf-8")

    with pytest.raises(BackupError, match="unexpected file"):
        verify_backup(backup)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("size", True),
        ("size", 1.0),
        ("size", -1),
        ("sha256", "A" * 64),
        ("sha256", "0" * 63),
    ],
)
def test_verify_rejects_malformed_file_metadata(tmp_path: Path, field: str, value: object) -> None:
    backup = _legacy_v1_backup(tmp_path)
    manifest = _manifest(backup)
    manifest["files"][0][field] = value
    _write_manifest(backup, manifest)

    with pytest.raises(BackupError, match="invalid file metadata"):
        verify_backup(backup)


@pytest.mark.parametrize(
    "unsafe_path",
    [
        "../outside",
        "C:/outside",
        "artifacts\\file.txt",
        "artifacts//file.txt",
        "artifacts/./file.txt",
        "artifacts/file.txt:stream",
        "artifacts/file?.txt",
        "artifacts/CON",
        "artifacts/file. ",
    ],
)
def test_verify_rejects_unsafe_manifest_paths(tmp_path: Path, unsafe_path: str) -> None:
    data = tmp_path / "data"
    data.mkdir()
    _database(data / "kira.db", version=latest_version())
    backup = create_backup(data)
    manifest_path = backup / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["files"][0]["path"] = unsafe_path
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(BackupError, match="unsafe file path"):
        verify_backup(backup)


@pytest.mark.parametrize("unsafe_path", ["other/file.txt", "artifacts/api_token.txt"])
def test_verify_rejects_disallowed_manifest_roots_and_sensitive_paths(
    tmp_path: Path, unsafe_path: str
) -> None:
    data = tmp_path / "data"
    data.mkdir()
    _database(data / "kira.db", version=latest_version())
    backup = create_backup(data)
    manifest = _manifest(backup)
    manifest["files"][0]["path"] = unsafe_path
    _write_manifest(backup, manifest)

    with pytest.raises(BackupError, match="disallowed file path"):
        verify_backup(backup)


def test_verify_rejects_linked_manifest(tmp_path: Path) -> None:
    backup = _legacy_v1_backup(tmp_path)
    manifest = backup / "manifest.json"
    external = tmp_path / "external-manifest.json"
    external.write_bytes(manifest.read_bytes())
    manifest.unlink()
    _symlink_or_skip(manifest, external)

    with pytest.raises(BackupError, match="missing or linked"):
        verify_backup(backup)


def test_verify_rejects_linked_archive_member(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    _database(data / "kira.db", version=latest_version())
    (data / "artifacts").mkdir()
    (data / "artifacts" / "answer.txt").write_text("original", encoding="utf-8")
    backup = create_backup(data)
    member = backup / "artifacts" / "answer.txt"
    external = tmp_path / "external.txt"
    external.write_bytes(member.read_bytes())
    member.unlink()
    _symlink_or_skip(member, external)

    with pytest.raises(BackupError, match="linked file or directory"):
        verify_backup(backup)


def test_verify_normalizes_inventory_filesystem_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backup = _legacy_v1_backup(tmp_path).resolve()
    original_iterdir = Path.iterdir

    def failing_iterdir(path: Path):
        if path == backup:
            raise OSError("injected inventory failure")
        return original_iterdir(path)

    monkeypatch.setattr(Path, "iterdir", failing_iterdir)

    with pytest.raises(BackupError, match="archive inventory is unreadable"):
        verify_backup(backup)


async def test_connect_creates_pre_migration_snapshot_for_real_older_database(
    tmp_path: Path,
) -> None:
    database = tmp_path / "kira.db"
    _database(database, version=latest_version() - 1)

    db = await connect(database)
    try:
        assert (await (await db.execute("PRAGMA user_version")).fetchone())[0] == latest_version()
    finally:
        await db.close()

    snapshots = list((tmp_path / "backups").glob("kira-backup-*-pre-migration-v*-to-v*"))
    assert len(snapshots) == 1
    result = verify_backup(snapshots[0])
    assert result["database_user_version"] == latest_version() - 1
    assert result["database"] == "kira.db"


def test_backup_cli_verify_is_read_only(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    data = tmp_path / "data"
    data.mkdir()
    _database(data / "kira.db", version=latest_version())
    backup = create_backup(data)
    before = _snapshot(backup)

    assert backup_cli(["verify", str(backup)]) == 0
    output = capsys.readouterr().out
    assert "Kira backup verified:" in output
    assert "Kira backup format v2" in output
    assert _snapshot(backup) == before


def test_backup_cli_reports_configuration_errors_without_traceback(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def fail_config() -> None:
        raise ConfigError("invalid test configuration")

    monkeypatch.setattr(config_module, "load_config", fail_config)

    assert backup_cli(["create"]) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Kira backup configuration error: invalid test configuration" in captured.err


def test_backup_create_barrier_contention_prevents_data_directory_creation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from kira.config import load_config

    config = load_config(root=tmp_path, env_file=None)
    monkeypatch.setattr(config_module, "load_config", lambda **_kwargs: config)
    with ResetBarrier(config.data_dir):
        assert backup_cli(["create"]) == 1

    assert not config.data_dir.exists()
    assert "data-maintenance operation" in capsys.readouterr().err


def test_backup_cli_errors_use_stderr(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert backup_cli(["verify", str(tmp_path / "missing")]) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Kira backup error: Backup directory is missing." in captured.err


def test_backup_cli_help_warns_that_archives_remain_private(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit, match="0"):
        backup_cli(["--help"])

    output = capsys.readouterr().out
    assert "private or secret user-authored content" in output
    assert "restore is not supported" in output


def test_verify_uses_kira_temporary_prefix(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    backup = _legacy_v1_backup(tmp_path)
    prefixes: list[str | None] = []
    original = backup_module.tempfile.TemporaryDirectory

    def tracked_temporary_directory(*args: object, **kwargs: object):
        prefixes.append(kwargs.get("prefix") if isinstance(kwargs.get("prefix"), str) else None)
        return original(*args, **kwargs)

    monkeypatch.setattr(backup_module.tempfile, "TemporaryDirectory", tracked_temporary_directory)

    verify_backup(backup)

    assert prefixes == ["kira-backup-verify-"]
