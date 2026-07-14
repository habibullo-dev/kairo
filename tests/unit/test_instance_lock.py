"""Only one Kairo process may own a configured data root."""

from __future__ import annotations

from pathlib import Path

import pytest

from jarvis.persistence.instance_lock import (
    InstanceAlreadyRunning,
    InstanceLock,
    instance_lock_path,
)


def test_lock_lives_beside_movable_data_root(tmp_path: Path) -> None:
    data = tmp_path / "data"
    assert instance_lock_path(data) == tmp_path / ".data.kairo-instance.lock"


def test_second_owner_fails_without_waiting_and_release_allows_reacquire(tmp_path: Path) -> None:
    data = tmp_path / "data"
    first = InstanceLock(data).acquire()
    try:
        with pytest.raises(InstanceAlreadyRunning, match="already running"):
            InstanceLock(data).acquire()
    finally:
        first.release()

    with InstanceLock(data):
        assert instance_lock_path(data).is_file()


def test_release_is_idempotent(tmp_path: Path) -> None:
    lock = InstanceLock(tmp_path / "data")
    lock.acquire()
    lock.release()
    lock.release()
