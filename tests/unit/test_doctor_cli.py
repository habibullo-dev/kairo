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
    path.parent.mkdir(parents=True)
    conn = sqlite3.connect(path)
    try:
        conn.execute("CREATE TABLE marker (id INTEGER PRIMARY KEY)")
        conn.execute(f"PRAGMA user_version = {version}")
        conn.commit()
    finally:
        conn.close()


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
    assert f"needs migration to v{latest_version()}" in joined
    conn = sqlite3.connect(database)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == latest_version() - 1
    finally:
        conn.close()
    assert not database.with_name("jarvis.db-wal").exists()


def test_doctor_accepts_current_database_and_reports_optional_extras(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "key")
    _db(tmp_path / "data" / "jarvis.db", version=latest_version())
    present = {"fastapi", "uvicorn", "multipart"}
    monkeypatch.setattr(
        doctor.importlib.util,
        "find_spec",
        lambda module: object() if module in present else None,
    )
    lines: list[str] = []
    assert doctor.doctor_cli([], root=tmp_path, emit=lines.append) == 0
    joined = "\n".join(lines)
    assert "UI: installed" in joined
    assert "Voice: missing openai, elevenlabs, sounddevice" in joined
    assert f"schema v{latest_version()} (current); integrity ok" in joined
    assert "Doctor: ready." in joined


def test_doctor_reports_corrupt_database_safely(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "key")
    database = tmp_path / "data" / "jarvis.db"
    database.parent.mkdir()
    database.write_text("not a sqlite database", encoding="utf-8")
    lines: list[str] = []
    assert doctor.doctor_cli([], root=tmp_path, emit=lines.append) == 1
    assert "Database: unreadable or corrupt (no change made)" in "\n".join(lines)


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
