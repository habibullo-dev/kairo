"""Kira and legacy processes remain mutually exclusive for one data root."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import BinaryIO

import pytest

import jarvis.persistence.instance_lock as lock_module
from jarvis.persistence.instance_lock import (
    InstanceAlreadyRunning,
    InstanceLock,
    ResetBarrier,
    ResetMaintenanceBusy,
    instance_lock_path,
    instance_lock_paths,
    legacy_instance_lock_path,
    reset_barrier_path,
)

RUNNING_MESSAGE = (
    "Kira may already be running for this data directory, or its instance lock is unavailable. "
    "Stop it before maintenance and verify directory access."
)

_KIRA_CHILD = r"""
import sys
from pathlib import Path
from jarvis.persistence.instance_lock import InstanceAlreadyRunning, InstanceLock

try:
    InstanceLock(Path(sys.argv[1])).acquire()
except InstanceAlreadyRunning as exc:
    print(str(exc))
    raise SystemExit(23)
raise SystemExit(0)
"""

_RAW_CHILD = r"""
import os
import sys
from pathlib import Path
from jarvis.persistence.instance_lock import instance_lock_path, legacy_instance_lock_path

path_fn = legacy_instance_lock_path if sys.argv[2] == "legacy" else instance_lock_path
path = path_fn(Path(sys.argv[1]))
handle = path.open("a+b")
try:
    handle.seek(0, os.SEEK_END)
    if handle.tell() == 0:
        handle.write(b"\0")
        handle.flush()
    handle.seek(0)
    if os.name == "nt":
        import msvcrt
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
except OSError:
    handle.close()
    raise SystemExit(23)
raise SystemExit(0)
"""


def _raw_lock(path: Path) -> BinaryIO:
    """Acquire one byte exactly as a legacy or canonical-only executable would."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+b")
    try:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        raise
    return handle


def _raw_unlock(handle: BinaryIO) -> None:
    try:
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


def _child(script: str, data: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", script, str(data), *args],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )


def test_locks_live_beside_movable_data_root_in_compatibility_order(tmp_path: Path) -> None:
    data = tmp_path / "data"
    legacy = tmp_path / ".data.kairo-instance.lock"
    canonical = tmp_path / ".data.kira-instance.lock"
    assert legacy_instance_lock_path(data) == legacy
    assert instance_lock_path(data) == canonical
    assert instance_lock_paths(data) == (legacy, canonical)


def test_reset_barrier_serializes_online_writers_without_owning_instance_lock(
    tmp_path: Path,
) -> None:
    data = tmp_path / "data"
    barrier_path = tmp_path / ".data.kira-reset-barrier.lock"
    assert reset_barrier_path(data) == barrier_path

    barrier = ResetBarrier(data)
    with pytest.raises(RuntimeError, match="not currently acquired"):
        barrier.owned_data_dir()
    with barrier:
        assert barrier.owned_data_dir() == data.resolve()
        with pytest.raises(ResetMaintenanceBusy):
            ResetBarrier(data).acquire()
        with InstanceLock(data):
            pass
    assert barrier_path.read_bytes() == b"\0"
    with ResetBarrier(data):
        pass


def test_reset_barrier_release_is_idempotent(tmp_path: Path) -> None:
    barrier = ResetBarrier(tmp_path / "data").acquire()
    barrier.release()
    barrier.release()
    with pytest.raises(RuntimeError, match="not currently acquired"):
        barrier.owned_data_dir()


def test_second_owner_fails_without_waiting_and_release_allows_reacquire(tmp_path: Path) -> None:
    data = tmp_path / "data"
    first = InstanceLock(data).acquire()
    try:
        with pytest.raises(InstanceAlreadyRunning) as blocked:
            InstanceLock(data).acquire()
        assert str(blocked.value) == RUNNING_MESSAGE
    finally:
        first.release()

    with InstanceLock(data):
        assert all(path.is_file() for path in instance_lock_paths(data))
    assert all(path.is_file() for path in instance_lock_paths(data))


def test_legacy_only_owner_blocks_kira_without_creating_canonical_lock(tmp_path: Path) -> None:
    data = tmp_path / "data"
    legacy = _raw_lock(legacy_instance_lock_path(data))
    try:
        with pytest.raises(InstanceAlreadyRunning) as blocked:
            InstanceLock(data).acquire()
        assert str(blocked.value) == RUNNING_MESSAGE
        assert not instance_lock_path(data).exists()
    finally:
        _raw_unlock(legacy)


def test_kira_dual_lock_blocks_a_legacy_only_owner(tmp_path: Path) -> None:
    data = tmp_path / "data"
    with InstanceLock(data), pytest.raises(OSError):
        _raw_lock(legacy_instance_lock_path(data))


def test_canonical_conflict_rolls_back_the_legacy_lock(tmp_path: Path) -> None:
    data = tmp_path / "data"
    canonical = _raw_lock(instance_lock_path(data))
    try:
        with pytest.raises(InstanceAlreadyRunning) as blocked:
            InstanceLock(data).acquire()
        assert str(blocked.value) == RUNNING_MESSAGE
        legacy = _raw_lock(legacy_instance_lock_path(data))
        _raw_unlock(legacy)
    finally:
        _raw_unlock(canonical)


def test_release_is_idempotent(tmp_path: Path) -> None:
    lock = InstanceLock(tmp_path / "data")
    lock.acquire()
    lock.release()
    lock.release()


def test_owned_data_dir_is_available_only_while_both_locks_are_held(tmp_path: Path) -> None:
    data = tmp_path / "data"
    lock = InstanceLock(data)
    with pytest.raises(RuntimeError, match="does not currently own"):
        lock.owned_data_dir()
    with lock:
        assert lock.owned_data_dir() == data.resolve()
    with pytest.raises(RuntimeError, match="does not currently own"):
        lock.owned_data_dir()


def test_repeated_acquire_is_rejected_without_releasing_ownership(tmp_path: Path) -> None:
    data = tmp_path / "data"
    lock = InstanceLock(data)
    assert lock.acquire() is lock
    try:
        with pytest.raises(RuntimeError, match="already acquired"):
            lock.acquire()
        with pytest.raises(InstanceAlreadyRunning):
            InstanceLock(data).acquire()
    finally:
        lock.release()


def test_context_exception_releases_both_locks_without_unlinking_them(tmp_path: Path) -> None:
    data = tmp_path / "data"
    with pytest.raises(RuntimeError, match="injected"), InstanceLock(data):
        raise RuntimeError("injected")
    assert all(path.is_file() for path in instance_lock_paths(data))
    with InstanceLock(data):
        pass


def test_interrupt_during_canonical_acquire_releases_legacy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = tmp_path / "data"
    canonical = instance_lock_path(data)
    real_acquire = lock_module._acquire_handle

    def interrupt_canonical(path: Path) -> BinaryIO:
        if path == canonical:
            raise KeyboardInterrupt
        return real_acquire(path)

    monkeypatch.setattr(lock_module, "_acquire_handle", interrupt_canonical)
    with pytest.raises(KeyboardInterrupt):
        InstanceLock(data).acquire()
    legacy = _raw_lock(legacy_instance_lock_path(data))
    _raw_unlock(legacy)


def test_release_drains_both_handles_before_reraising_interrupt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = tmp_path / "data"
    lock = InstanceLock(data).acquire()
    real_release = lock_module._release_handle
    released = 0

    def release_then_interrupt(handle: BinaryIO) -> None:
        nonlocal released
        real_release(handle)
        released += 1
        if released == 1:
            raise KeyboardInterrupt

    monkeypatch.setattr(lock_module, "_release_handle", release_then_interrupt)
    with pytest.raises(KeyboardInterrupt):
        lock.release()
    assert released == 2
    for path in instance_lock_paths(data):
        handle = _raw_lock(path)
        _raw_unlock(handle)


def test_real_legacy_process_blocks_kira_without_touching_data_or_canonical(
    tmp_path: Path,
) -> None:
    data = tmp_path / "data"
    data.mkdir()
    sentinel = data / "owner-data.bin"
    sentinel.write_bytes(b"preserve-this")
    legacy_path = legacy_instance_lock_path(data)
    legacy = _raw_lock(legacy_path)
    try:
        child = _child(_KIRA_CHILD, data)
    finally:
        _raw_unlock(legacy)

    assert child.returncode == 23, child.stderr
    assert child.stdout.strip() == RUNNING_MESSAGE
    assert sentinel.read_bytes() == b"preserve-this"
    assert list(data.iterdir()) == [sentinel]
    assert not instance_lock_path(data).exists()
    assert legacy_path.read_bytes() == b"\0"


@pytest.mark.parametrize("path_kind", ["legacy", "canonical"])
def test_kira_dual_lock_blocks_real_single_lock_processes(tmp_path: Path, path_kind: str) -> None:
    data = tmp_path / "data"
    with InstanceLock(data):
        child = _child(_RAW_CHILD, data, path_kind)
    assert child.returncode == 23, child.stderr


def test_blocked_entrypoint_does_not_prepare_runtime_dirs_or_logging(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import jarvis.__main__ as entry
    import jarvis.config as config_module
    import jarvis.observability as observability

    calls: list[str] = []
    config = SimpleNamespace(
        root=tmp_path,
        data_dir=tmp_path / "data",
        logs_dir=tmp_path / "logs",
        logging=SimpleNamespace(model_dump=lambda: {}),
        require=lambda *_services: None,
        ensure_dirs=lambda: calls.append("ensure_dirs"),
    )

    class BlockedLock:
        def __init__(self, _data_dir: Path) -> None:
            pass

        def __enter__(self):
            raise InstanceAlreadyRunning(RUNNING_MESSAGE)

        def __exit__(self, *_args) -> None:
            pass

    monkeypatch.setattr(sys, "argv", ["kira"])
    monkeypatch.setattr(config_module, "load_config", lambda **_kwargs: config)
    monkeypatch.setattr(lock_module, "InstanceLock", BlockedLock)
    monkeypatch.setattr(
        observability, "configure_logging", lambda *_args, **_kwargs: calls.append("logging")
    )

    with pytest.raises(SystemExit) as exited:
        entry.main()

    assert exited.value.code == 1
    assert calls == []
    assert not config.data_dir.exists() and not config.logs_dir.exists()
    assert "Startup blocked" in capsys.readouterr().out


def test_entrypoint_migrates_database_under_lock_before_logging_and_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import jarvis.__main__ as entry
    import jarvis.cli.repl as repl_module
    import jarvis.config as config_module
    import jarvis.observability as observability
    import jarvis.persistence.database_identity as identity_module
    import jarvis.persistence.reset_recovery as recovery_module

    calls: list[str] = []
    data = tmp_path / "data"
    canonical = data / "kira.db"

    def ensure_dirs() -> None:
        calls.append("ensure_dirs")
        data.mkdir(parents=True)

    config = SimpleNamespace(
        root=tmp_path,
        data_dir=data,
        logs_dir=tmp_path / "logs",
        logging=SimpleNamespace(model_dump=lambda: {}),
        require=lambda *_services: None,
        ensure_dirs=ensure_dirs,
    )

    def migrate(lock: InstanceLock) -> Path:
        assert lock.owned_data_dir() == data.resolve()
        calls.append("migrate")
        return canonical

    def recover(_config, barrier: ResetBarrier, lock: InstanceLock) -> bool:
        assert barrier.owned_data_dir() == data.resolve()
        assert lock.owned_data_dir() == data.resolve()
        calls.append("recover")
        return False

    async def run_repl(
        _config,
        *,
        resume: bool,
        console,
        database: Path,  # noqa: ANN001
    ) -> None:
        assert not resume and console is not None and database == canonical
        calls.append("runtime")

    monkeypatch.setattr(sys, "argv", ["kira"])
    monkeypatch.setattr(config_module, "load_config", lambda **_kwargs: config)
    monkeypatch.setattr(identity_module, "migrate_live_database", migrate)
    monkeypatch.setattr(recovery_module, "recover_interrupted_reset", recover)
    monkeypatch.setattr(
        observability,
        "configure_logging",
        lambda *_args, **_kwargs: calls.append("logging"),
    )
    monkeypatch.setattr(repl_module, "run_repl", run_repl)

    entry.main()

    assert calls == ["recover", "ensure_dirs", "migrate", "logging", "runtime"]


def test_interrupted_reset_error_blocks_startup_before_directory_creation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import jarvis.__main__ as entry
    import jarvis.config as config_module
    import jarvis.observability as observability
    import jarvis.persistence.reset_recovery as recovery_module

    calls: list[str] = []
    config = SimpleNamespace(
        root=tmp_path,
        data_dir=tmp_path / "data",
        logs_dir=tmp_path / "logs",
        logging=SimpleNamespace(model_dump=lambda: {}),
        require=lambda *_services: None,
        ensure_dirs=lambda: calls.append("ensure_dirs"),
    )

    def refuse(_config, _barrier, _lock) -> bool:
        calls.append("recover")
        raise recovery_module.ResetRecoveryError("ambiguous interrupted reset")

    monkeypatch.setattr(sys, "argv", ["kira"])
    monkeypatch.setattr(config_module, "load_config", lambda **_kwargs: config)
    monkeypatch.setattr(recovery_module, "recover_interrupted_reset", refuse)
    monkeypatch.setattr(
        observability, "configure_logging", lambda *_args, **_kwargs: calls.append("logging")
    )

    with pytest.raises(SystemExit) as exited:
        entry.main()

    assert exited.value.code == 1
    assert calls == ["recover"]
    assert not config.data_dir.exists() and not config.logs_dir.exists()
    assert "ambiguous interrupted reset" in capsys.readouterr().out


def test_missing_provider_key_without_pending_reset_stays_lock_and_directory_free(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import jarvis.__main__ as entry
    import jarvis.config as config_module
    from jarvis.config import ConfigError

    def require(*_services: str) -> None:
        raise ConfigError("Missing required API key(s): ANTHROPIC_API_KEY")

    config = SimpleNamespace(
        root=tmp_path,
        data_dir=tmp_path / "data",
        logs_dir=tmp_path / "logs",
        require=require,
    )
    monkeypatch.setattr(sys, "argv", ["kira"])
    monkeypatch.setattr(config_module, "load_config", lambda **_kwargs: config)

    with pytest.raises(SystemExit) as exited:
        entry.main()

    assert exited.value.code == 1
    assert not config.data_dir.exists() and not config.logs_dir.exists()
    assert not list(tmp_path.glob(".*.lock"))
    assert "ANTHROPIC_API_KEY" in capsys.readouterr().out
