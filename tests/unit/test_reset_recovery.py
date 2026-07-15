"""Interrupted reset recovery is identity-bound, idempotent, and never deletes data."""

from __future__ import annotations

import datetime as dt
import json
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

from jarvis.cli import reset as reset_module
from jarvis.config import Config, load_config
from jarvis.persistence.db import connect
from jarvis.persistence.durable_fs import durable_rename_no_replace
from jarvis.persistence.instance_lock import InstanceLock, ResetBarrier
from jarvis.persistence.reset_recovery import (
    FAILED_FRESH_LABEL,
    RESET_FORMAT_VERSION,
    RESET_LOCATOR_SUFFIX,
    RESET_MANIFEST_DIRNAME,
    RESET_RETIRED_LOCATOR_SUFFIX,
    ResetRecoveryError,
    find_pending_reset,
    interrupted_reset_diagnostic,
    manifest_locator_payload,
    quarantine_paths,
    recover_interrupted_reset,
)


@dataclass
class _RecoveryCase:
    config: Config
    reset_id: str
    manifest: Path
    payload: dict
    move: reset_module._RootMove


async def _pending_case(
    tmp_path: Path,
    *,
    absent_logs: bool = False,
    separate_manifest_anchor: bool = False,
    external_data_parent: Path | None = None,
    external_data_root: Path | None = None,
) -> _RecoveryCase:
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "settings.yaml").write_text("{}\n", encoding="utf-8")
    config = load_config(root=tmp_path)
    if external_data_root is not None:
        data_root = external_data_root
    elif external_data_parent is not None:
        data_root = external_data_parent / "data"
    elif separate_manifest_anchor:
        data_root = Path("runtime/data")
    else:
        data_root = Path("data")
    config.paths.data_dir = data_root
    config.knowledge.dir = data_root / "knowledge"
    config.paths.logs_dir = tmp_path / "external-logs" if absent_logs else data_root / "logs"
    config.data_dir.mkdir(parents=True)
    config.knowledge_dir.mkdir(parents=True)
    if not absent_logs:
        config.logs_dir.mkdir(parents=True)
    (config.data_dir / "old-sentinel.txt").write_text("old data", encoding="utf-8")
    db = await connect(config.data_dir / "kira.db")
    await db.close()

    reset_id = "20260715T120000Z-deadbeef"
    configured = reset_module._configured_root_paths(config)
    moves, _configured = reset_module._planned_moves(
        config,
        reset_id,
        include_external_knowledge=False,
        include_external_logs=absent_logs,
        configured_roots=configured,
        config_root=config.root,
    )
    assert len(moves) == 1
    absent = reset_module._planned_absent_roots(configured, reset_id, moves)
    payload = {
        "format_version": RESET_FORMAT_VERSION,
        "reset_id": reset_id,
        "created_at": dt.datetime.now(dt.UTC).isoformat(),
        "status": "in_progress",
        "config_root": str(config.root.resolve()),
        "old_schema_version": 1,
        "old_counts": {},
        "roots": [
            {
                "roles": list(move.roles),
                "source": str(move.source),
                "quarantine": str(move.quarantine),
                "source_identity": move.source_identity.payload(),
            }
            for move in moves
        ],
        "absent_roots": [
            {"roles": list(root.roles), "source": str(root.source)} for root in absent
        ],
        "preserved": [],
        "locked_integrations": [],
    }
    manifest = reset_module._manifest_path(config, reset_id)
    reset_module._write_manifest(manifest, payload)
    return _RecoveryCase(config, reset_id, manifest, payload, moves[0])


def _config_for_data(root: Path, data: Path) -> Config:
    (root / "config").mkdir(parents=True)
    (root / "config" / "settings.yaml").write_text("{}\n", encoding="utf-8")
    config = load_config(root=root)
    config.paths.data_dir = data
    config.paths.logs_dir = data / "logs"
    config.knowledge.dir = data / "knowledge"
    config.ensure_dirs()
    config.knowledge_dir.mkdir(parents=True, exist_ok=True)
    return config


def _publish_locator(case: _RecoveryCase) -> Path:
    locator = (
        case.config.root.resolve()
        / RESET_MANIFEST_DIRNAME
        / f"{case.reset_id}{RESET_LOCATOR_SUFFIX}"
    )
    reset_module._write_manifest(
        locator,
        manifest_locator_payload(
            reset_id=case.reset_id,
            manifest=case.manifest,
            config_root=case.config.root.resolve(),
            data_root=case.config.data_dir.resolve(),
            manifest_payload=case.payload,
        ),
    )
    return locator


def _terminal_payload(case: _RecoveryCase, status: str) -> dict:
    timestamp = dt.datetime.now(dt.UTC).isoformat()
    if status == "completed":
        return {
            **case.payload,
            "status": status,
            "completed_at": timestamp,
            "fresh_schema_version": 1,
            "integrity_check": "ok",
        }
    return {
        **case.payload,
        "status": status,
        "rolled_back_at": timestamp,
        "error_type": "InterruptedResetRecovery",
    }


def _retired_locator(locator: Path) -> Path:
    return locator.with_name(
        f"{locator.name.removesuffix(RESET_LOCATOR_SUFFIX)}{RESET_RETIRED_LOCATOR_SUFFIX}"
    )


def _failed_fresh(case: _RecoveryCase) -> Path:
    source = case.move.source
    return source.with_name(f".{source.name}.{FAILED_FRESH_LABEL}-{case.reset_id}")


def _recover(case: _RecoveryCase) -> bool:
    with (
        ResetBarrier(case.config.data_dir) as barrier,
        InstanceLock(case.config.data_dir) as lock,
    ):
        return recover_interrupted_reset(case.config, barrier, lock)


def _startup_outcome(
    config: Config,
) -> tuple[str | None, bool | None, ResetRecoveryError | None]:
    diagnostic = interrupted_reset_diagnostic(config)
    try:
        with (
            ResetBarrier(config.data_dir) as barrier,
            InstanceLock(config.data_dir) as lock,
        ):
            recovered = recover_interrupted_reset(config, barrier, lock)
    except ResetRecoveryError as exc:
        return diagnostic, None, exc
    return diagnostic, recovered, None


async def test_sibling_instance_ignores_pending_manifest_for_other_data_root(
    tmp_path: Path,
) -> None:
    shared = tmp_path / "shared-state"
    workspace_a = tmp_path / "workspace-a"
    workspace_a.mkdir()
    case_a = await _pending_case(
        workspace_a,
        external_data_root=shared / "data-a",
    )
    locator_a = _publish_locator(case_a)
    durable_rename_no_replace(case_a.move.source, case_a.move.quarantine)
    workspace_b = tmp_path / "workspace-b"
    config_b = _config_for_data(workspace_b, shared / "data-b")
    manifest_before = case_a.manifest.read_bytes()
    locator_before = locator_a.read_bytes()
    quarantine_before = {
        path.relative_to(case_a.move.quarantine).as_posix(): path.read_bytes()
        for path in case_a.move.quarantine.rglob("*")
        if path.is_file()
    }

    diagnostic = interrupted_reset_diagnostic(config_b)
    recovery_error: ResetRecoveryError | None = None
    recovered: bool | None = None
    try:
        with (
            ResetBarrier(config_b.data_dir) as barrier,
            InstanceLock(config_b.data_dir) as lock,
        ):
            recovered = recover_interrupted_reset(config_b, barrier, lock)
    except ResetRecoveryError as exc:
        recovery_error = exc

    assert case_a.manifest.read_bytes() == manifest_before
    assert locator_a.read_bytes() == locator_before
    assert case_a.move.quarantine.is_dir()
    assert {
        path.relative_to(case_a.move.quarantine).as_posix(): path.read_bytes()
        for path in case_a.move.quarantine.rglob("*")
        if path.is_file()
    } == quarantine_before
    assert diagnostic is None
    assert recovery_error is None
    assert recovered is False


async def test_same_data_root_with_changed_config_anchor_remains_blocked(
    tmp_path: Path,
) -> None:
    shared_data = tmp_path / "shared-state" / "data"
    workspace_a = tmp_path / "workspace-a"
    workspace_a.mkdir()
    case_a = await _pending_case(workspace_a, external_data_root=shared_data)
    config_b = _config_for_data(tmp_path / "workspace-b", shared_data)
    manifest_before = case_a.manifest.read_bytes()

    diagnostic = interrupted_reset_diagnostic(config_b)

    assert diagnostic is not None and diagnostic.startswith("blocked (")
    assert "different workspace location" in diagnostic
    with (
        ResetBarrier(config_b.data_dir) as barrier,
        InstanceLock(config_b.data_dir) as lock,
        pytest.raises(ResetRecoveryError, match="different workspace location"),
    ):
        recover_interrupted_reset(config_b, barrier, lock)
    assert case_a.manifest.read_bytes() == manifest_before
    assert (shared_data / "old-sentinel.txt").is_file()


async def test_sibling_manifest_without_matching_locator_remains_blocked(
    tmp_path: Path,
) -> None:
    shared = tmp_path / "shared-state"
    workspace_a = tmp_path / "workspace-a"
    workspace_a.mkdir()
    case_a = await _pending_case(workspace_a, external_data_root=shared / "data-a")
    durable_rename_no_replace(case_a.move.source, case_a.move.quarantine)
    config_b = _config_for_data(tmp_path / "workspace-b", shared / "data-b")
    manifest_before = case_a.manifest.read_bytes()
    assert not list((workspace_a / RESET_MANIFEST_DIRNAME).glob("*.locator"))

    diagnostic, recovered, error = _startup_outcome(config_b)

    assert diagnostic is not None and "different workspace location" in diagnostic
    assert recovered is None
    assert error is not None and "different workspace location" in str(error)
    assert case_a.manifest.read_bytes() == manifest_before
    assert (case_a.move.quarantine / "old-sentinel.txt").is_file()


async def test_nested_sibling_data_root_remains_blocked(tmp_path: Path) -> None:
    shared = tmp_path / "shared-state"
    workspace_a = tmp_path / "workspace-a"
    workspace_a.mkdir()
    case_a = await _pending_case(workspace_a, external_data_root=shared / "data-a")
    _publish_locator(case_a)
    durable_rename_no_replace(case_a.move.source, case_a.move.quarantine)
    config_b = _config_for_data(shared, case_a.move.source / "nested-b")

    diagnostic, recovered, error = _startup_outcome(config_b)

    assert diagnostic is not None and "different workspace location" in diagnostic
    assert recovered is None
    assert error is not None and "different workspace location" in str(error)
    assert (case_a.move.quarantine / "old-sentinel.txt").is_file()
    assert config_b.data_dir.is_relative_to(case_a.move.source)


@pytest.mark.parametrize("shared_role", ["logs", "knowledge"])
async def test_sibling_with_overlapping_auxiliary_root_remains_blocked(
    tmp_path: Path,
    shared_role: str,
) -> None:
    shared = tmp_path / "shared-state"
    workspace_a = tmp_path / "workspace-a"
    workspace_a.mkdir()
    case_a = await _pending_case(workspace_a, external_data_root=shared / "data-a")
    _publish_locator(case_a)
    config_b = _config_for_data(tmp_path / "workspace-b", shared / "data-b")
    if shared_role == "logs":
        config_b.paths.logs_dir = case_a.config.logs_dir
    else:
        config_b.knowledge.dir = case_a.config.knowledge_dir

    diagnostic, recovered, error = _startup_outcome(config_b)

    assert diagnostic is not None and "different workspace location" in diagnostic
    assert recovered is None
    assert error is not None and "different workspace location" in str(error)
    assert (case_a.move.source / "old-sentinel.txt").is_file()


async def test_config_equal_to_data_parent_proves_locator_free_disjoint_sibling(
    tmp_path: Path,
) -> None:
    shared = tmp_path / "shared-state"
    shared.mkdir()
    case_a = await _pending_case(shared, external_data_root=shared / "data-a")
    durable_rename_no_replace(case_a.move.source, case_a.move.quarantine)
    config_b = _config_for_data(tmp_path / "workspace-b", shared / "data-b")
    manifest_before = case_a.manifest.read_bytes()
    assert case_a.config.root.resolve() == case_a.config.data_dir.resolve().parent
    assert not list((shared / RESET_MANIFEST_DIRNAME).glob("*.locator"))

    diagnostic, recovered, error = _startup_outcome(config_b)

    assert diagnostic is None
    assert recovered is False
    assert error is None
    assert case_a.manifest.read_bytes() == manifest_before
    assert (case_a.move.quarantine / "old-sentinel.txt").is_file()


async def test_foreign_recovery_parent_inside_current_config_root_remains_blocked(
    tmp_path: Path,
) -> None:
    workspace_b = tmp_path / "workspace-b"
    workspace_b.mkdir()
    shared = workspace_b / "shared-state"
    workspace_a = tmp_path / "workspace-a"
    workspace_a.mkdir()
    case_a = await _pending_case(workspace_a, external_data_root=shared / "data-a")
    _publish_locator(case_a)
    durable_rename_no_replace(case_a.move.source, case_a.move.quarantine)
    config_b = _config_for_data(workspace_b, shared / "data-b")
    manifest_before = case_a.manifest.read_bytes()
    quarantine_before = {
        path.relative_to(case_a.move.quarantine).as_posix(): path.read_bytes()
        for path in case_a.move.quarantine.rglob("*")
        if path.is_file()
    }
    assert case_a.manifest.is_relative_to(config_b.root.resolve())
    assert case_a.move.quarantine.parent.is_relative_to(config_b.root.resolve())

    diagnostic, recovered, error = _startup_outcome(config_b)

    assert diagnostic is not None and "different workspace location" in diagnostic
    assert recovered is None
    assert error is not None and "different workspace location" in str(error)
    assert case_a.manifest.read_bytes() == manifest_before
    assert {
        path.relative_to(case_a.move.quarantine).as_posix(): path.read_bytes()
        for path in case_a.move.quarantine.rglob("*")
        if path.is_file()
    } == quarantine_before


async def test_link_backed_foreign_locator_storage_inside_current_roots_remains_blocked(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shared = tmp_path / "shared-state"
    workspace_a = tmp_path / "workspace-a"
    workspace_a.mkdir()
    case_a = await _pending_case(workspace_a, external_data_root=shared / "data-a")
    config_b = _config_for_data(tmp_path / "workspace-b", shared / "data-b")
    target = config_b.logs_dir / "foreign-locator-storage"
    target.mkdir()
    locator_root = workspace_a / RESET_MANIFEST_DIRNAME
    simulated_link = False
    try:
        locator_root.symlink_to(target, target_is_directory=True)
    except OSError:
        locator_root.mkdir()
        simulated_link = True
        from jarvis.persistence import reset_recovery as recovery_module

        real_is_link_like = recovery_module._is_link_like
        monkeypatch.setattr(
            recovery_module,
            "_is_link_like",
            lambda path: path == locator_root or real_is_link_like(path),
        )
    locator = locator_root / f"{case_a.reset_id}{RESET_LOCATOR_SUFFIX}"
    locator_payload = manifest_locator_payload(
        reset_id=case_a.reset_id,
        manifest=case_a.manifest,
        config_root=case_a.config.root.resolve(),
        data_root=case_a.config.data_dir.resolve(),
        manifest_payload=case_a.payload,
    )
    reset_module._write_manifest(locator, locator_payload)
    durable_rename_no_replace(case_a.move.source, case_a.move.quarantine)
    manifest_before = case_a.manifest.read_bytes()
    locator_before = locator.read_bytes()
    quarantine_before = {
        path.relative_to(case_a.move.quarantine).as_posix(): path.read_bytes()
        for path in case_a.move.quarantine.rglob("*")
        if path.is_file()
    }
    assert target.is_relative_to(config_b.logs_dir)
    if not simulated_link:
        assert locator_root.resolve().is_relative_to(config_b.logs_dir.resolve())

    diagnostic, recovered, error = _startup_outcome(config_b)

    assert diagnostic is not None and "different workspace location" in diagnostic
    assert recovered is None
    assert error is not None and "different workspace location" in str(error)
    assert case_a.manifest.read_bytes() == manifest_before
    assert locator.read_bytes() == locator_before
    assert {
        path.relative_to(case_a.move.quarantine).as_posix(): path.read_bytes()
        for path in case_a.move.quarantine.rglob("*")
        if path.is_file()
    } == quarantine_before


async def test_locator_and_direct_discovery_coalesce_the_same_manifest(tmp_path: Path) -> None:
    case = await _pending_case(tmp_path, separate_manifest_anchor=True)
    _publish_locator(case)

    with ResetBarrier(case.config.data_dir) as barrier:
        pending = find_pending_reset(case.config, barrier)

    assert pending is not None
    assert pending.path == case.manifest
    assert pending.reset_id == case.reset_id


@pytest.mark.parametrize("status", ["completed", "rolled_back"])
async def test_locked_startup_retires_terminal_locator_before_workspace_move(
    tmp_path: Path,
    status: str,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    case = await _pending_case(workspace, separate_manifest_anchor=True)
    locator = _publish_locator(case)
    locator_payload = json.loads(locator.read_text(encoding="utf-8"))
    terminal = _terminal_payload(case, status)
    reset_module._write_manifest(case.manifest, terminal)

    assert _recover(case) is False

    retired = _retired_locator(locator)
    assert not locator.exists()
    assert json.loads(retired.read_text(encoding="utf-8")) == locator_payload
    relocated = tmp_path / "workspace-relocated"
    workspace.rename(relocated)
    case.config.root = relocated
    assert interrupted_reset_diagnostic(case.config) is None


async def test_terminal_locator_reconciles_after_workspace_and_data_parent_move(
    tmp_path: Path,
) -> None:
    old_workspace = tmp_path / "workspace-old"
    old_workspace.mkdir()
    old_state = tmp_path / "state-old"
    case = await _pending_case(old_workspace, external_data_parent=old_state)
    locator = _publish_locator(case)
    locator_payload = json.loads(locator.read_text(encoding="utf-8"))
    terminal = _terminal_payload(case, "completed")
    reset_module._write_manifest(case.manifest, terminal)

    new_workspace = tmp_path / "workspace-new"
    new_state = tmp_path / "state-new"
    old_workspace.rename(new_workspace)
    old_state.rename(new_state)
    case.config.root = new_workspace
    case.config.paths.data_dir = new_state / "data"
    case.config.paths.logs_dir = new_state / "data" / "logs"
    case.config.knowledge.dir = new_state / "data" / "knowledge"
    moved_manifest = new_state / RESET_MANIFEST_DIRNAME / f"{case.reset_id}.json"
    moved_locator = (
        new_workspace / RESET_MANIFEST_DIRNAME / f"{case.reset_id}{RESET_LOCATOR_SUFFIX}"
    )
    assert moved_manifest.is_file() and moved_locator.is_file()

    assert _recover(case) is False

    assert moved_manifest.is_file()
    assert not moved_locator.exists()
    assert json.loads(_retired_locator(moved_locator).read_text(encoding="utf-8")) == (
        locator_payload
    )
    assert interrupted_reset_diagnostic(case.config) is None


async def test_relocated_forged_terminal_cannot_satisfy_stale_locator(
    tmp_path: Path,
) -> None:
    old_workspace = tmp_path / "workspace-old"
    old_workspace.mkdir()
    old_state = tmp_path / "state-old"
    case = await _pending_case(old_workspace, external_data_parent=old_state)
    locator = _publish_locator(case)
    locator_payload = json.loads(locator.read_text(encoding="utf-8"))
    forged = {
        **_terminal_payload(case, "completed"),
        "old_counts": {"projects": 1},
    }
    forged_binding = manifest_locator_payload(
        reset_id=case.reset_id,
        manifest=case.manifest,
        config_root=case.config.root.resolve(),
        data_root=case.config.data_dir.resolve(),
        manifest_payload=forged,
    )
    assert forged_binding["manifest_digest"] != locator_payload["manifest_digest"]
    reset_module._write_manifest(case.manifest, forged)

    new_workspace = tmp_path / "workspace-new"
    new_state = tmp_path / "state-new"
    old_workspace.rename(new_workspace)
    old_state.rename(new_state)
    case.config.root = new_workspace
    case.config.paths.data_dir = new_state / "data"
    case.config.paths.logs_dir = new_state / "data" / "logs"
    case.config.knowledge.dir = new_state / "data" / "knowledge"
    moved_manifest = new_state / RESET_MANIFEST_DIRNAME / f"{case.reset_id}.json"
    moved_locator = (
        new_workspace / RESET_MANIFEST_DIRNAME / f"{case.reset_id}{RESET_LOCATOR_SUFFIX}"
    )

    diagnostic = interrupted_reset_diagnostic(case.config)

    assert diagnostic is not None and diagnostic.startswith("blocked (")
    assert "Reset manifest is unavailable" in diagnostic
    with pytest.raises(ResetRecoveryError, match="Reset manifest is unavailable"):
        _recover(case)
    assert moved_manifest.is_file() and moved_locator.is_file()
    assert not _retired_locator(moved_locator).exists()


@pytest.mark.parametrize(
    ("fault", "expected"),
    [
        ("malformed", "unreadable"),
        ("missing_target", "unavailable"),
        ("wrong_id", "ID does not match its filename"),
        ("wrong_target", "invalid target"),
    ],
)
async def test_invalid_manifest_locators_fail_closed(
    tmp_path: Path,
    fault: str,
    expected: str,
) -> None:
    case = await _pending_case(tmp_path, separate_manifest_anchor=True)
    locator = _publish_locator(case)
    if fault == "malformed":
        locator.write_text("{not-json\n", encoding="utf-8")
    elif fault == "missing_target":
        case.manifest.unlink()
    elif fault == "wrong_id":
        reset_module._write_manifest(
            locator,
            manifest_locator_payload(
                reset_id="20260715T120001Z-feedface",
                manifest=case.manifest,
                config_root=case.config.root.resolve(),
                data_root=case.config.data_dir.resolve(),
                manifest_payload=case.payload,
            ),
        )
    else:
        reset_module._write_manifest(
            locator,
            manifest_locator_payload(
                reset_id=case.reset_id,
                manifest=case.manifest.with_name("wrong-target.json"),
                config_root=case.config.root.resolve(),
                data_root=case.config.data_dir.resolve(),
                manifest_payload=case.payload,
            ),
        )

    diagnostic = interrupted_reset_diagnostic(case.config)

    assert diagnostic is not None and diagnostic.startswith("blocked (")
    assert expected in diagnostic
    assert (case.move.source / "old-sentinel.txt").is_file()


@pytest.mark.parametrize(
    ("record", "version", "expected"),
    [
        ("locator", True, "unsupported format"),
        ("locator", 1.0, "unsupported format"),
        ("manifest", True, "legacy reset cannot be recovered"),
        ("manifest", 2.0, "legacy reset cannot be recovered"),
    ],
)
async def test_boolean_and_float_reset_format_versions_fail_closed(
    tmp_path: Path,
    record: str,
    version: object,
    expected: str,
) -> None:
    case = await _pending_case(tmp_path, separate_manifest_anchor=True)
    if record == "locator":
        path = _publish_locator(case)
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["locator_format_version"] = version
    else:
        path = case.manifest
        payload = {**case.payload, "format_version": version}
    reset_module._write_manifest(path, payload)

    diagnostic = interrupted_reset_diagnostic(case.config)

    assert diagnostic is not None and diagnostic.startswith("blocked (")
    assert expected in diagnostic
    assert (case.move.source / "old-sentinel.txt").is_file()


async def test_conflicting_manifest_locators_fail_closed(tmp_path: Path) -> None:
    case = await _pending_case(tmp_path, separate_manifest_anchor=True)
    _publish_locator(case)
    conflicting_config_root = case.manifest.parent.parent
    conflicting_data = conflicting_config_root / "other-runtime" / "data"
    conflicting_manifest = (
        conflicting_data.parent / RESET_MANIFEST_DIRNAME / f"{case.reset_id}.json"
    )
    conflicting_payload = {
        **case.payload,
        "config_root": str(conflicting_config_root),
        "roots": [
            {
                **record,
                "source": str(conflicting_data),
                "quarantine": str(quarantine_paths(conflicting_data, case.reset_id)[0]),
            }
            for record in case.payload["roots"]
        ],
    }
    reset_module._write_manifest(conflicting_manifest, conflicting_payload)
    reset_module._write_manifest(
        case.manifest.with_name(f"{case.reset_id}{RESET_LOCATOR_SUFFIX}"),
        manifest_locator_payload(
            reset_id=case.reset_id,
            manifest=conflicting_manifest,
            config_root=conflicting_config_root,
            data_root=conflicting_data,
            manifest_payload=conflicting_payload,
        ),
    )

    diagnostic = interrupted_reset_diagnostic(case.config)

    assert diagnostic is not None and diagnostic.startswith("blocked (")
    assert "Conflicting reset manifest locators" in diagnostic
    assert (case.move.source / "old-sentinel.txt").is_file()


async def test_recovery_finds_manifest_when_missing_data_leaf_is_retargeted(
    tmp_path: Path,
) -> None:
    case = await _pending_case(tmp_path)
    durable_rename_no_replace(case.move.source, case.move.quarantine)
    other = tmp_path / "other-data"
    other.mkdir()
    sentinel = other / "unrelated.txt"
    sentinel.write_text("do not touch", encoding="utf-8")
    try:
        case.move.source.symlink_to(other, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory symlinks unavailable: {exc}")

    with pytest.raises(ResetRecoveryError, match="no longer matches configuration"):
        _recover(case)

    assert sentinel.read_text(encoding="utf-8") == "do not touch"
    assert not (other / "kira.db").exists()
    assert (case.move.quarantine / "old-sentinel.txt").is_file()
    assert json.loads(case.manifest.read_text(encoding="utf-8"))["status"] == "in_progress"


async def test_config_anchor_finds_manifest_after_linked_data_ancestor_is_retargeted(
    tmp_path: Path,
) -> None:
    case = await _pending_case(tmp_path)
    alias = tmp_path / "data-alias"
    try:
        alias.symlink_to(tmp_path, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory symlinks unavailable: {exc}")
    case.config.paths.data_dir = alias / "data"
    case.config.paths.logs_dir = alias / "data" / "logs"
    case.config.knowledge.dir = alias / "data" / "knowledge"
    durable_rename_no_replace(case.move.source, case.move.quarantine)

    other = tmp_path / "other-parent"
    target = other / "data"
    target.mkdir(parents=True)
    sentinel = target / "unrelated.txt"
    sentinel.write_text("do not touch", encoding="utf-8")
    alias.unlink()
    alias.symlink_to(other, target_is_directory=True)

    with pytest.raises(ResetRecoveryError, match="no longer matches configuration"):
        _recover(case)

    assert sentinel.read_text(encoding="utf-8") == "do not touch"
    assert not (target / "kira.db").exists()
    assert (case.move.quarantine / "old-sentinel.txt").is_file()
    assert json.loads(case.manifest.read_text(encoding="utf-8"))["status"] == "in_progress"


async def test_recovery_restores_quarantine_and_is_idempotent(tmp_path: Path) -> None:
    case = await _pending_case(tmp_path)
    durable_rename_no_replace(case.move.source, case.move.quarantine)

    assert _recover(case) is True
    assert (case.move.source / "old-sentinel.txt").read_text(encoding="utf-8") == "old data"
    assert not case.move.quarantine.exists()
    assert json.loads(case.manifest.read_text(encoding="utf-8"))["status"] == "rolled_back"
    assert _recover(case) is False


async def test_identity_bound_legacy_namespace_and_quarantine_are_recovered(
    tmp_path: Path,
) -> None:
    case = await _pending_case(tmp_path)
    legacy_quarantine = quarantine_paths(case.move.source, case.reset_id)[1]
    legacy_manifest = tmp_path / ".kairo-reset-manifests" / case.manifest.name
    payload = dict(case.payload)
    payload["roots"] = [dict(case.payload["roots"][0], quarantine=str(legacy_quarantine))]
    case.manifest.unlink()
    reset_module._write_manifest(legacy_manifest, payload)
    durable_rename_no_replace(case.move.source, legacy_quarantine)

    case.manifest = legacy_manifest
    case.payload = payload
    case.move = reset_module._RootMove(
        case.move.roles,
        case.move.source,
        legacy_quarantine,
        case.move.source_identity,
    )
    assert _recover(case) is True
    assert (case.move.source / "old-sentinel.txt").is_file()
    assert not legacy_quarantine.exists()


async def test_recovery_parks_partial_fresh_state_before_restoring_old_data(
    tmp_path: Path,
) -> None:
    case = await _pending_case(tmp_path)
    durable_rename_no_replace(case.move.source, case.move.quarantine)
    case.move.source.mkdir()
    (case.move.source / "fresh-sentinel.txt").write_text("fresh data", encoding="utf-8")

    assert _recover(case) is True
    assert (case.move.source / "old-sentinel.txt").read_text(encoding="utf-8") == "old data"
    assert (_failed_fresh(case) / "fresh-sentinel.txt").read_text(encoding="utf-8") == (
        "fresh data"
    )


async def test_recovery_resumes_after_crash_between_fresh_park_and_restore(
    tmp_path: Path,
) -> None:
    case = await _pending_case(tmp_path)
    durable_rename_no_replace(case.move.source, case.move.quarantine)
    case.move.source.mkdir()
    (case.move.source / "fresh-sentinel.txt").write_text("fresh data", encoding="utf-8")
    durable_rename_no_replace(case.move.source, _failed_fresh(case))

    assert _recover(case) is True
    assert (case.move.source / "old-sentinel.txt").is_file()
    assert (_failed_fresh(case) / "fresh-sentinel.txt").is_file()


async def test_manifest_published_before_any_move_rolls_back_without_moving_source(
    tmp_path: Path,
) -> None:
    case = await _pending_case(tmp_path)
    before = (case.move.source / "old-sentinel.txt").read_bytes()

    assert _recover(case) is True
    assert (case.move.source / "old-sentinel.txt").read_bytes() == before
    assert not case.move.quarantine.exists() and not _failed_fresh(case).exists()


async def test_recovery_retries_after_rename_failure_without_losing_either_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = await _pending_case(tmp_path)
    durable_rename_no_replace(case.move.source, case.move.quarantine)
    case.move.source.mkdir()
    (case.move.source / "fresh-sentinel.txt").write_text("fresh data", encoding="utf-8")
    from jarvis.persistence import reset_recovery as recovery_module

    real_rename = recovery_module.durable_rename_no_replace
    calls = 0

    def fail_second(source: Path, destination: Path) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected restore failure")
        real_rename(source, destination)

    monkeypatch.setattr(recovery_module, "durable_rename_no_replace", fail_second)
    with pytest.raises(ResetRecoveryError, match="all recoverable data remains preserved"):
        _recover(case)
    assert (case.move.quarantine / "old-sentinel.txt").is_file()
    assert (_failed_fresh(case) / "fresh-sentinel.txt").is_file()
    assert not case.move.source.exists()

    monkeypatch.setattr(recovery_module, "durable_rename_no_replace", real_rename)
    assert _recover(case) is True
    assert (case.move.source / "old-sentinel.txt").is_file()
    assert (_failed_fresh(case) / "fresh-sentinel.txt").is_file()


async def test_manifest_write_failure_after_restore_only_requires_status_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = await _pending_case(tmp_path)
    durable_rename_no_replace(case.move.source, case.move.quarantine)
    from jarvis.persistence import reset_recovery as recovery_module

    real_write = recovery_module.write_manifest

    def fail_write(_path: Path, _payload: dict) -> None:
        raise OSError("injected status write failure")

    monkeypatch.setattr(recovery_module, "write_manifest", fail_write)
    with pytest.raises(ResetRecoveryError, match="all recoverable data remains preserved"):
        _recover(case)
    assert (case.move.source / "old-sentinel.txt").is_file()
    assert not case.move.quarantine.exists()
    assert json.loads(case.manifest.read_text(encoding="utf-8"))["status"] == "in_progress"

    monkeypatch.setattr(recovery_module, "write_manifest", real_write)
    assert _recover(case) is True
    assert json.loads(case.manifest.read_text(encoding="utf-8"))["status"] == "rolled_back"


async def test_visible_rolled_back_manifest_counts_as_success_after_publish_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = await _pending_case(tmp_path)
    durable_rename_no_replace(case.move.source, case.move.quarantine)
    from jarvis.persistence import reset_recovery as recovery_module

    real_write = recovery_module.write_manifest

    def publish_then_report_error(path: Path, payload: dict) -> None:
        real_write(path, payload)
        raise OSError("injected post-publication durability error")

    monkeypatch.setattr(recovery_module, "write_manifest", publish_then_report_error)
    assert _recover(case) is True
    assert (case.move.source / "old-sentinel.txt").is_file()
    assert not case.move.quarantine.exists()
    assert json.loads(case.manifest.read_text(encoding="utf-8"))["status"] == "rolled_back"


async def test_originally_absent_root_is_archived_instead_of_deleted(tmp_path: Path) -> None:
    case = await _pending_case(tmp_path, absent_logs=True)
    durable_rename_no_replace(case.move.source, case.move.quarantine)
    case.move.source.mkdir()
    (case.move.source / "fresh-data.txt").write_text("fresh", encoding="utf-8")
    case.config.logs_dir.mkdir()
    (case.config.logs_dir / "fresh-log.txt").write_text("fresh log", encoding="utf-8")

    assert _recover(case) is True
    failed_logs = case.config.logs_dir.with_name(
        f".{case.config.logs_dir.name}.{FAILED_FRESH_LABEL}-{case.reset_id}"
    )
    assert not case.config.logs_dir.exists()
    assert (failed_logs / "fresh-log.txt").read_text(encoding="utf-8") == "fresh log"
    assert (_failed_fresh(case) / "fresh-data.txt").read_text(encoding="utf-8") == "fresh"
    assert (case.move.source / "old-sentinel.txt").is_file()


@pytest.mark.parametrize("state", ["missing", "fresh_only", "all_three", "wrong_source"])
async def test_ambiguous_states_fail_closed_without_overwriting_any_tree(
    tmp_path: Path,
    state: str,
) -> None:
    case = await _pending_case(tmp_path)
    lost = case.move.source.with_name("preserved-old-outside-recovery")
    if state in {"missing", "fresh_only", "all_three"}:
        durable_rename_no_replace(case.move.source, case.move.quarantine)
    if state in {"missing", "fresh_only"}:
        durable_rename_no_replace(case.move.quarantine, lost)
    if state == "fresh_only":
        _failed_fresh(case).mkdir()
        (_failed_fresh(case) / "fresh.txt").write_text("fresh", encoding="utf-8")
    elif state == "all_three":
        case.move.source.mkdir()
        (case.move.source / "fresh.txt").write_text("source", encoding="utf-8")
        _failed_fresh(case).mkdir()
        (_failed_fresh(case) / "fresh.txt").write_text("archive", encoding="utf-8")
    elif state == "wrong_source":
        durable_rename_no_replace(case.move.source, lost)
        case.move.source.mkdir()
        (case.move.source / "fresh.txt").write_text("fresh", encoding="utf-8")

    with pytest.raises(ResetRecoveryError):
        _recover(case)

    if lost.exists():
        assert (lost / "old-sentinel.txt").is_file()
    if case.move.quarantine.exists():
        assert (case.move.quarantine / "old-sentinel.txt").is_file()
    if case.move.source.exists():
        assert (case.move.source / "fresh.txt").is_file()
    assert json.loads(case.manifest.read_text(encoding="utf-8"))["status"] == "in_progress"


async def test_quarantine_identity_swap_fails_closed(tmp_path: Path) -> None:
    case = await _pending_case(tmp_path)
    original = case.move.quarantine.with_name("preserved-original-quarantine")
    durable_rename_no_replace(case.move.source, case.move.quarantine)
    durable_rename_no_replace(case.move.quarantine, original)
    case.move.quarantine.mkdir()
    (case.move.quarantine / "attacker.txt").write_text("do not move", encoding="utf-8")

    with pytest.raises(ResetRecoveryError, match="identity"):
        _recover(case)
    assert (original / "old-sentinel.txt").is_file()
    assert (case.move.quarantine / "attacker.txt").is_file()
    assert not case.move.source.exists()


async def test_legacy_unbound_manifest_blocks_automatic_recovery(tmp_path: Path) -> None:
    case = await _pending_case(tmp_path)
    payload = dict(case.payload)
    payload.pop("format_version")
    payload.pop("config_root")
    payload.pop("absent_roots")
    reset_module._write_manifest(case.manifest, payload)

    with pytest.raises(ResetRecoveryError):
        _recover(case)
    assert (case.move.source / "old-sentinel.txt").is_file()


async def test_multiple_in_progress_manifests_across_namespaces_fail_closed(
    tmp_path: Path,
) -> None:
    case = await _pending_case(tmp_path)
    duplicate = tmp_path / ".kairo-reset-manifests" / case.manifest.name
    reset_module._write_manifest(duplicate, case.payload)

    with pytest.raises(ResetRecoveryError, match="Multiple interrupted resets"):
        _recover(case)
    assert (case.move.source / "old-sentinel.txt").is_file()


async def test_manifest_enumeration_error_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = await _pending_case(tmp_path)
    real_iterdir = Path.iterdir

    def refuse_manifest_scan(path: Path):
        if path == case.manifest.parent:
            raise PermissionError("injected ACL denial")
        return real_iterdir(path)

    monkeypatch.setattr(Path, "iterdir", refuse_manifest_scan)
    with pytest.raises(ResetRecoveryError, match="cannot be inspected"):
        _recover(case)
    assert (case.move.source / "old-sentinel.txt").is_file()
    assert not case.move.quarantine.exists()


async def test_deeply_nested_manifest_fails_closed_without_raw_recursion_error(
    tmp_path: Path,
) -> None:
    case = await _pending_case(tmp_path)
    case.manifest.write_text(
        '{"status":' + "[" * 5000 + "0" + "]" * 5000 + "}",
        encoding="utf-8",
    )

    with pytest.raises(ResetRecoveryError, match="manifest is unreadable"):
        _recover(case)

    assert (case.move.source / "old-sentinel.txt").is_file()
    assert not case.move.quarantine.exists()


async def test_startup_recovers_before_reporting_missing_provider_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import jarvis.__main__ as entry
    import jarvis.cli.repl as repl_module
    import jarvis.config as config_module

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    case = await _pending_case(tmp_path)
    durable_rename_no_replace(case.move.source, case.move.quarantine)
    monkeypatch.setattr(config_module, "load_config", lambda **_kwargs: case.config)
    monkeypatch.setattr(
        repl_module,
        "run_repl",
        lambda *_args, **_kwargs: pytest.fail("runtime must not start without its key"),
    )
    monkeypatch.setattr(sys, "argv", ["kira"])

    with pytest.raises(SystemExit) as exited:
        entry.main()

    assert exited.value.code == 1
    assert (case.move.source / "old-sentinel.txt").is_file()
    assert not case.move.quarantine.exists()
    assert json.loads(case.manifest.read_text(encoding="utf-8"))["status"] == "rolled_back"
    assert "Missing required API key" in capsys.readouterr().out
