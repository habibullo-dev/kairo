"""Configured-data writer CLIs cannot race the Kira database cutover."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

import kira.config as config_module
from kira.cli import dream as dream_module
from kira.cli import graph as graph_module
from kira.persistence.instance_lock import InstanceLock
from kira.persistence.migrations import latest_version
from kira.persistence.reset_recovery import ResetRecoveryError


def _config(data: Path):
    return SimpleNamespace(
        root=data.parent,
        data_dir=data,
        require=lambda *_services: None,
        ensure_dirs=lambda: data.mkdir(parents=True, exist_ok=True),
    )


def _legacy_database(path: Path) -> None:
    path.parent.mkdir(parents=True)
    db = sqlite3.connect(path)
    try:
        db.execute("CREATE TABLE marker (value TEXT)")
        db.execute("INSERT INTO marker VALUES ('preserved')")
        db.execute(f"PRAGMA user_version = {latest_version()}")
        db.commit()
    finally:
        db.close()


@pytest.mark.parametrize(
    ("invoke", "blocked_copy"),
    [
        (lambda: graph_module.graph_cli(["rebuild"]), "Graph command blocked:"),
        (lambda: dream_module.dream_cli(["run", "nightly_review"]), "Dream command blocked:"),
    ],
)
def test_configured_data_commands_respect_the_runtime_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    invoke,
    blocked_copy: str,
) -> None:
    data = tmp_path / "data"
    config = _config(data)
    monkeypatch.setattr(config_module, "load_config", lambda **_kwargs: config)

    with InstanceLock(data):
        assert invoke() == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert blocked_copy in captured.err
    assert not data.exists()


def test_graph_cli_migrates_legacy_identity_before_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = tmp_path / "data"
    legacy = data / "jarvis.db"
    _legacy_database(legacy)
    config = _config(data)
    dispatched: list[Path] = []

    async def rebuild(database: Path) -> int:
        dispatched.append(database)
        return 0

    monkeypatch.setattr(config_module, "load_config", lambda **_kwargs: config)
    monkeypatch.setattr(graph_module, "_run_rebuild", rebuild)

    assert graph_module.graph_cli(["rebuild"]) == 0

    assert dispatched == [data / "kira.db"]
    assert legacy.is_file() and (data / "kira.db").is_file()


def test_dream_cli_passes_the_locked_config_and_database_to_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = tmp_path / "data"
    legacy = data / "jarvis.db"
    _legacy_database(legacy)
    config = _config(data)
    dispatched: list[tuple[object, Path, str]] = []
    loads: list[object] = []

    def load_config(**kwargs):
        loads.append(kwargs)
        return config

    async def run(config_arg, database: Path, job: str) -> int:
        dispatched.append((config_arg, database, job))
        return 0

    monkeypatch.setattr(config_module, "load_config", load_config)
    monkeypatch.setattr(dream_module, "_run", run)

    assert dream_module.dream_cli(["run", "nightly_review"]) == 0

    assert loads == [{}]
    assert dispatched == [(config, data / "kira.db", "nightly_review")]


@pytest.mark.parametrize(
    ("module", "invoke", "blocked_copy"),
    [
        (graph_module, lambda: graph_module.graph_cli(["rebuild"]), "Graph command blocked:"),
        (
            dream_module,
            lambda: dream_module.dream_cli(["run", "nightly_review"]),
            "Dream command blocked:",
        ),
    ],
)
def test_interrupted_reset_blocks_data_commands_before_ensure_dirs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    module,
    invoke,
    blocked_copy: str,
) -> None:
    data = tmp_path / "data"
    calls: list[str] = []
    config = SimpleNamespace(
        root=tmp_path,
        data_dir=data,
        require=lambda *_services: None,
        ensure_dirs=lambda: calls.append("ensure_dirs"),
    )

    def refuse(_config, _barrier, _lock) -> bool:
        calls.append("recover")
        raise ResetRecoveryError("ambiguous interrupted reset")

    monkeypatch.setattr(config_module, "load_config", lambda **_kwargs: config)
    monkeypatch.setattr(module, "recover_interrupted_reset", refuse)

    assert invoke() == 1
    assert calls == ["recover"]
    assert not data.exists()
    assert blocked_copy in capsys.readouterr().err
