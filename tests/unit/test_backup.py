"""Local backup MVP: consistent DB image, secret exclusion, tamper detection, migration guard."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from jarvis.cli.backup import backup_cli
from jarvis.persistence.backup import BackupError, create_backup, verify_backup
from jarvis.persistence.db import connect
from jarvis.persistence.migrations import latest_version


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


def test_backup_manifest_and_consistent_sqlite_copy(tmp_path: Path) -> None:
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

    assert manifest["database_user_version"] == latest_version()
    assert {item["path"] for item in manifest["files"]} == {
        "jarvis.db", "knowledge/raw/note.txt", "artifacts/answer.md", "evals/history.jsonl"
    }
    assert verify_backup(backup)["database_user_version"] == latest_version()
    copied = sqlite3.connect(backup / "jarvis.db")
    try:
        assert copied.execute("SELECT text FROM notes").fetchone()[0] == "recoverable"
    finally:
        copied.close()


def test_backup_excludes_connectors_and_sensitive_names(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    _database(data / "jarvis.db", version=latest_version())
    (data / "connectors").mkdir()
    (data / "connectors" / "google_token.json").write_text("TOKEN-CANARY", encoding="utf-8")
    (data / "artifacts").mkdir()
    (data / "artifacts" / "report.txt").write_text("safe", encoding="utf-8")
    (data / "artifacts" / "api_token.txt").write_text("TOKEN-CANARY", encoding="utf-8")
    (data / "knowledge").mkdir()
    (data / "knowledge" / ".env.local").write_text("LOCAL-ENV-CANARY", encoding="utf-8")
    (data / "knowledge" / ".env.production").write_text(
        "PRODUCTION-ENV-CANARY", encoding="utf-8"
    )
    (data / "knowledge" / ".envrc").write_text("ENVRC-CANARY", encoding="utf-8")
    (tmp_path / ".env").write_text("ENV-CANARY=never-copy", encoding="utf-8")

    backup = create_backup(data)

    assert not (backup / "connectors").exists()
    assert not (backup / "artifacts" / "api_token.txt").exists()
    backup_text = "\n".join(
        path.read_text(encoding="utf-8", errors="ignore")
        for path in backup.rglob("*")
        if path.is_file() and path.name != "jarvis.db"
    )
    assert "TOKEN-CANARY" not in backup_text
    assert "ENV-CANARY=never-copy" not in backup_text
    assert "LOCAL-ENV-CANARY" not in backup_text
    assert "PRODUCTION-ENV-CANARY" not in backup_text
    assert "ENVRC-CANARY" not in backup_text
    assert not (backup / ".env").exists()
    assert not (backup / "knowledge" / ".env.local").exists()
    assert not (backup / "knowledge" / ".env.production").exists()
    assert not (backup / "knowledge" / ".envrc").exists()


def test_verify_detects_tampered_backup_file(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    _database(data / "jarvis.db", version=latest_version())
    (data / "artifacts").mkdir()
    (data / "artifacts" / "answer.txt").write_text("original", encoding="utf-8")
    backup = create_backup(data)
    (backup / "artifacts" / "answer.txt").write_text("tampered", encoding="utf-8")

    with pytest.raises(BackupError, match="verification failed"):
        verify_backup(backup)


@pytest.mark.parametrize("unsafe_path", ["../outside", "C:/outside"])
def test_verify_rejects_unsafe_manifest_paths(tmp_path: Path, unsafe_path: str) -> None:
    data = tmp_path / "data"
    data.mkdir()
    _database(data / "jarvis.db", version=latest_version())
    backup = create_backup(data)
    manifest_path = backup / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["files"][0]["path"] = unsafe_path
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(BackupError, match="unsafe file path"):
        verify_backup(backup)


async def test_connect_creates_pre_migration_snapshot_for_real_older_database(
    tmp_path: Path,
) -> None:
    database = tmp_path / "jarvis.db"
    _database(database, version=latest_version() - 1)

    db = await connect(database)
    try:
        assert (await (await db.execute("PRAGMA user_version")).fetchone())[0] == latest_version()
    finally:
        await db.close()

    snapshots = list((tmp_path / "backups").glob("*-pre-migration-v*-to-v*"))
    assert len(snapshots) == 1
    assert verify_backup(snapshots[0])["database_user_version"] == latest_version() - 1


def test_backup_cli_verify_is_read_only(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    data = tmp_path / "data"
    data.mkdir()
    _database(data / "jarvis.db", version=latest_version())
    backup = create_backup(data)

    assert backup_cli(["verify", str(backup)]) == 0
    assert "Backup verified:" in capsys.readouterr().out
