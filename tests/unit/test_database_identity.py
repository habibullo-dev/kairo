"""Kira live-database identity migration is offline, lossless, and fail closed."""

from __future__ import annotations

import contextlib
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from jarvis.persistence import database_identity as identity_module
from jarvis.persistence.backup import verify_backup
from jarvis.persistence.database_identity import (
    DATABASE_FILENAME,
    LEGACY_DATABASE_FILENAME,
    DatabaseIdentityError,
    migrate_live_database,
    select_database,
)
from jarvis.persistence.db import connect
from jarvis.persistence.instance_lock import InstanceLock
from jarvis.persistence.migrations import latest_version


def _database(path: Path, *, version: int | None = None, value: str = "preserved") -> None:
    db = sqlite3.connect(path)
    try:
        db.execute("CREATE TABLE identity_probe (value TEXT NOT NULL)")
        db.execute("INSERT INTO identity_probe VALUES (?)", (value,))
        db.execute(f"PRAGMA user_version = {latest_version() if version is None else version}")
        db.commit()
    finally:
        db.close()


def _probe(path: Path) -> tuple[str, int]:
    db = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    try:
        value = str(db.execute("SELECT value FROM identity_probe").fetchone()[0])
        version = int(db.execute("PRAGMA user_version").fetchone()[0])
        return value, version
    finally:
        db.close()


def test_migration_requires_an_acquired_dual_lock(tmp_path: Path) -> None:
    lock = InstanceLock(tmp_path / "data")

    with pytest.raises(RuntimeError, match="does not currently own"):
        migrate_live_database(lock)


def test_atomic_no_replace_move_preserves_an_existing_destination(tmp_path: Path) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.write_bytes(b"source")
    destination.write_bytes(b"destination")

    with pytest.raises(FileExistsError):
        identity_module._rename_no_replace(source, destination)

    assert source.read_bytes() == b"source"
    assert destination.read_bytes() == b"destination"


@pytest.mark.parametrize(
    "payload",
    [
        b"unrecognized cutover state",
        identity_module._PENDING_GUARD_PAYLOAD,
        identity_module._TOMBSTONE_PAYLOAD,
    ],
)
def test_selector_blocks_every_parked_cutover_state(tmp_path: Path, payload: bytes) -> None:
    data = tmp_path / "data"
    data.mkdir()
    (data / identity_module._CUTOVER_PARKED).write_bytes(payload)

    with pytest.raises(DatabaseIdentityError, match="cutover was interrupted"):
        select_database(data)


def test_fresh_and_canonical_states_are_idempotent(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    canonical = data / DATABASE_FILENAME

    with InstanceLock(data) as lock:
        assert migrate_live_database(lock) == canonical
        assert canonical.is_file() and canonical.stat().st_size == 0
        assert (data / LEGACY_DATABASE_FILENAME).is_file()
        _database(canonical)
        before = canonical.read_bytes()
        assert migrate_live_database(lock) == canonical

    assert canonical.read_bytes() == before
    assert select_database(data) == canonical


def test_fresh_initialization_guard_survives_failure_before_canonical_creation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = tmp_path / "data"
    data.mkdir()
    legacy = data / LEGACY_DATABASE_FILENAME
    canonical = data / DATABASE_FILENAME
    original_create = identity_module._create_fresh_canonical

    def fail_create(_canonical: Path):
        raise DatabaseIdentityError("injected first-start interruption")

    monkeypatch.setattr(identity_module, "_create_fresh_canonical", fail_create)
    with InstanceLock(data) as lock, pytest.raises(DatabaseIdentityError, match="injected"):
        migrate_live_database(lock)

    assert legacy.is_file() and not canonical.exists()
    attempt = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sqlite3,sys; d=sqlite3.connect(sys.argv[1]); "
            "d.execute('CREATE TABLE split(x)')",
            str(legacy),
        ],
        capture_output=True,
        check=False,
    )
    assert attempt.returncode != 0
    with pytest.raises(DatabaseIdentityError, match="initialization was interrupted"):
        select_database(data)

    monkeypatch.setattr(identity_module, "_create_fresh_canonical", original_create)
    with InstanceLock(data) as lock:
        assert migrate_live_database(lock) == canonical
    assert canonical.is_file() and select_database(data) == canonical


@pytest.mark.parametrize(
    ("name", "payload"),
    [
        (identity_module._PENDING_GUARD_STAGING, identity_module._PENDING_GUARD_PAYLOAD),
        (identity_module._TOMBSTONE_STAGING, identity_module._TOMBSTONE_PAYLOAD),
    ],
)
def test_marker_short_write_never_publishes_partial_staging(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    payload: bytes,
) -> None:
    data = tmp_path / "data"
    data.mkdir()
    original_write = os.write
    calls = 0

    def fail_after_prefix(descriptor: int, content) -> int:
        nonlocal calls
        calls += 1
        if calls == 1:
            return original_write(descriptor, content[:4])
        raise OSError("injected marker write failure")

    monkeypatch.setattr(os, "write", fail_after_prefix)
    with pytest.raises(DatabaseIdentityError, match="could not be staged"):
        identity_module._write_marker_staging(data, name, payload)

    assert not (data / name).exists()
    assert list(data.glob(f"{name}.tmp-*")) == []
    monkeypatch.setattr(os, "write", original_write)
    assert identity_module._write_marker_staging(data, name, payload) == data / name


def test_fresh_guard_race_with_legacy_writer_is_cleanly_recovered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = tmp_path / "data"
    data.mkdir()
    legacy = data / LEGACY_DATABASE_FILENAME
    original_link = os.link

    def raced_link(source: Path | str, target: Path | str, **kwargs) -> None:
        if Path(target) == legacy:
            _database(legacy, value="legacy-race")
        original_link(source, target, **kwargs)

    monkeypatch.setattr(os, "link", raced_link)
    with InstanceLock(data) as lock, pytest.raises(DatabaseIdentityError, match="appeared"):
        migrate_live_database(lock)

    monkeypatch.setattr(os, "link", original_link)
    with InstanceLock(data) as lock:
        canonical = migrate_live_database(lock)
    assert _probe(canonical) == ("legacy-race", latest_version())
    assert not (data / identity_module._PENDING_GUARD_STAGING).exists()


def test_clean_legacy_database_moves_to_canonical_name(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    legacy = data / LEGACY_DATABASE_FILENAME
    canonical = data / DATABASE_FILENAME
    _database(legacy)

    with InstanceLock(data) as lock:
        result = migrate_live_database(lock)

    assert result == canonical
    assert legacy.is_file()  # exact invalid-SQLite compatibility guard for older binaries
    assert _probe(canonical) == ("preserved", latest_version())
    assert not any(
        (data / f"{LEGACY_DATABASE_FILENAME}{suffix}").exists()
        for suffix in ("-wal", "-shm", "-journal")
    )


def test_committed_dirty_wal_survives_filename_migration(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    legacy = data / LEGACY_DATABASE_FILENAME
    script = "\n".join(
        [
            "import os, sqlite3, sys",
            "db = sqlite3.connect(sys.argv[1])",
            "db.execute('PRAGMA journal_mode = WAL')",
            "db.execute('PRAGMA wal_autocheckpoint = 0')",
            "db.execute('CREATE TABLE identity_probe (value TEXT NOT NULL)')",
            "db.execute(\"INSERT INTO identity_probe VALUES ('dirty-wal-row')\")",
            "db.execute(f'PRAGMA user_version = {sys.argv[2]}')",
            "db.commit()",
            "os._exit(0)",
        ]
    )
    subprocess.run([sys.executable, "-c", script, str(legacy), str(latest_version())], check=True)
    wal = data / f"{LEGACY_DATABASE_FILENAME}-wal"
    assert wal.is_file() and wal.stat().st_size > 0
    (data / f"{LEGACY_DATABASE_FILENAME}-shm").unlink(missing_ok=True)

    with InstanceLock(data) as lock:
        canonical = migrate_live_database(lock)

    assert _probe(canonical) == ("dirty-wal-row", latest_version())
    assert {path.name for path in data.iterdir()} == {
        DATABASE_FILENAME,
        LEGACY_DATABASE_FILENAME,
    }


def test_both_database_names_fail_without_touching_either(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    canonical = data / DATABASE_FILENAME
    legacy = data / LEGACY_DATABASE_FILENAME
    _database(canonical, value="canonical")
    _database(legacy, value="legacy")
    before = {path.name: path.read_bytes() for path in (canonical, legacy)}

    with InstanceLock(data) as lock, pytest.raises(DatabaseIdentityError, match="Both"):
        migrate_live_database(lock)

    assert {path.name: path.read_bytes() for path in (canonical, legacy)} == before


def test_orphan_sidecar_fails_instead_of_looking_like_a_fresh_install(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    orphan = data / f"{LEGACY_DATABASE_FILENAME}-wal"
    orphan.write_bytes(b"committed-state-might-live-here")

    with pytest.raises(DatabaseIdentityError, match="Orphan database sidecar"):
        select_database(data)

    assert orphan.read_bytes() == b"committed-state-might-live-here"
    assert not (data / DATABASE_FILENAME).exists()


def test_corrupt_legacy_database_is_left_in_place(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    legacy = data / LEGACY_DATABASE_FILENAME
    legacy.write_bytes(b"not a SQLite database")
    before = legacy.read_bytes()

    with InstanceLock(data) as lock, pytest.raises(DatabaseIdentityError, match="prepared safely"):
        migrate_live_database(lock)

    assert legacy.read_bytes() == before
    assert not (data / DATABASE_FILENAME).exists()


def test_linked_legacy_database_is_refused(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    external = tmp_path / "external.db"
    _database(external)
    legacy = data / LEGACY_DATABASE_FILENAME
    try:
        legacy.symlink_to(external)
    except OSError as exc:
        pytest.skip(f"Symbolic links are unavailable in this environment: {exc}")

    with pytest.raises(DatabaseIdentityError, match="not a regular local file"):
        select_database(data)

    assert _probe(external) == ("preserved", latest_version())


def test_active_reader_blocks_cutover_without_creating_canonical_branch(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    legacy = data / LEGACY_DATABASE_FILENAME
    _database(legacy, value="first")
    writer = sqlite3.connect(legacy)
    reader = sqlite3.connect(legacy)
    try:
        assert writer.execute("PRAGMA journal_mode = WAL").fetchone() == ("wal",)
        reader.execute("BEGIN")
        assert reader.execute("SELECT COUNT(*) FROM identity_probe").fetchone() == (1,)
        writer.execute("INSERT INTO identity_probe VALUES ('second')")
        writer.commit()
        writer.close()

        with InstanceLock(data) as lock, pytest.raises(DatabaseIdentityError, match="WAL is busy"):
            migrate_live_database(lock)
    finally:
        reader.close()
        with contextlib.suppress(sqlite3.Error):
            writer.close()

    assert not (data / DATABASE_FILENAME).exists()
    db = sqlite3.connect(legacy)
    try:
        assert db.execute("SELECT value FROM identity_probe ORDER BY rowid").fetchall() == [
            ("first",),
            ("second",),
        ]
    finally:
        db.close()


def test_interrupted_guard_publication_leaves_data_usable_and_retry_succeeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = tmp_path / "data"
    data.mkdir()
    legacy = data / LEGACY_DATABASE_FILENAME
    _database(legacy)
    canonical = data / DATABASE_FILENAME
    original_rename = identity_module._rename_no_replace

    def blocked_rename(source: Path, target: Path) -> None:
        if source == legacy:
            raise PermissionError("injected sharing violation")
        original_rename(source, target)

    monkeypatch.setattr(identity_module, "_rename_no_replace", blocked_rename)
    with (
        InstanceLock(data) as lock,
        pytest.raises(DatabaseIdentityError, match="publication was interrupted"),
    ):
        migrate_live_database(lock)

    assert _probe(legacy) == ("preserved", latest_version())
    assert _probe(canonical) == ("preserved", latest_version())
    assert legacy.samefile(canonical)

    monkeypatch.setattr(identity_module, "_rename_no_replace", original_rename)
    with InstanceLock(data) as lock:
        canonical = migrate_live_database(lock)
    assert _probe(canonical) == ("preserved", latest_version())
    assert not legacy.samefile(canonical)


def test_destination_race_never_overwrites_canonical_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = tmp_path / "data"
    data.mkdir()
    legacy = data / LEGACY_DATABASE_FILENAME
    canonical = data / DATABASE_FILENAME
    _database(legacy, value="legacy")
    original_link = os.link

    def raced_link(
        source: Path | str,
        target: Path | str,
        *,
        src_dir_fd=None,
        dst_dir_fd=None,
        follow_symlinks: bool = True,
    ) -> None:
        if Path(target) == canonical:
            _database(canonical, value="canonical-race")
        original_link(
            source,
            target,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
            follow_symlinks=follow_symlinks,
        )

    monkeypatch.setattr(os, "link", raced_link)
    with InstanceLock(data) as lock, pytest.raises(DatabaseIdentityError, match="appeared"):
        migrate_live_database(lock)

    assert _probe(canonical) == ("canonical-race", latest_version())
    assert _probe(legacy) == ("legacy", latest_version())


def test_source_swap_is_detected_before_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = tmp_path / "data"
    data.mkdir()
    legacy = data / LEGACY_DATABASE_FILENAME
    parked = data / "original-preserved.db"
    replacement = data / "replacement.db"
    _database(legacy, value="original")
    _database(replacement, value="replacement")
    original_prepare = identity_module._prepare_single_file_database

    def swap_after_prepare(database: Path, expected) -> None:
        original_prepare(database, expected)
        database.rename(parked)
        replacement.rename(database)

    monkeypatch.setattr(identity_module, "_prepare_single_file_database", swap_after_prepare)
    with InstanceLock(data) as lock, pytest.raises(DatabaseIdentityError, match="changed"):
        migrate_live_database(lock)

    assert _probe(parked) == ("original", latest_version())
    assert _probe(legacy) == ("replacement", latest_version())
    assert not (data / DATABASE_FILENAME).exists()


def test_source_swap_at_publication_rolls_back_the_created_canonical_link(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = tmp_path / "data"
    data.mkdir()
    legacy = data / LEGACY_DATABASE_FILENAME
    canonical = data / DATABASE_FILENAME
    parked = data / "original-preserved.db"
    replacement = data / "replacement.db"
    _database(legacy, value="original")
    _database(replacement, value="replacement")
    original_link = os.link

    def swap_during_link(source: Path | str, target: Path | str, **kwargs) -> None:
        if Path(target) == canonical:
            Path(source).rename(parked)
            replacement.rename(source)
        original_link(source, target, **kwargs)

    monkeypatch.setattr(os, "link", swap_during_link)
    with InstanceLock(data) as lock, pytest.raises(DatabaseIdentityError, match="changed"):
        migrate_live_database(lock)

    assert not canonical.exists()
    assert _probe(parked) == ("original", latest_version())
    assert _probe(legacy) == ("replacement", latest_version())


def test_source_swap_during_final_guard_publication_preserves_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = tmp_path / "data"
    data.mkdir()
    legacy = data / LEGACY_DATABASE_FILENAME
    canonical = data / DATABASE_FILENAME
    replacement = data / "replacement.db"
    parked = data / identity_module._CUTOVER_PARKED
    _database(legacy, value="original")
    _database(replacement, value="replacement")
    original_rename = identity_module._rename_no_replace

    def swap_before_park(source: Path, target: Path) -> None:
        if source == legacy:
            os.replace(replacement, legacy)
        original_rename(source, target)

    monkeypatch.setattr(identity_module, "_rename_no_replace", swap_before_park)
    with InstanceLock(data) as lock, pytest.raises(DatabaseIdentityError, match="changed"):
        migrate_live_database(lock)

    assert _probe(canonical) == ("original", latest_version())
    assert _probe(legacy) == ("replacement", latest_version())
    assert not replacement.exists()
    assert not parked.exists()


def test_legacy_writer_race_after_parking_is_never_overwritten(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = tmp_path / "data"
    data.mkdir()
    legacy = data / LEGACY_DATABASE_FILENAME
    canonical = data / DATABASE_FILENAME
    parked = data / identity_module._CUTOVER_PARKED
    _database(legacy, value="original")
    original_link = os.link

    def race_guard_link(source: Path | str, target: Path | str, **kwargs) -> None:
        if Path(source).name == identity_module._TOMBSTONE_STAGING and Path(target) == legacy:
            _database(legacy, value="legacy-race")
        original_link(source, target, **kwargs)

    monkeypatch.setattr(os, "link", race_guard_link)
    with InstanceLock(data) as lock, pytest.raises(DatabaseIdentityError, match="appeared"):
        migrate_live_database(lock)

    assert _probe(canonical) == ("original", latest_version())
    assert _probe(legacy) == ("legacy-race", latest_version())
    assert parked.samefile(canonical)
    with InstanceLock(data) as lock, pytest.raises(DatabaseIdentityError, match="appeared"):
        migrate_live_database(lock)


def test_retry_finishes_a_crash_after_guard_link_before_park_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = tmp_path / "data"
    data.mkdir()
    legacy = data / LEGACY_DATABASE_FILENAME
    canonical = data / DATABASE_FILENAME
    parked = data / identity_module._CUTOVER_PARKED
    _database(legacy)
    original_remove = identity_module._remove_expected_parked

    def interrupt_cleanup(
        _data_dir: Path,
        target: Path,
        _expected,
        *,
        label: str,
        **_cleanup_invariants,
    ) -> None:
        assert target == parked and label == "Legacy database"
        assert legacy.read_bytes() == identity_module._TOMBSTONE_PAYLOAD
        raise DatabaseIdentityError("injected cleanup interruption")

    monkeypatch.setattr(identity_module, "_remove_expected_parked", interrupt_cleanup)
    with InstanceLock(data) as lock, pytest.raises(DatabaseIdentityError, match="injected"):
        migrate_live_database(lock)

    assert _probe(canonical) == ("preserved", latest_version())
    assert parked.samefile(canonical)
    with pytest.raises(DatabaseIdentityError, match="cutover was interrupted"):
        select_database(data)

    monkeypatch.setattr(identity_module, "_remove_expected_parked", original_remove)
    with InstanceLock(data) as lock:
        assert migrate_live_database(lock) == canonical
    assert _probe(canonical) == ("preserved", latest_version())
    assert not parked.exists()


def test_canonical_swap_before_park_cleanup_preserves_original_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = tmp_path / "data"
    data.mkdir()
    legacy = data / LEGACY_DATABASE_FILENAME
    canonical = data / DATABASE_FILENAME
    parked = data / identity_module._CUTOVER_PARKED
    replacement = data / "replacement.db"
    _database(legacy, value="original")
    _database(replacement, value="replacement")
    original_remove = identity_module._remove_expected_parked

    def swap_canonical_before_cleanup(*args, **kwargs) -> None:
        os.replace(replacement, canonical)
        original_remove(*args, **kwargs)

    monkeypatch.setattr(
        identity_module,
        "_remove_expected_parked",
        swap_canonical_before_cleanup,
    )
    with InstanceLock(data) as lock, pytest.raises(DatabaseIdentityError, match="Kira database"):
        migrate_live_database(lock)

    assert _probe(canonical) == ("replacement", latest_version())
    assert _probe(parked) == ("original", latest_version())
    assert legacy.read_bytes() == identity_module._TOMBSTONE_PAYLOAD


def test_unrecognized_parked_file_is_restored_without_clobber(
    tmp_path: Path,
) -> None:
    data = tmp_path / "data"
    data.mkdir()
    legacy = data / LEGACY_DATABASE_FILENAME
    canonical = data / DATABASE_FILENAME
    parked = data / identity_module._CUTOVER_PARKED
    parked.write_bytes(b"operator-owned unexpected bytes")

    with (
        InstanceLock(data) as lock,
        pytest.raises(DatabaseIdentityError, match="Unrecognized"),
    ):
        migrate_live_database(lock)

    assert legacy.read_bytes() == b"operator-owned unexpected bytes"
    assert not parked.exists() and not canonical.exists()


@pytest.mark.parametrize("seed_legacy", [False, True])
def test_retry_recovers_a_crash_after_parking_the_old_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    seed_legacy: bool,
) -> None:
    data = tmp_path / "data"
    data.mkdir()
    legacy = data / LEGACY_DATABASE_FILENAME
    canonical = data / DATABASE_FILENAME
    parked = data / identity_module._CUTOVER_PARKED
    if seed_legacy:
        _database(legacy)
    original_install = identity_module._install_tombstone_no_clobber

    def interrupt_after_park(_data_dir: Path, target: Path) -> None:
        assert target == legacy and not legacy.exists() and parked.is_file()
        raise DatabaseIdentityError("injected post-park interruption")

    monkeypatch.setattr(
        identity_module,
        "_install_tombstone_no_clobber",
        interrupt_after_park,
    )
    with InstanceLock(data) as lock, pytest.raises(DatabaseIdentityError, match="injected"):
        migrate_live_database(lock)

    assert canonical.is_file() and parked.is_file() and not legacy.exists()
    with pytest.raises(DatabaseIdentityError, match="cutover was interrupted"):
        select_database(data)

    monkeypatch.setattr(
        identity_module,
        "_install_tombstone_no_clobber",
        original_install,
    )
    with InstanceLock(data) as lock:
        assert migrate_live_database(lock) == canonical
    assert canonical.is_file() and legacy.is_file() and not parked.exists()
    assert select_database(data) == canonical


def test_hardlinked_legacy_database_is_refused(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    legacy = data / LEGACY_DATABASE_FILENAME
    external_link = tmp_path / "legacy-hardlink.db"
    _database(legacy)
    try:
        os.link(legacy, external_link)
    except OSError as exc:
        pytest.skip(f"Hard links are unavailable in this environment: {exc}")

    with InstanceLock(data) as lock, pytest.raises(DatabaseIdentityError, match="filesystem links"):
        migrate_live_database(lock)

    assert _probe(legacy) == ("preserved", latest_version())
    assert _probe(external_link) == ("preserved", latest_version())


def test_legacy_tombstone_blocks_an_old_hardcoded_sqlite_writer(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    legacy = data / LEGACY_DATABASE_FILENAME
    _database(legacy)
    with InstanceLock(data) as lock:
        canonical = migrate_live_database(lock)
    before = {path: path.read_bytes() for path in (canonical, legacy)}
    script = (
        "import sqlite3, sys; db=sqlite3.connect(sys.argv[1]); db.execute('CREATE TABLE split(x)')"
    )

    attempt = subprocess.run(
        [sys.executable, "-c", script, str(legacy)], capture_output=True, check=False
    )

    assert attempt.returncode != 0
    assert {path: path.read_bytes() for path in (canonical, legacy)} == before
    assert select_database(data) == canonical


def test_externally_hardlinked_tombstone_is_refused(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    legacy = data / LEGACY_DATABASE_FILENAME
    _database(legacy)
    with InstanceLock(data) as lock:
        canonical = migrate_live_database(lock)
    external_link = tmp_path / "tombstone-hardlink"
    try:
        os.link(legacy, external_link)
    except OSError as exc:
        pytest.skip(f"Hard links are unavailable in this environment: {exc}")

    with InstanceLock(data) as lock, pytest.raises(DatabaseIdentityError, match="guard.*links"):
        migrate_live_database(lock)

    assert _probe(canonical) == ("preserved", latest_version())


async def test_filename_migration_precedes_schema_snapshot_and_upgrade(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    legacy = data / LEGACY_DATABASE_FILENAME
    _database(legacy, version=latest_version() - 1)

    with InstanceLock(data) as lock:
        canonical = migrate_live_database(lock)
        db = await connect(canonical)
        await db.close()

    snapshots = list((data / "backups").glob("kira-backup-*-pre-migration-v*-to-v*"))
    assert len(snapshots) == 1
    assert verify_backup(snapshots[0])["database"] == DATABASE_FILENAME
    assert _probe(canonical)[1] == latest_version()
