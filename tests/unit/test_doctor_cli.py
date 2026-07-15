"""``kira doctor`` pins: local/read-only, redacted, and useful on a fresh install."""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

from jarvis.cli import doctor
from jarvis.persistence.migrations import latest_version


@pytest.fixture(autouse=True)
def _clear_secret_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for field in doctor._SECRET_FIELDS:
        monkeypatch.delenv(field.upper(), raising=False)


def _db(path: Path, *, version: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        conn.execute("CREATE TABLE marker (id INTEGER PRIMARY KEY)")
        conn.execute(f"PRAGMA user_version = {version}")
        conn.commit()
    finally:
        conn.close()


def _inventory(root: Path) -> dict[str, tuple[bytes, int]]:
    return {
        path.relative_to(root).as_posix(): (path.read_bytes(), path.stat().st_mtime_ns)
        for path in root.rglob("*")
        if path.is_file()
    }


def test_fresh_doctor_never_creates_paths_or_contacts_services(tmp_path: Path) -> None:
    root = tmp_path / "fresh"
    lines: list[str] = []
    assert doctor.doctor_cli([], root=root, emit=lines.append) == 1
    assert not root.exists()  # no config/data/log directory was created
    joined = "\n".join(lines)
    assert "ANTHROPIC_API_KEY: missing" in joined
    assert "Database: not created" in joined
    assert "no network requests or local changes" in joined


def test_doctor_reports_secret_presence_without_leaking_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "DOCTOR-SECRET-CANARY")
    lines: list[str] = []
    assert doctor.doctor_cli([], root=tmp_path, emit=lines.append) == 1  # no DB yet
    joined = "\n".join(lines)
    assert "ANTHROPIC_API_KEY: present" in joined
    assert "DOCTOR-SECRET-CANARY" not in joined


def test_doctor_detects_stale_schema_without_migrating(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "key")
    database = tmp_path / "data" / "jarvis.db"
    _db(database, version=latest_version() - 1)
    lines: list[str] = []
    assert doctor.doctor_cli([], root=tmp_path, emit=lines.append) == 1
    joined = "\n".join(lines)
    assert "Database identity: legacy jarvis.db" in joined
    assert f"needs migration to v{latest_version()}" in joined
    conn = sqlite3.connect(database)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == latest_version() - 1
    finally:
        conn.close()
    assert not database.with_name("jarvis.db-wal").exists()
    assert not database.with_name("jarvis.db-shm").exists()


def test_doctor_does_not_create_sidecars_for_a_clean_wal_mode_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "key")
    database = tmp_path / "data" / "kira.db"
    _db(database, version=latest_version())
    conn = sqlite3.connect(database)
    try:
        assert conn.execute("PRAGMA journal_mode = WAL").fetchone() == ("wal",)
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        conn.close()
    database.with_name("kira.db-wal").unlink(missing_ok=True)
    database.with_name("kira.db-shm").unlink(missing_ok=True)
    before = _inventory(tmp_path)
    lines: list[str] = []

    assert doctor.doctor_cli([], root=tmp_path, emit=lines.append) == 0

    assert _inventory(tmp_path) == before
    assert f"schema v{latest_version()} (current); integrity ok" in "\n".join(lines)


def test_doctor_defers_on_pending_wal_without_touching_recovery_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "key")
    database = tmp_path / "data" / "kira.db"
    _db(database, version=latest_version())
    writer = sqlite3.connect(database)
    try:
        assert writer.execute("PRAGMA journal_mode = WAL").fetchone() == ("wal",)
        writer.execute("PRAGMA wal_autocheckpoint = 0")
        writer.execute("INSERT INTO marker DEFAULT VALUES")
        writer.commit()
        assert database.with_name("kira.db-wal").stat().st_size > 0
        before = _inventory(tmp_path)
        lines: list[str] = []

        assert doctor.doctor_cli([], root=tmp_path, emit=lines.append) == 1

        assert _inventory(tmp_path) == before
        assert "active or pending recovery state" in "\n".join(lines)
    finally:
        writer.close()


def test_doctor_accepts_current_database_and_reports_optional_extras(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "key")
    _db(tmp_path / "data" / "kira.db", version=latest_version())
    present = {"fastapi", "uvicorn", "multipart"}
    monkeypatch.setattr(
        doctor.importlib.util,
        "find_spec",
        lambda module: object() if module in present else None,
    )
    lines: list[str] = []
    assert doctor.doctor_cli([], root=tmp_path, emit=lines.append) == 0
    joined = "\n".join(lines)
    assert "Database identity: canonical kira.db" in joined
    assert "UI: installed" in joined
    assert "Voice: missing openai, elevenlabs, sounddevice" in joined
    assert f"schema v{latest_version()} (current); integrity ok" in joined
    assert "Doctor: ready." in joined


def test_doctor_reports_corrupt_database_safely(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "key")
    database = tmp_path / "data" / "kira.db"
    database.parent.mkdir()
    database.write_text("not a sqlite database", encoding="utf-8")
    lines: list[str] = []
    assert doctor.doctor_cli([], root=tmp_path, emit=lines.append) == 1
    assert "Database: unreadable or corrupt (no change made)" in "\n".join(lines)


def test_doctor_reports_ambiguous_database_identity_without_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "key")
    canonical = tmp_path / "data" / "kira.db"
    legacy = tmp_path / "data" / "jarvis.db"
    _db(canonical, version=latest_version())
    _db(legacy, version=latest_version() - 1)
    before = {
        path.name: (path.read_bytes(), path.stat().st_mtime_ns) for path in (canonical, legacy)
    }
    lines: list[str] = []

    assert doctor.doctor_cli([], root=tmp_path, emit=lines.append) == 1

    assert "Database identity: blocked (Both Kira and legacy databases exist" in "\n".join(lines)
    assert {
        path.name: (path.read_bytes(), path.stat().st_mtime_ns) for path in (canonical, legacy)
    } == before


def test_doctor_reports_interrupted_reset_without_creating_or_recovering_paths(
    tmp_path: Path,
) -> None:
    manifest = tmp_path / ".kira-reset-manifests" / "20260715T120000Z-deadbeef.json"
    manifest.parent.mkdir()
    manifest.write_text('{"status": "in_progress"}\n', encoding="utf-8")
    before = _inventory(tmp_path)
    lines: list[str] = []

    assert doctor.doctor_cli([], root=tmp_path, emit=lines.append) == 1

    assert _inventory(tmp_path) == before
    assert not (tmp_path / "data").exists() and not (tmp_path / "logs").exists()
    assert "Reset recovery: blocked" in "\n".join(lines)


def test_doctor_reports_invalid_yaml_without_creating_runtime_paths(tmp_path: Path) -> None:
    settings = tmp_path / "config" / "settings.yaml"
    settings.parent.mkdir()
    settings.write_text("models: [DOCTOR-SECRET-CANARY", encoding="utf-8")
    lines: list[str] = []
    assert doctor.doctor_cli([], root=tmp_path, emit=lines.append) == 2
    joined = "\n".join(lines)
    assert "Configuration error: invalid YAML in config/settings.yaml" in joined
    assert "DOCTOR-SECRET-CANARY" not in joined
    assert not (tmp_path / "data").exists() and not (tmp_path / "logs").exists()


def test_main_dispatches_doctor(monkeypatch: pytest.MonkeyPatch) -> None:
    import jarvis.__main__ as entry

    monkeypatch.setattr(sys, "argv", ["jarvis", "doctor"])
    monkeypatch.setattr(doctor, "doctor_cli", lambda argv: 7)
    with pytest.raises(SystemExit) as exited:
        entry.main()
    assert exited.value.code == 7
