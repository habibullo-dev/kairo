"""Whole-instance reset is offline, owner-gated, quarantine-first, and reversible."""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from kira.cli import reset as reset_module
from kira.cli.reset import DataResetError, reset_all_data
from kira.config import load_config
from kira.connectors.consent import (
    LOCKED_PROVIDERS,
    integration_consent_path,
    locked_integrations,
)
from kira.persistence import SessionStore
from kira.persistence.db import connect
from kira.persistence.instance_lock import InstanceLock, ResetBarrier
from kira.persistence.reset_recovery import (
    ResetRecoveryError,
    interrupted_reset_diagnostic,
    recover_interrupted_reset,
)
from kira.ui.owner_auth import (
    Argon2PasswordHasher,
    OwnerAuthService,
    OwnerLoginThrottledError,
)

PASSWORD = "A unique reset passphrase 2026!"


def _matching(directory: Path, pattern: str) -> list[Path]:
    return list(directory.glob(pattern)) if directory.exists() else []


def _entries(directory: Path) -> list[Path]:
    return list(directory.iterdir())


def _tree_state(directory: Path) -> dict[str, tuple[bytes, int]]:
    return {
        path.relative_to(directory).as_posix(): (path.read_bytes(), path.stat().st_mtime_ns)
        for path in directory.rglob("*")
        if path.is_file()
    }


async def _seed_instance(root: Path, *, database_name: str = "kira.db"):
    (root / "config").mkdir(parents=True)
    (root / "config" / "settings.yaml").write_text("{}\n", encoding="utf-8")
    (root / ".env").write_text("TELEGRAM_BOT_TOKEN=PRESERVED-SECRET\n", encoding="utf-8")
    (root / "source-sentinel.txt").write_text("source stays", encoding="utf-8")
    config = load_config(root=root)
    config.ensure_dirs()
    config.knowledge_dir.mkdir(parents=True)
    (config.data_dir / "connectors").mkdir()
    (config.data_dir / "connectors" / "google_token.json").write_text(
        "QUARANTINED-TOKEN", encoding="utf-8"
    )
    (config.knowledge_dir / "project.md").write_text("old knowledge", encoding="utf-8")
    (config.logs_dir / "kairo.log").write_text("old log", encoding="utf-8")

    db = await connect(config.data_dir / database_name)
    auth = OwnerAuthService(
        db,
        SessionStore(db).lock,
        hasher=Argon2PasswordHasher(time_cost=1, memory_cost=1024, parallelism=1),
    )
    grant = await auth.issue_auth_grant("enroll")
    await auth.enroll(grant.token, "habib", PASSWORD)
    await db.close()
    return config


@pytest.mark.parametrize("database_name", ["kira.db", "jarvis.db"])
async def test_reset_quarantines_old_roots_bootstraps_fresh_ownerless_instance(
    tmp_path: Path, database_name: str
) -> None:
    config = await _seed_instance(tmp_path, database_name=database_name)
    source_database = config.data_dir / database_name
    source_state = (source_database.read_bytes(), source_database.stat().st_mtime_ns)
    result = await reset_all_data(config, PASSWORD)
    assert result.manifest.parent.name == ".kira-reset-manifests"
    assert all(".kira-quarantine-" in path.name for path in result.quarantines)

    assert (tmp_path / ".env").read_text(encoding="utf-8").startswith("TELEGRAM_BOT_TOKEN")
    assert (tmp_path / "config" / "settings.yaml").is_file()
    assert (tmp_path / "source-sentinel.txt").read_text(encoding="utf-8") == "source stays"
    assert locked_integrations(config.data_dir) == LOCKED_PROVIDERS
    assert not (config.knowledge_dir / "project.md").exists()
    assert not (config.logs_dir / "kairo.log").exists()

    data_quarantine = next(path for path in result.quarantines if (path / database_name).exists())
    quarantined_database = data_quarantine / database_name
    assert (
        quarantined_database.read_bytes(),
        quarantined_database.stat().st_mtime_ns,
    ) == source_state
    assert (data_quarantine / "connectors" / "google_token.json").read_text(
        encoding="utf-8"
    ) == "QUARANTINED-TOKEN"
    assert (data_quarantine / "knowledge" / "project.md").is_file()
    assert any((path / "kairo.log").is_file() for path in result.quarantines)

    fresh = await connect(config.data_dir / "kira.db")
    try:
        assert await (await fresh.execute("SELECT COUNT(*) FROM owner_accounts")).fetchone() == (0,)
        assert await (await fresh.execute("PRAGMA integrity_check")).fetchone() == ("ok",)
        assert await (await fresh.execute("PRAGMA foreign_key_check")).fetchone() is None
    finally:
        await fresh.close()

    manifest_text = result.manifest.read_text(encoding="utf-8")
    manifest = json.loads(manifest_text)
    assert manifest["status"] == "completed"
    assert manifest["integrity_check"] == "ok"
    assert manifest["old_counts"]["owner_accounts"] == 1
    assert manifest["locked_integrations"] == sorted(LOCKED_PROVIDERS)
    assert PASSWORD not in manifest_text and "QUARANTINED-TOKEN" not in manifest_text


async def test_legacy_reset_artifacts_are_preserved_and_block_id_reuse(tmp_path: Path) -> None:
    config = await _seed_instance(tmp_path)
    reset_id = "20260715T000000Z-deadbeef"
    legacy_quarantine = config.data_dir.with_name(
        f".{config.data_dir.name}.kairo-quarantine-{reset_id}"
    )
    legacy_quarantine.mkdir()
    (legacy_quarantine / "archive-sentinel").write_bytes(b"preserve legacy archive")

    with pytest.raises(DataResetError, match="already exists"):
        reset_module._planned_moves(
            config,
            reset_id,
            include_external_knowledge=False,
        )

    legacy_manifest = tmp_path / ".kairo-reset-manifests" / f"{reset_id}.json"
    legacy_manifest.parent.mkdir()
    legacy_manifest.write_bytes(b'{"legacy": true}\n')
    with pytest.raises(DataResetError, match="manifest already exists"):
        reset_module._manifest_path(config, reset_id)

    assert (legacy_quarantine / "archive-sentinel").read_bytes() == b"preserve legacy archive"
    assert legacy_manifest.read_bytes() == b'{"legacy": true}\n'
    assert not (tmp_path / ".kira-reset-manifests").exists()


async def test_preexisting_failed_fresh_path_blocks_reset_before_manifest_or_move(
    tmp_path: Path,
) -> None:
    config = await _seed_instance(tmp_path)
    reset_id = "20260715T000000Z-feedface"
    failed = config.data_dir.with_name(
        f".{config.data_dir.name}.kira-reset-failed-fresh-{reset_id}"
    )
    failed.mkdir()
    (failed / "sentinel").write_bytes(b"preserve collision")

    with pytest.raises(DataResetError, match="recovery path already exists"):
        reset_module._planned_moves(
            config,
            reset_id,
            include_external_knowledge=False,
        )

    assert (config.data_dir / "kira.db").is_file()
    assert (failed / "sentinel").read_bytes() == b"preserve collision"
    assert not (tmp_path / ".kira-reset-manifests").exists()


async def test_completed_legacy_reset_history_coexists_with_a_new_kira_reset(
    tmp_path: Path,
) -> None:
    config = await _seed_instance(tmp_path)
    legacy_manifest = tmp_path / ".kairo-reset-manifests" / "historical.json"
    legacy_manifest.parent.mkdir()
    legacy_manifest.write_bytes(b'{"reset_id": "historical", "status": "completed"}\n')
    legacy_quarantine = tmp_path / ".data.kairo-quarantine-historical"
    legacy_quarantine.mkdir()
    (legacy_quarantine / "archive-sentinel").write_bytes(b"historical data")
    legacy_manifest_state = (legacy_manifest.read_bytes(), legacy_manifest.stat().st_mtime_ns)
    legacy_quarantine_state = _tree_state(legacy_quarantine)

    result = await reset_all_data(config, PASSWORD)

    assert (legacy_manifest.read_bytes(), legacy_manifest.stat().st_mtime_ns) == (
        legacy_manifest_state
    )
    assert _tree_state(legacy_quarantine) == legacy_quarantine_state
    assert result.manifest.parent.name == ".kira-reset-manifests"
    assert all(".kira-quarantine-" in path.name for path in result.quarantines)


@pytest.mark.parametrize(
    "manifest_dirname",
    [".kira-reset-manifests", ".kairo-reset-manifests"],
)
async def test_manifest_storage_remains_protected_from_reset(
    tmp_path: Path,
    manifest_dirname: str,
) -> None:
    config = await _seed_instance(tmp_path)
    manifests = tmp_path / manifest_dirname
    manifests.mkdir()
    (manifests / "audit.json").write_text('{"status": "completed"}\n', encoding="utf-8")
    config.paths.logs_dir = manifests

    with pytest.raises(DataResetError, match="overlaps reset manifest storage"):
        reset_module._planned_moves(
            config,
            "20260715T000000Z-feedface",
            include_external_knowledge=False,
        )

    assert (manifests / "audit.json").read_text(encoding="utf-8") == '{"status": "completed"}\n'


@pytest.mark.parametrize("database_name", ["kira.db", "jarvis.db"])
async def test_wrong_password_leaves_all_roots_untouched(
    tmp_path: Path, database_name: str
) -> None:
    config = await _seed_instance(tmp_path, database_name=database_name)
    database = config.data_dir / database_name
    conn = sqlite3.connect(database)
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        assert conn.execute("PRAGMA journal_mode = DELETE").fetchone() == ("delete",)
    finally:
        conn.close()
    database.with_name(f"{database.name}-wal").unlink(missing_ok=True)
    database.with_name(f"{database.name}-shm").unlink(missing_ok=True)
    before = {
        "data": _tree_state(config.data_dir),
        "logs": _tree_state(config.logs_dir),
        "knowledge": _tree_state(config.knowledge_dir),
    }

    with pytest.raises(DataResetError, match="password"):
        await reset_all_data(config, "A wrong but sufficiently long reset password")

    assert {
        "data": _tree_state(config.data_dir),
        "logs": _tree_state(config.logs_dir),
        "knowledge": _tree_state(config.knowledge_dir),
    } == before
    assert (config.knowledge_dir / "project.md").is_file()
    assert not _matching(tmp_path, ".*.kira-quarantine-*")
    assert not (tmp_path / ".kira-reset-manifests").exists()
    throttle = json.loads(reset_module._reset_auth_path(config).read_text(encoding="utf-8"))
    assert throttle == {"failed_attempts": 1, "locked_until": None}


async def test_reset_password_failures_are_durably_throttled(tmp_path: Path) -> None:
    config = await _seed_instance(tmp_path)
    database = config.data_dir / "kira.db"
    before = (database.read_bytes(), database.stat().st_mtime_ns)

    for _attempt in range(4):
        with pytest.raises(DataResetError, match="password"):
            await reset_all_data(config, "A wrong but sufficiently long reset password")
    with pytest.raises(OwnerLoginThrottledError):
        await reset_all_data(config, "A wrong but sufficiently long reset password")

    throttle = json.loads(reset_module._reset_auth_path(config).read_text(encoding="utf-8"))
    assert throttle["failed_attempts"] == 5 and throttle["locked_until"] is not None
    assert (database.read_bytes(), database.stat().st_mtime_ns) == before


async def test_source_login_threshold_cannot_bypass_reset_throttle(tmp_path: Path) -> None:
    config = await _seed_instance(tmp_path)
    database = config.data_dir / "kira.db"
    conn = sqlite3.connect(database)
    try:
        conn.execute("UPDATE owner_accounts SET failed_attempts = 4, locked_until = NULL")
        conn.commit()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        assert conn.execute("PRAGMA journal_mode = DELETE").fetchone() == ("delete",)
    finally:
        conn.close()
    before = _tree_state(config.data_dir)

    with pytest.raises(DataResetError, match="password"):
        await reset_all_data(config, "A wrong but sufficiently long reset password")

    assert _tree_state(config.data_dir) == before
    throttle = json.loads(reset_module._reset_auth_path(config).read_text(encoding="utf-8"))
    assert throttle == {"failed_attempts": 1, "locked_until": None}


async def test_reset_auth_snapshot_reads_dirty_wal_without_touching_source(tmp_path: Path) -> None:
    config = await _seed_instance(tmp_path)
    database = config.data_dir / "kira.db"
    script = "\n".join(
        [
            "import os, sqlite3, sys",
            "db = sqlite3.connect(sys.argv[1])",
            "db.execute('PRAGMA journal_mode = WAL')",
            "db.execute('PRAGMA wal_autocheckpoint = 0')",
            'db.execute("INSERT INTO projects '
            "(name, slug, created_at, updated_at) VALUES "
            "('WAL project', 'wal-project', '2026-01-01', '2026-01-01')\")",
            "db.commit()",
            "os._exit(0)",
        ]
    )
    await asyncio.to_thread(
        subprocess.run,
        [sys.executable, "-c", script, str(database)],
        check=True,
    )
    wal = database.with_name("kira.db-wal")
    assert wal.is_file() and wal.stat().st_size > 0
    database.with_name("kira.db-shm").unlink(missing_ok=True)
    before = _tree_state(config.data_dir)

    with InstanceLock(config.data_dir):
        _version, counts = await reset_module._authenticate_old_database(database, PASSWORD)

    assert counts["projects"] == 1
    assert _tree_state(config.data_dir) == before


async def test_crashed_external_reset_remains_visible_after_config_anchor_changes(
    tmp_path: Path,
) -> None:
    original_root = tmp_path / "project-a"
    config = await _seed_instance(original_root)
    external_parent = tmp_path / "external-state"
    external_parent.mkdir()
    external_data = external_parent / "data"
    external_logs = external_parent / "logs"
    config.data_dir.rename(external_data)
    config.logs_dir.rename(external_logs)
    settings = "\n".join(
        [
            "paths:",
            f"  data_dir: {json.dumps(str(external_data))}",
            f"  logs_dir: {json.dumps(str(external_logs))}",
            "knowledge:",
            f"  dir: {json.dumps(str(external_data / 'knowledge'))}",
            "",
        ]
    )
    (original_root / "config" / "settings.yaml").write_text(settings, encoding="utf-8")

    crash_script = "\n".join(
        [
            "import asyncio, os, sys",
            "from pathlib import Path",
            "from kira.cli import reset as reset_module",
            "from kira.cli.reset import reset_all_data",
            "from kira.config import load_config",
            "real_bootstrap = reset_module._bootstrap_fresh_database",
            "async def crash(database):",
            "    await real_bootstrap(database)",
            "    os._exit(73)",
            "reset_module._bootstrap_fresh_database = crash",
            "config = load_config(root=Path(sys.argv[1]))",
            "asyncio.run(reset_all_data(",
            "    config,",
            f"    {PASSWORD!r},",
            "    include_external_logs=True,",
            "))",
        ]
    )
    crashed = await asyncio.to_thread(
        subprocess.run,
        [sys.executable, "-c", crash_script, str(original_root)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert crashed.returncode == 73, crashed.stderr

    fresh_database = external_data / "kira.db"
    quarantines = [
        path
        for path in external_parent.glob(".data.kira-quarantine-*")
        if (path / "kira.db").is_file()
    ]
    assert fresh_database.is_file()
    assert len(quarantines) == 1
    with sqlite3.connect(fresh_database) as db:
        assert db.execute("SELECT COUNT(*) FROM owner_accounts").fetchone() == (0,)
    with sqlite3.connect(quarantines[0] / "kira.db") as db:
        assert db.execute("SELECT COUNT(*) FROM owner_accounts").fetchone() == (1,)

    relocated_root = tmp_path / "project-b"
    (relocated_root / "config").mkdir(parents=True)
    (relocated_root / "config" / "settings.yaml").write_text(settings, encoding="utf-8")
    relocated = load_config(root=relocated_root)
    diagnostic = interrupted_reset_diagnostic(relocated)

    assert diagnostic is not None
    assert diagnostic.startswith("blocked (") or " is pending" in diagnostic
    if diagnostic.startswith("blocked ("):
        with (
            ResetBarrier(relocated.data_dir) as barrier,
            InstanceLock(relocated.data_dir) as lock,
            pytest.raises(ResetRecoveryError),
        ):
            recover_interrupted_reset(relocated, barrier, lock)
        with sqlite3.connect(fresh_database) as db:
            assert db.execute("SELECT COUNT(*) FROM owner_accounts").fetchone() == (0,)
        assert (quarantines[0] / "kira.db").is_file()
    else:
        with (
            ResetBarrier(relocated.data_dir) as barrier,
            InstanceLock(relocated.data_dir) as lock,
        ):
            assert recover_interrupted_reset(relocated, barrier, lock) is True
        with sqlite3.connect(fresh_database) as db:
            assert db.execute("SELECT COUNT(*) FROM owner_accounts").fetchone() == (1,)


async def test_ambiguous_database_names_block_reset_without_moving_data(tmp_path: Path) -> None:
    config = await _seed_instance(tmp_path)
    canonical = config.data_dir / "kira.db"
    legacy = config.data_dir / "jarvis.db"
    legacy.write_bytes(canonical.read_bytes())
    before = {path.name: path.read_bytes() for path in (canonical, legacy)}

    with pytest.raises(DataResetError, match="Both Kira and legacy databases exist"):
        await reset_all_data(config, PASSWORD)

    assert {path.name: path.read_bytes() for path in (canonical, legacy)} == before
    assert not _matching(tmp_path, ".*.kira-quarantine-*")


async def test_live_instance_lock_leaves_every_runtime_byte_untouched(tmp_path: Path) -> None:
    config = await _seed_instance(tmp_path)
    protected = (
        config.data_dir / "kira.db",
        config.data_dir / "connectors" / "google_token.json",
        config.knowledge_dir / "project.md",
        config.logs_dir / "kairo.log",
    )
    before = {path: path.read_bytes() for path in protected}

    with InstanceLock(config.data_dir), pytest.raises(DataResetError, match="instance lock"):
        await reset_all_data(config, PASSWORD)

    assert {path: path.read_bytes() for path in protected} == before
    assert not _matching(tmp_path, ".*.kira-quarantine-*")
    assert not (tmp_path / ".kira-reset-manifests").exists()


@pytest.mark.parametrize("database_name", ["kira.db", "jarvis.db"])
async def test_bootstrap_failure_restores_every_quarantine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, database_name: str
) -> None:
    config = await _seed_instance(tmp_path, database_name=database_name)

    async def fail_bootstrap(_database: Path) -> int:
        raise RuntimeError("injected bootstrap failure")

    monkeypatch.setattr(reset_module, "_bootstrap_fresh_database", fail_bootstrap)
    with pytest.raises(DataResetError, match="original Kira data was restored"):
        await reset_all_data(config, PASSWORD)

    assert (config.data_dir / database_name).is_file()
    other_name = "jarvis.db" if database_name == "kira.db" else "kira.db"
    assert not (config.data_dir / other_name).exists()
    assert (config.knowledge_dir / "project.md").read_text(encoding="utf-8") == "old knowledge"
    assert (config.logs_dir / "kairo.log").read_text(encoding="utf-8") == "old log"
    assert not _matching(tmp_path, ".*.kira-quarantine-*")
    manifests = _matching(tmp_path / ".kira-reset-manifests", "*.json")
    assert len(manifests) == 1
    assert json.loads(manifests[0].read_text(encoding="utf-8"))["status"] == "rolled_back"


async def test_bootstrap_failure_with_missing_published_records_reports_recovery_blocked(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = await _seed_instance(tmp_path)
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    config.data_dir.rename(runtime / "data")
    config.paths.data_dir = Path("runtime/data")
    config.knowledge.dir = Path("runtime/data/knowledge")

    async def delete_records_then_fail(_database: Path) -> int:
        manifests = list((runtime / ".kira-reset-manifests").glob("*.json"))
        locators = list((tmp_path / ".kira-reset-manifests").glob("*.locator"))
        assert len(manifests) == 1
        assert len(locators) == 1
        for path in (*manifests, *locators):
            path.unlink()
        raise RuntimeError("injected bootstrap failure after manifest loss")

    monkeypatch.setattr(reset_module, "_bootstrap_fresh_database", delete_records_then_fail)
    with pytest.raises(DataResetError) as caught:
        await reset_all_data(config, PASSWORD)

    assert "lossless automatic recovery was blocked" in str(caught.value)
    assert "original Kira data was restored" not in str(caught.value)
    quarantines = _matching(runtime, ".data.kira-quarantine-*")
    assert len(quarantines) == 1
    assert (quarantines[0] / "connectors" / "google_token.json").read_text(
        encoding="utf-8"
    ) == "QUARANTINED-TOKEN"
    assert config.data_dir.is_dir()
    assert not (config.data_dir / "connectors" / "google_token.json").exists()


async def test_visible_completed_manifest_remains_the_commit_point_when_publish_reports_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = await _seed_instance(tmp_path)
    real_write = reset_module._write_manifest

    def publish_then_report_error(path: Path, payload: dict) -> None:
        real_write(path, payload)
        if payload.get("status") == "completed":
            raise OSError("injected post-publication durability error")

    monkeypatch.setattr(reset_module, "_write_manifest", publish_then_report_error)
    result = await reset_all_data(config, PASSWORD)

    assert json.loads(result.manifest.read_text(encoding="utf-8"))["status"] == "completed"
    assert (config.data_dir / "kira.db").is_file()
    assert not (config.data_dir / "connectors" / "google_token.json").exists()
    old_data = next(path for path in result.quarantines if (path / "kira.db").is_file())
    assert (old_data / "connectors" / "google_token.json").read_text(encoding="utf-8") == (
        "QUARANTINED-TOKEN"
    )


async def test_missing_fresh_consent_floor_prevents_completion_and_restores_old_data(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = await _seed_instance(tmp_path)
    real_bootstrap = reset_module._bootstrap_fresh_database

    async def bootstrap_then_remove_consent(database: Path) -> int:
        version = await real_bootstrap(database)
        integration_consent_path(config.data_dir).unlink()
        return version

    monkeypatch.setattr(reset_module, "_bootstrap_fresh_database", bootstrap_then_remove_consent)
    with pytest.raises(DataResetError, match="integration-consent lock verification failed"):
        await reset_all_data(config, PASSWORD)

    assert (config.data_dir / "connectors" / "google_token.json").read_text(encoding="utf-8") == (
        "QUARANTINED-TOKEN"
    )
    manifest = json.loads(
        next((tmp_path / ".kira-reset-manifests").glob("*.json")).read_text(encoding="utf-8")
    )
    assert manifest["status"] == "rolled_back"


async def test_nested_originally_absent_roots_roll_back_as_one_outer_archive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = await _seed_instance(tmp_path)
    outer = tmp_path / "external" / "logs"
    config.paths.logs_dir = outer
    config.knowledge.dir = outer / "knowledge"

    async def fail_bootstrap(_database: Path) -> int:
        raise RuntimeError("injected bootstrap failure")

    monkeypatch.setattr(reset_module, "_bootstrap_fresh_database", fail_bootstrap)
    with pytest.raises(DataResetError, match="original Kira data was restored"):
        await reset_all_data(
            config,
            PASSWORD,
            include_external_knowledge=True,
            include_external_logs=True,
        )

    assert (config.data_dir / "kira.db").is_file()
    assert not outer.exists()
    failed = _matching(outer.parent, f".{outer.name}.kira-reset-failed-fresh-*")
    assert len(failed) == 1 and (failed[0] / "knowledge").is_dir()
    manifest = json.loads(
        next((tmp_path / ".kira-reset-manifests").glob("*.json")).read_text(encoding="utf-8")
    )
    assert manifest["status"] == "rolled_back"
    assert manifest["absent_roots"] == [{"roles": ["knowledge", "logs"], "source": str(outer)}]


@pytest.mark.parametrize("outer_role", ["logs", "knowledge"])
async def test_absent_nested_role_is_bound_to_its_existing_moved_outer_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    outer_role: str,
) -> None:
    config = await _seed_instance(tmp_path)
    outer = tmp_path / "external-vault"
    outer.mkdir()
    (outer / "old-external.txt").write_text("old external", encoding="utf-8")
    if outer_role == "logs":
        config.paths.logs_dir = outer
        config.knowledge.dir = outer / "knowledge"
    else:
        config.knowledge.dir = outer
        config.paths.logs_dir = outer / "logs"

    async def fail_bootstrap(_database: Path) -> int:
        raise RuntimeError("injected bootstrap failure")

    monkeypatch.setattr(reset_module, "_bootstrap_fresh_database", fail_bootstrap)
    with pytest.raises(DataResetError, match="original Kira data was restored"):
        await reset_all_data(
            config,
            PASSWORD,
            include_external_knowledge=True,
            include_external_logs=True,
        )

    assert (outer / "old-external.txt").read_text(encoding="utf-8") == "old external"
    nested = outer / ("knowledge" if outer_role == "logs" else "logs")
    assert not nested.exists()
    manifest = json.loads(
        next((tmp_path / ".kira-reset-manifests").glob("*.json")).read_text(encoding="utf-8")
    )
    external_record = next(record for record in manifest["roots"] if record["source"] == str(outer))
    assert external_record["roles"] == ["knowledge", "logs"]


async def test_root_appearing_during_authentication_is_archived_and_reset_rolls_back(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = await _seed_instance(tmp_path)
    raced_logs = tmp_path / "raced-logs"
    config.paths.logs_dir = raced_logs
    real_authenticate = reset_module._authenticate_old_database

    async def authenticate_then_race(database: Path, password: str):
        facts = await real_authenticate(database, password)
        raced_logs.mkdir()
        (raced_logs / "concurrent.txt").write_text("preserve race", encoding="utf-8")
        return facts

    monkeypatch.setattr(reset_module, "_authenticate_old_database", authenticate_then_race)
    with pytest.raises(DataResetError, match="Originally absent reset root appeared"):
        await reset_all_data(config, PASSWORD, include_external_logs=True)

    assert (config.data_dir / "kira.db").is_file()
    assert not raced_logs.exists()
    archived = _matching(tmp_path, ".raced-logs.kira-reset-failed-fresh-*")
    assert len(archived) == 1
    assert (archived[0] / "concurrent.txt").read_text(encoding="utf-8") == "preserve race"


async def test_linked_ancestor_is_canonicalized_consistently_for_recovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = await _seed_instance(tmp_path)
    real = tmp_path / "external-real"
    logs = real / "logs"
    logs.mkdir(parents=True)
    (logs / "old.log").write_text("old log", encoding="utf-8")
    alias = tmp_path / "external-alias"
    try:
        os.symlink(real, alias, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory symlinks unavailable: {exc}")
    config.paths.logs_dir = alias / "logs"

    async def fail_bootstrap(_database: Path) -> int:
        raise RuntimeError("injected bootstrap failure")

    monkeypatch.setattr(reset_module, "_bootstrap_fresh_database", fail_bootstrap)
    with pytest.raises(DataResetError, match="original Kira data was restored"):
        await reset_all_data(config, PASSWORD, include_external_logs=True)

    assert (config.data_dir / "kira.db").is_file()
    assert (logs / "old.log").read_text(encoding="utf-8") == "old log"
    manifest = json.loads(
        next((tmp_path / ".kira-reset-manifests").glob("*.json")).read_text(encoding="utf-8")
    )
    assert manifest["status"] == "rolled_back"


async def test_linked_data_ancestor_remains_bound_across_move_and_rollback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = await _seed_instance(tmp_path)
    alias = tmp_path / "workspace-alias"
    try:
        alias.symlink_to(tmp_path, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory symlinks unavailable: {exc}")
    config.paths.data_dir = alias / "data"
    config.paths.logs_dir = alias / "logs"
    config.knowledge.dir = alias / "data" / "knowledge"

    async def fail_bootstrap(_database: Path) -> int:
        raise RuntimeError("injected bootstrap failure")

    monkeypatch.setattr(reset_module, "_bootstrap_fresh_database", fail_bootstrap)
    with pytest.raises(DataResetError, match="original Kira data was restored"):
        await reset_all_data(config, PASSWORD)

    assert (tmp_path / "data" / "kira.db").is_file()
    assert (tmp_path / "data" / "knowledge" / "project.md").is_file()
    assert (tmp_path / "logs" / "kairo.log").is_file()
    manifest = json.loads(
        next((tmp_path / ".kira-reset-manifests").glob("*.json")).read_text(encoding="utf-8")
    )
    assert manifest["status"] == "rolled_back"


async def test_data_ancestor_retarget_during_authentication_blocks_before_move(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = await _seed_instance(tmp_path)
    alias = tmp_path / "workspace-alias"
    try:
        alias.symlink_to(tmp_path, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory symlinks unavailable: {exc}")
    config.paths.data_dir = alias / "data"
    config.paths.logs_dir = alias / "logs"
    config.knowledge.dir = alias / "data" / "knowledge"
    other = tmp_path / "retargeted-workspace"
    other_data = other / "data"
    other_data.mkdir(parents=True)
    sentinel = other_data / "unrelated.txt"
    sentinel.write_text("do not touch", encoding="utf-8")
    real_authenticate = reset_module._authenticate_old_database

    async def authenticate_then_retarget(database: Path, password: str):
        facts = await real_authenticate(database, password)
        alias.unlink()
        alias.symlink_to(other, target_is_directory=True)
        return facts

    monkeypatch.setattr(reset_module, "_authenticate_old_database", authenticate_then_retarget)
    with pytest.raises(DataResetError, match="roots changed during reset"):
        await reset_all_data(config, PASSWORD)

    assert (tmp_path / "data" / "kira.db").is_file()
    assert sentinel.read_text(encoding="utf-8") == "do not touch"
    assert not (other_data / "kira.db").exists()
    assert not (other_data / ".integration-consent.json").exists()
    assert not (tmp_path / ".kira-reset-manifests").exists()


async def test_failed_password_throttle_stays_bound_when_data_ancestor_retargets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = await _seed_instance(tmp_path)
    alias = tmp_path / "workspace-alias"
    try:
        alias.symlink_to(tmp_path, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory symlinks unavailable: {exc}")
    config.paths.data_dir = alias / "data"
    config.paths.logs_dir = alias / "logs"
    config.knowledge.dir = alias / "data" / "knowledge"
    other = tmp_path / "retargeted-workspace"
    other_data = other / "data"
    other_data.mkdir(parents=True)
    sentinel = other_data / "unrelated.txt"
    sentinel.write_text("do not touch", encoding="utf-8")
    real_authenticate = reset_module._authenticate_old_database

    async def reject_then_retarget(database: Path, password: str):
        try:
            return await real_authenticate(database, password)
        except reset_module._ResetPasswordRejected:
            alias.unlink()
            alias.symlink_to(other, target_is_directory=True)
            raise

    monkeypatch.setattr(reset_module, "_authenticate_old_database", reject_then_retarget)
    with pytest.raises(DataResetError, match="password"):
        await reset_all_data(config, "A wrong but sufficiently long reset password")

    bound_throttle = tmp_path / ".data.kira-reset-auth.json"
    assert json.loads(bound_throttle.read_text(encoding="utf-8"))["failed_attempts"] == 1
    assert not (other / ".data.kira-reset-auth.json").exists()
    assert sentinel.read_text(encoding="utf-8") == "do not touch"
    assert (tmp_path / "data" / "kira.db").is_file()
    assert not (tmp_path / ".kira-reset-manifests").exists()


async def test_failed_password_throttle_does_not_follow_replaced_data_leaf(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = await _seed_instance(tmp_path)
    data = config.data_dir
    parked = tmp_path / "parked-original-data"
    other = tmp_path / "unrelated-parent"
    other_data = other / "data"
    other_data.mkdir(parents=True)
    sentinel = other_data / "unrelated.txt"
    sentinel.write_text("do not touch", encoding="utf-8")
    real_authenticate = reset_module._authenticate_old_database

    async def reject_then_replace_leaf(database: Path, password: str):
        try:
            return await real_authenticate(database, password)
        except reset_module._ResetPasswordRejected:
            data.rename(parked)
            try:
                data.symlink_to(other_data, target_is_directory=True)
            except OSError as exc:
                parked.rename(data)
                pytest.skip(f"directory symlinks unavailable: {exc}")
            raise

    monkeypatch.setattr(reset_module, "_authenticate_old_database", reject_then_replace_leaf)
    with pytest.raises(DataResetError, match="password"):
        await reset_all_data(config, "A wrong but sufficiently long reset password")

    bound_throttle = tmp_path / ".data.kira-reset-auth.json"
    assert json.loads(bound_throttle.read_text(encoding="utf-8"))["failed_attempts"] == 1
    assert not (other / ".data.kira-reset-auth.json").exists()
    assert sentinel.read_text(encoding="utf-8") == "do not touch"
    assert (parked / "kira.db").is_file()
    assert not (tmp_path / ".kira-reset-manifests").exists()


async def test_external_knowledge_requires_separate_explicit_consent(tmp_path: Path) -> None:
    config = await _seed_instance(tmp_path)
    external = tmp_path / "external-vault"
    external.mkdir()
    (external / "private-note.md").write_text("preserve me", encoding="utf-8")
    config.knowledge.dir = external

    with pytest.raises(DataResetError, match="separate confirmation"):
        await reset_all_data(config, PASSWORD)
    assert (external / "private-note.md").is_file()
    assert (config.data_dir / "kira.db").is_file()

    result = await reset_all_data(config, PASSWORD, include_external_knowledge=True)
    vault_quarantine = next(
        path for path in result.quarantines if (path / "private-note.md").is_file()
    )
    assert vault_quarantine.parent == external.parent
    assert external.is_dir() and not _entries(external)


async def test_external_logs_require_separate_explicit_consent(tmp_path: Path) -> None:
    config = await _seed_instance(tmp_path)
    external = tmp_path / "external-logs"
    external.mkdir()
    (external / "personal.log").write_text("preserve me", encoding="utf-8")
    config.paths.logs_dir = external

    with pytest.raises(DataResetError, match="logs root.*separate confirmation"):
        await reset_all_data(config, PASSWORD)
    assert (external / "personal.log").is_file()
    assert (config.data_dir / "kira.db").is_file()

    result = await reset_all_data(config, PASSWORD, include_external_logs=True)
    logs_quarantine = next(path for path in result.quarantines if (path / "personal.log").is_file())
    assert logs_quarantine.parent == external.parent
    assert external.is_dir() and not _entries(external)


async def test_absent_external_logs_require_consent_before_authentication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = await _seed_instance(tmp_path)
    external = tmp_path / "future-external-logs"
    config.paths.logs_dir = external

    async def unexpected_authentication(_database: Path, _password: str):
        pytest.fail("an unconfirmed external root must block before password authentication")

    monkeypatch.setattr(
        reset_module,
        "_authenticate_old_database",
        unexpected_authentication,
    )
    with pytest.raises(DataResetError, match="logs root.*separate confirmation"):
        await reset_all_data(config, PASSWORD)

    assert not external.exists()
    assert (config.data_dir / "kira.db").is_file()
    assert not (tmp_path / ".kira-reset-manifests").exists()


def test_reset_cli_refuses_noninteractive_input(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(reset_module.sys, "stdin", SimpleNamespace(isatty=lambda: False))
    assert reset_module.reset_cli(["data"]) == 1
    assert "interactively" in capsys.readouterr().out


def test_main_dispatches_reset_before_provider_key_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import kira.__main__ as entry

    monkeypatch.setattr(sys, "argv", ["jarvis", "reset", "data"])
    monkeypatch.setattr(reset_module, "reset_cli", lambda argv: 9 if argv == ["data"] else 1)
    with pytest.raises(SystemExit) as exited:
        entry.main()
    assert exited.value.code == 9
