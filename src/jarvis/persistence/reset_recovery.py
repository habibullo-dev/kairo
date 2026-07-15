"""Identity-bound, lossless recovery for interrupted whole-instance resets."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import stat
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jarvis.config import Config
from jarvis.persistence.durable_fs import (
    durable_mkdir,
    durable_rename_no_replace,
    durable_replace,
    sync_directory,
)
from jarvis.persistence.instance_lock import InstanceLock, ResetBarrier

RESET_FORMAT_VERSION = 2
RESET_MANIFEST_DIRNAME = ".kira-reset-manifests"
LEGACY_RESET_MANIFEST_DIRNAMES = (".kairo-reset-manifests",)
RESET_LOCATOR_FORMAT_VERSION = 1
RESET_LOCATOR_SUFFIX = ".locator"
RESET_RETIRED_LOCATOR_SUFFIX = ".locator-retired"
QUARANTINE_LABEL = "kira-quarantine"
LEGACY_QUARANTINE_LABELS = ("kairo-quarantine",)
FAILED_FRESH_LABEL = "kira-reset-failed-fresh"

_RESET_ID = re.compile(r"\A\d{8}T\d{6}Z-[0-9a-f]{8}\Z")
_MANIFEST_DIGEST = re.compile(r"\A[0-9a-f]{64}\Z")
_MAX_MANIFEST_BYTES = 1024 * 1024
_ROLES = frozenset({"data", "logs", "knowledge"})
_LOCATOR_FIELDS = frozenset(
    {
        "locator_format_version",
        "reset_id",
        "manifest",
        "manifest_digest",
        "config_root",
        "data_root",
    }
)
_IN_PROGRESS_FIELDS = frozenset(
    {
        "format_version",
        "reset_id",
        "created_at",
        "status",
        "config_root",
        "old_schema_version",
        "old_counts",
        "roots",
        "absent_roots",
        "preserved",
        "locked_integrations",
    }
)
_COMPLETED_FIELDS = _IN_PROGRESS_FIELDS | frozenset(
    {"completed_at", "fresh_schema_version", "integrity_check"}
)
_ROLLED_BACK_FIELDS = _IN_PROGRESS_FIELDS | frozenset({"rolled_back_at", "error_type"})


class ResetRecoveryError(RuntimeError):
    """An interrupted reset is unsafe or ambiguous and requires operator attention."""


class _ForeignResetManifest(ResetRecoveryError):
    """A valid reset record is proven to belong to a disjoint sibling instance."""


@dataclass(frozen=True)
class DirectoryIdentity:
    device: int
    inode: int

    def payload(self) -> dict[str, int]:
        return {"device": self.device, "inode": self.inode}


@dataclass(frozen=True)
class RecoveryRoot:
    roles: tuple[str, ...]
    source: Path
    quarantine: Path
    expected: DirectoryIdentity

    def failed_fresh(self, reset_id: str) -> Path:
        return self.source.with_name(f".{self.source.name}.{FAILED_FRESH_LABEL}-{reset_id}")


@dataclass(frozen=True)
class AbsentRoot:
    roles: tuple[str, ...]
    source: Path

    def failed_fresh(self, reset_id: str) -> Path:
        return self.source.with_name(f".{self.source.name}.{FAILED_FRESH_LABEL}-{reset_id}")


@dataclass(frozen=True)
class PendingReset:
    path: Path
    reset_id: str
    payload: dict[str, Any]
    roots: tuple[RecoveryRoot, ...]
    absent_roots: tuple[AbsentRoot, ...]
    locators: tuple[Path, ...] = ()


def _is_link_like(path: Path) -> bool:
    junction = getattr(path, "is_junction", None)
    return path.is_symlink() or bool(junction and junction())


def _path_present(path: Path) -> bool:
    return os.path.lexists(path)


def _absolute(path: Path) -> Path:
    return Path(os.path.abspath(path))


def canonical_local_path(
    path: Path,
    *,
    label: str,
    must_exist: bool = False,
    reject_final_link: bool = False,
) -> Path:
    """Resolve one physical path, optionally refusing a linked final entry."""
    absolute = _absolute(path)
    if reject_final_link and _path_present(absolute) and _is_link_like(absolute):
        raise ResetRecoveryError(f"{label} is linked or junction-backed: {absolute}")
    try:
        return absolute.resolve(strict=must_exist)
    except OSError as exc:
        raise ResetRecoveryError(f"{label} is unavailable: {absolute}") from exc


def manifest_roots(data_dir: Path, *, config_root: Path | None = None) -> tuple[Path, ...]:
    """Return config-anchored storage plus legacy-compatible data-sibling locations."""
    absolute = _absolute(data_dir)
    try:
        parents = []
        if config_root is not None:
            parents.append(_absolute(config_root).resolve())
        parents.extend((absolute.resolve().parent, absolute.parent.resolve()))
    except OSError as exc:
        raise ResetRecoveryError(f"Configured data root is unavailable: {absolute}") from exc
    unique_parents = list(dict.fromkeys(parents))
    return tuple(
        root
        for parent in unique_parents
        for root in (
            parent / RESET_MANIFEST_DIRNAME,
            *(parent / name for name in LEGACY_RESET_MANIFEST_DIRNAMES),
        )
    )


def _manifest_identity_digest(payload: dict[str, Any]) -> str:
    try:
        identity = {field: payload[field] for field in _IN_PROGRESS_FIELDS}
        identity["status"] = "in_progress"
        encoded = json.dumps(
            identity,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (KeyError, TypeError, ValueError, RecursionError) as exc:
        raise ResetRecoveryError("Reset manifest identity cannot be fingerprinted") from exc
    return hashlib.sha256(encoded).hexdigest()


def manifest_locator_payload(
    *,
    reset_id: str,
    manifest: Path,
    config_root: Path,
    data_root: Path,
    manifest_payload: dict[str, Any],
) -> dict[str, Any]:
    """Build the immutable record that locates a data-anchored reset manifest."""
    return {
        "locator_format_version": RESET_LOCATOR_FORMAT_VERSION,
        "reset_id": reset_id,
        "manifest": str(manifest),
        "manifest_digest": _manifest_identity_digest(manifest_payload),
        "config_root": str(config_root),
        "data_root": str(data_root),
    }


def retire_manifest_locator(path: Path, expected: dict[str, Any]) -> Path:
    """Durably take a terminal locator out of the active discovery namespace."""
    if not path.name.endswith(RESET_LOCATOR_SUFFIX):
        raise ResetRecoveryError(f"Reset manifest locator has an invalid filename: {path}")
    retired = path.with_name(
        f"{path.name.removesuffix(RESET_LOCATOR_SUFFIX)}{RESET_RETIRED_LOCATOR_SUFFIX}"
    )
    source_present = _path_present(path)
    retired_present = _path_present(retired)
    if source_present and retired_present:
        raise ResetRecoveryError("Active and retired reset manifest locators both exist")
    if source_present:
        if not manifest_matches(path, expected):
            raise ResetRecoveryError("Reset manifest locator changed before retirement")
        try:
            durable_rename_no_replace(path, retired)
        except OSError as exc:
            raise ResetRecoveryError("Reset manifest locator could not be retired") from exc
    elif not retired_present:
        raise ResetRecoveryError("Reset manifest locator disappeared before retirement")
    if not manifest_matches(retired, expected):
        raise ResetRecoveryError("Retired reset manifest locator could not be verified")
    return retired


def quarantine_paths(source: Path, reset_id: str) -> tuple[Path, ...]:
    return (
        source.with_name(f".{source.name}.{QUARANTINE_LABEL}-{reset_id}"),
        *(
            source.with_name(f".{source.name}.{label}-{reset_id}")
            for label in LEGACY_QUARANTINE_LABELS
        ),
    )


def directory_identity(path: Path, *, label: str) -> DirectoryIdentity:
    identity = _optional_directory_identity(path, label=label)
    if identity is None:
        raise ResetRecoveryError(f"{label} disappeared: {path}")
    return identity


def _optional_directory_identity(path: Path, *, label: str) -> DirectoryIdentity | None:
    try:
        info = path.stat(follow_symlinks=False)
    except FileNotFoundError:
        if _path_present(path):
            raise ResetRecoveryError(f"{label} is an unreadable filesystem entry: {path}") from None
        return None
    except OSError as exc:
        raise ResetRecoveryError(f"{label} is unavailable: {path}") from exc
    if _is_link_like(path) or not stat.S_ISDIR(info.st_mode):
        raise ResetRecoveryError(f"{label} is not a regular local directory: {path}")
    return DirectoryIdentity(device=int(info.st_dev), inode=int(info.st_ino))


def _identity_from_payload(value: object) -> DirectoryIdentity:
    if not isinstance(value, dict) or set(value) != {"device", "inode"}:
        raise ResetRecoveryError("Interrupted reset has an invalid root identity")
    device = value.get("device")
    inode = value.get("inode")
    if (
        isinstance(device, bool)
        or isinstance(inode, bool)
        or not isinstance(device, int)
        or not isinstance(inode, int)
        or device < 0
        or inode < 0
    ):
        raise ResetRecoveryError("Interrupted reset has an invalid root identity")
    return DirectoryIdentity(device=device, inode=inode)


def _json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ResetRecoveryError(f"Interrupted reset manifest repeats field: {key}")
        result[key] = value
    return result


def _read_manifest(path: Path) -> dict[str, Any]:
    try:
        info = path.stat(follow_symlinks=False)
    except OSError as exc:
        raise ResetRecoveryError(f"Reset manifest is unavailable: {path}") from exc
    if (
        _is_link_like(path)
        or not stat.S_ISREG(info.st_mode)
        or int(info.st_nlink) != 1
        or int(info.st_size) > _MAX_MANIFEST_BYTES
    ):
        raise ResetRecoveryError(f"Reset manifest is not a safe local file: {path}")

    descriptor: int | None = None
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        opened = os.fstat(descriptor)
        if (
            int(opened.st_dev) != int(info.st_dev)
            or int(opened.st_ino) != int(info.st_ino)
            or int(opened.st_size) != int(info.st_size)
            or not stat.S_ISREG(opened.st_mode)
            or int(opened.st_nlink) != 1
        ):
            raise ResetRecoveryError(f"Reset manifest changed while opening: {path}")
        raw = b""
        remaining = int(opened.st_size) + 1
        while remaining > 0:
            chunk = os.read(descriptor, min(65536, remaining))
            if not chunk:
                break
            raw += chunk
            remaining -= len(chunk)
        if len(raw) != int(opened.st_size):
            raise ResetRecoveryError(f"Reset manifest changed while reading: {path}")
        parsed = json.loads(raw.decode("utf-8"), object_pairs_hook=_json_object)
    except ResetRecoveryError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ResetRecoveryError(f"Reset manifest is unreadable: {path}") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
    if not isinstance(parsed, dict):
        raise ResetRecoveryError(f"Reset manifest is not a JSON object: {path}")
    return parsed


def write_manifest(path: Path, payload: dict[str, Any]) -> None:
    """Atomically publish and durably record one reset manifest state."""
    parent_existed = path.parent.exists()
    if not parent_existed:
        durable_mkdir(path.parent)
    if not parent_existed:
        sync_directory(path.parent.parent)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        durable_replace(temporary_path, path)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise


def _validated_path(value: object, *, label: str) -> Path:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ResetRecoveryError(f"Interrupted reset has an invalid {label}")
    candidate = Path(value)
    if not candidate.is_absolute() or candidate != _absolute(candidate):
        raise ResetRecoveryError(f"Interrupted reset has a non-canonical {label}")
    return candidate


def manifest_matches(path: Path, expected: dict[str, Any]) -> bool:
    """Read back one safely-opened manifest and compare its exact logical payload."""
    return _read_manifest(path) == expected


def _validated_roles(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or not value or any(not isinstance(v, str) for v in value):
        raise ResetRecoveryError("Interrupted reset has invalid root roles")
    roles = tuple(value)
    if len(set(roles)) != len(roles) or not set(roles).issubset(_ROLES):
        raise ResetRecoveryError("Interrupted reset has invalid root roles")
    return roles


def _validate_safe_source(path: Path, *, config_root: Path) -> None:
    anchor = Path(path.anchor)
    home = Path.home().resolve()
    root = config_root.resolve()
    if (
        path in {anchor, home, root}
        or home.is_relative_to(path)
        or root.is_relative_to(path)
        or len(path.parts) < 2
    ):
        raise ResetRecoveryError(f"Interrupted reset references an unsafe root: {path}")


def _validate_timestamp(value: object) -> None:
    if not isinstance(value, str):
        raise ResetRecoveryError("Interrupted reset has an invalid creation time")
    try:
        parsed = dt.datetime.fromisoformat(value)
    except ValueError as exc:
        raise ResetRecoveryError("Interrupted reset has an invalid creation time") from exc
    if parsed.tzinfo is None:
        raise ResetRecoveryError("Interrupted reset has an invalid creation time")


def _paths_overlap(first: Path, second: Path) -> bool:
    return first == second or first.is_relative_to(second) or second.is_relative_to(first)


def _is_proven_foreign_sibling_reset(
    config: Config,
    path: Path,
    payload: dict[str, Any],
    *,
    reset_id: str,
    config_root: Path,
    current: dict[str, Path],
    roots: list[RecoveryRoot],
    absent_roots: list[AbsentRoot],
) -> bool:
    """Recognize only a fully validated, disjoint sibling-instance reset record."""
    current_config_root = config.root.resolve()
    data_root = next(root.source for root in roots if "data" in root.roles)
    current_data = current["data"]
    if config_root == current_config_root or data_root == current_data:
        return False
    if os.path.normcase(data_root.name) == os.path.normcase(current_data.name):
        return False
    if data_root.parent != current_data.parent or path.parent.parent != current_data.parent:
        return False
    if path.parent.name != RESET_MANIFEST_DIRNAME:
        return False
    if path != data_root.parent / RESET_MANIFEST_DIRNAME / f"{reset_id}.json":
        return False

    proof_paths = [path]
    if config_root != data_root.parent:
        locator = config_root / RESET_MANIFEST_DIRNAME / f"{reset_id}{RESET_LOCATOR_SUFFIX}"
        try:
            if (
                _optional_directory_identity(
                    locator.parent,
                    label="Foreign reset locator storage",
                )
                is None
            ):
                return False
            physical_locator_parent = canonical_local_path(
                locator.parent,
                label="Foreign reset locator storage",
                must_exist=True,
                reject_final_link=True,
            )
            located_path, located_payload, _locator_payload = _read_manifest_locator(
                locator, {path: payload}
            )
        except ResetRecoveryError:
            return False
        if located_path != path or located_payload != payload:
            return False
        proof_paths.extend((locator, physical_locator_parent / locator.name))
    elif path.parent.parent != config_root:
        return False

    recovery_paths = list(proof_paths)
    for root in roots:
        recovery_paths.extend((root.source, root.quarantine, root.failed_fresh(reset_id)))
    for root in absent_roots:
        recovery_paths.extend((root.source, root.failed_fresh(reset_id)))
    current_roots = tuple(current.values())
    if any(
        _paths_overlap(recovery_path, current_root)
        for recovery_path in recovery_paths
        for current_root in current_roots
    ):
        return False
    return not any(
        _paths_overlap(recovery_path, current_config_root) for recovery_path in recovery_paths
    )


def _validate_pending(
    config: Config,
    path: Path,
    payload: dict[str, Any],
    *,
    locators: tuple[Path, ...] = (),
) -> PendingReset:
    if set(payload) != _IN_PROGRESS_FIELDS:
        raise ResetRecoveryError("Interrupted reset manifest has unexpected fields")
    format_version = payload.get("format_version")
    if (
        isinstance(format_version, bool)
        or not isinstance(format_version, int)
        or format_version != RESET_FORMAT_VERSION
    ):
        raise ResetRecoveryError(
            "Interrupted legacy reset cannot be recovered automatically; operator recovery "
            "is required"
        )
    reset_id = payload.get("reset_id")
    if not isinstance(reset_id, str) or not _RESET_ID.fullmatch(reset_id):
        raise ResetRecoveryError("Interrupted reset has an invalid reset ID")
    if path.name != f"{reset_id}.json":
        raise ResetRecoveryError("Interrupted reset ID does not match its manifest filename")
    _validate_timestamp(payload.get("created_at"))
    if payload.get("status") != "in_progress":
        raise ResetRecoveryError("Interrupted reset has an invalid state")
    config_root = _validated_path(payload.get("config_root"), label="configuration root")
    version = payload.get("old_schema_version")
    counts = payload.get("old_counts")
    if isinstance(version, bool) or not isinstance(version, int) or version < 0:
        raise ResetRecoveryError("Interrupted reset has an invalid prior schema version")
    if not isinstance(counts, dict) or any(
        not isinstance(key, str)
        or isinstance(value, bool)
        or not isinstance(value, int)
        or value < 0
        for key, value in counts.items()
    ):
        raise ResetRecoveryError("Interrupted reset has invalid prior record counts")

    current = {
        "data": canonical_local_path(config.data_dir, label="Configured data root"),
        "logs": canonical_local_path(config.logs_dir, label="Configured logs root"),
        "knowledge": canonical_local_path(config.knowledge_dir, label="Configured knowledge root"),
    }
    raw_roots = payload.get("roots")
    raw_absent = payload.get("absent_roots")
    if not isinstance(raw_roots, list) or not raw_roots or not isinstance(raw_absent, list):
        raise ResetRecoveryError("Interrupted reset has an invalid root plan")

    if path.parent.name == RESET_MANIFEST_DIRNAME:
        namespace_index = 0
    else:
        try:
            namespace_index = LEGACY_RESET_MANIFEST_DIRNAMES.index(path.parent.name) + 1
        except ValueError as exc:
            raise ResetRecoveryError(
                "Interrupted reset uses an unknown manifest namespace"
            ) from exc
    roots: list[RecoveryRoot] = []
    absent_roots: list[AbsentRoot] = []
    seen_roles: set[str] = set()
    seen_sources: set[Path] = set()
    for item in raw_roots:
        if not isinstance(item, dict) or set(item) != {
            "roles",
            "source",
            "quarantine",
            "source_identity",
        }:
            raise ResetRecoveryError("Interrupted reset has an invalid moved-root record")
        roles = _validated_roles(item.get("roles"))
        source = _validated_path(item.get("source"), label="source root")
        quarantine = _validated_path(item.get("quarantine"), label="quarantine root")
        expected_quarantine = quarantine_paths(source, reset_id)[namespace_index]
        if quarantine != expected_quarantine:
            raise ResetRecoveryError("Interrupted reset has an invalid quarantine path")
        expected = _identity_from_payload(item.get("source_identity"))
        _validate_safe_source(source, config_root=config_root)
        if source in seen_sources or seen_roles.intersection(roles):
            raise ResetRecoveryError("Interrupted reset repeats a configured root")
        seen_sources.add(source)
        seen_roles.update(roles)
        roots.append(RecoveryRoot(roles, source, quarantine, expected))

    for item in raw_absent:
        if not isinstance(item, dict) or set(item) != {"roles", "source"}:
            raise ResetRecoveryError("Interrupted reset has an invalid absent-root record")
        roles = _validated_roles(item.get("roles"))
        source = _validated_path(item.get("source"), label="absent source root")
        _validate_safe_source(source, config_root=config_root)
        if source in seen_sources or seen_roles.intersection(roles):
            raise ResetRecoveryError("Interrupted reset repeats a configured root")
        seen_sources.add(source)
        seen_roles.update(roles)
        absent_roots.append(AbsentRoot(roles, source))

    if seen_roles != _ROLES or not any("data" in root.roles for root in roots):
        raise ResetRecoveryError("Interrupted reset does not cover the configured Kira roots")
    combined = [root.source for root in roots] + [root.source for root in absent_roots]
    if any(
        first != second and (first.is_relative_to(second) or second.is_relative_to(first))
        for index, first in enumerate(combined)
        for second in combined[index + 1 :]
    ):
        raise ResetRecoveryError("Interrupted reset has overlapping root records")
    if _is_proven_foreign_sibling_reset(
        config,
        path,
        payload,
        reset_id=reset_id,
        config_root=config_root,
        current=current,
        roots=roots,
        absent_roots=absent_roots,
    ):
        raise _ForeignResetManifest("Reset manifest belongs to a disjoint sibling instance")
    if config_root != config.root.resolve():
        raise ResetRecoveryError(
            "Interrupted reset belongs to a different workspace location; operator recovery "
            "is required"
        )
    for root in roots:
        if root.source not in {current[role] for role in root.roles}:
            raise ResetRecoveryError("Interrupted reset root no longer matches configuration")
        if any(
            not (current[role] == root.source or current[role].is_relative_to(root.source))
            for role in root.roles
        ):
            raise ResetRecoveryError("Interrupted reset root roles no longer match configuration")
    for root in absent_roots:
        if root.source not in {current[role] for role in root.roles}:
            raise ResetRecoveryError("Interrupted reset root no longer matches configuration")
        if any(
            not (current[role] == root.source or current[role].is_relative_to(root.source))
            for role in root.roles
        ):
            raise ResetRecoveryError("Interrupted reset absent roles no longer match configuration")
    return PendingReset(path, reset_id, payload, tuple(roots), tuple(absent_roots), locators)


def _validate_locator_binding(
    path: Path,
    *,
    reset_id: str,
    config_root: Path,
    data_root: Path,
    manifest_digest: str,
    payload: dict[str, Any],
) -> str:
    status = payload.get("status")
    if status not in {"in_progress", "completed", "rolled_back"}:
        raise ResetRecoveryError(f"Reset manifest locator target has an unknown state: {path}")
    format_version = payload.get("format_version")
    if (
        isinstance(format_version, bool)
        or not isinstance(format_version, int)
        or format_version != RESET_FORMAT_VERSION
        or payload.get("reset_id") != reset_id
        or _validated_path(payload.get("config_root"), label="manifest configuration root")
        != config_root
    ):
        raise ResetRecoveryError(f"Reset manifest locator does not match its target: {path}")
    if _manifest_identity_digest(payload) != manifest_digest:
        raise ResetRecoveryError(f"Reset manifest locator fingerprint does not match: {path}")
    raw_roots = payload.get("roots")
    if not isinstance(raw_roots, list):
        raise ResetRecoveryError(f"Reset manifest locator target has no root plan: {path}")
    data_sources: list[Path] = []
    for item in raw_roots:
        if not isinstance(item, dict):
            continue
        roles = item.get("roles")
        if isinstance(roles, list) and "data" in roles:
            data_sources.append(_validated_path(item.get("source"), label="manifest data root"))
    if data_sources != [data_root]:
        raise ResetRecoveryError(
            f"Reset manifest locator does not match the data root plan: {path}"
        )

    if status != "in_progress":
        expected_fields = _COMPLETED_FIELDS if status == "completed" else _ROLLED_BACK_FIELDS
        if set(payload) != expected_fields:
            raise ResetRecoveryError(f"Terminal reset manifest has unexpected fields: {path}")
        _validate_timestamp(payload.get("created_at"))
        if status == "completed":
            _validate_timestamp(payload.get("completed_at"))
            version = payload.get("fresh_schema_version")
            if (
                isinstance(version, bool)
                or not isinstance(version, int)
                or version < 0
                or payload.get("integrity_check") != "ok"
            ):
                raise ResetRecoveryError(f"Completed reset manifest is invalid: {path}")
        else:
            _validate_timestamp(payload.get("rolled_back_at"))
            if payload.get("error_type") != "InterruptedResetRecovery":
                raise ResetRecoveryError(f"Rolled-back reset manifest is invalid: {path}")
    return status


def _read_manifest_locator(
    path: Path,
    direct_records: dict[Path, dict[str, Any]],
) -> tuple[Path, dict[str, Any], dict[str, Any]]:
    locator = _read_manifest(path)
    if set(locator) != _LOCATOR_FIELDS:
        raise ResetRecoveryError(f"Reset manifest locator has unexpected fields: {path}")
    locator_version = locator.get("locator_format_version")
    if (
        isinstance(locator_version, bool)
        or not isinstance(locator_version, int)
        or locator_version != RESET_LOCATOR_FORMAT_VERSION
    ):
        raise ResetRecoveryError(f"Reset manifest locator has an unsupported format: {path}")

    reset_id = locator.get("reset_id")
    if not isinstance(reset_id, str) or not _RESET_ID.fullmatch(reset_id):
        raise ResetRecoveryError(f"Reset manifest locator has an invalid reset ID: {path}")
    if path.name != f"{reset_id}{RESET_LOCATOR_SUFFIX}":
        raise ResetRecoveryError(f"Reset manifest locator ID does not match its filename: {path}")

    manifest = _validated_path(locator.get("manifest"), label="located manifest path")
    manifest_digest = locator.get("manifest_digest")
    if not isinstance(manifest_digest, str) or not _MANIFEST_DIGEST.fullmatch(manifest_digest):
        raise ResetRecoveryError(f"Reset manifest locator has an invalid fingerprint: {path}")
    config_root = _validated_path(locator.get("config_root"), label="located configuration root")
    data_root = _validated_path(locator.get("data_root"), label="located data root")
    expected_manifest = data_root.parent / RESET_MANIFEST_DIRNAME / f"{reset_id}.json"
    if manifest != expected_manifest:
        raise ResetRecoveryError(f"Reset manifest locator has an invalid target: {path}")

    if _path_present(manifest):
        if _optional_directory_identity(manifest.parent, label="Located manifest storage") is None:
            raise ResetRecoveryError(f"Reset manifest locator target directory is missing: {path}")
        payload = _read_manifest(manifest)
        status = _validate_locator_binding(
            path,
            reset_id=reset_id,
            config_root=config_root,
            data_root=data_root,
            manifest_digest=manifest_digest,
            payload=payload,
        )
    else:
        matches: list[tuple[Path, dict[str, Any]]] = []
        for candidate, payload in direct_records.items():
            if candidate.name != f"{reset_id}.json":
                continue
            try:
                status = _validate_locator_binding(
                    path,
                    reset_id=reset_id,
                    config_root=config_root,
                    data_root=data_root,
                    manifest_digest=manifest_digest,
                    payload=payload,
                )
            except ResetRecoveryError:
                continue
            if status in {"completed", "rolled_back"}:
                matches.append((candidate, payload))
        if len(matches) != 1:
            if len(matches) > 1:
                raise ResetRecoveryError(
                    "A terminal reset manifest locator matches multiple records; operator "
                    "recovery is required"
                )
            raise ResetRecoveryError(f"Reset manifest is unavailable: {manifest}")
        manifest, payload = matches[0]
        status = str(payload["status"])

    if status == "in_progress":
        if path.parent.name != RESET_MANIFEST_DIRNAME or path.parent.parent != config_root:
            raise ResetRecoveryError(
                f"Reset manifest locator is outside its configuration root: {path}"
            )
        if (
            canonical_local_path(
                config_root,
                label="Located configuration root",
                must_exist=True,
            )
            != config_root
        ):
            raise ResetRecoveryError(
                f"Reset manifest locator configuration root is not canonical: {path}"
            )
        if canonical_local_path(data_root, label="Located data root") != data_root:
            raise ResetRecoveryError(f"Reset manifest locator data root is not canonical: {path}")
    return manifest, payload, locator


def _discover_reset_state_unlocked(
    config: Config,
) -> tuple[PendingReset | None, tuple[tuple[Path, dict[str, Any]], ...]]:
    records: dict[Path, dict[str, Any]] = {}
    locator_targets: dict[str, Path] = {}
    locator_paths: dict[Path, set[Path]] = {}
    locator_payloads: dict[Path, dict[str, Any]] = {}
    direct_paths: set[Path] = set()
    active_locator_paths: set[Path] = set()
    for root in manifest_roots(config.data_dir, config_root=config.root):
        if _optional_directory_identity(root, label="Reset manifest storage") is None:
            continue
        try:
            paths = sorted(root.iterdir())
        except OSError as exc:
            raise ResetRecoveryError("Reset manifest storage cannot be inspected") from exc
        for path in paths:
            if path.name.endswith(".json"):
                direct_paths.add(path)
            elif root.name == RESET_MANIFEST_DIRNAME and path.name.endswith(RESET_LOCATOR_SUFFIX):
                active_locator_paths.add(path)
            else:
                continue

    for path in sorted(direct_paths):
        records[path] = _read_manifest(path)
    for path in sorted(active_locator_paths):
        manifest, payload, locator = _read_manifest_locator(path, records)
        locator_paths.setdefault(manifest, set()).add(path)
        locator_payloads[path] = locator
        reset_id = path.name.removesuffix(RESET_LOCATOR_SUFFIX)
        previous_target = locator_targets.setdefault(reset_id, manifest)
        if previous_target != manifest:
            raise ResetRecoveryError(
                "Conflicting reset manifest locators were found; operator recovery is required"
            )
        previous_payload = records.setdefault(manifest, payload)
        if previous_payload != payload:
            raise ResetRecoveryError(
                "A reset manifest changed during discovery; operator recovery is required"
            )

    candidates: list[tuple[Path, dict[str, Any]]] = []
    reset_locations: dict[str, Path] = {}
    for path, payload in records.items():
        status = payload.get("status")
        if status not in {"in_progress", "completed", "rolled_back"}:
            raise ResetRecoveryError(f"Reset manifest has an unknown state: {path}")
        reset_id = payload.get("reset_id")
        if isinstance(reset_id, str):
            previous_path = reset_locations.setdefault(reset_id, path)
            if previous_path != path:
                raise ResetRecoveryError(
                    "Multiple interrupted resets were found; operator recovery is required"
                )
        if status == "in_progress":
            candidates.append((path, payload))
    validated_candidates: list[PendingReset] = []
    for path, payload in candidates:
        try:
            validated_candidates.append(
                _validate_pending(
                    config,
                    path,
                    payload,
                    locators=tuple(sorted(locator_paths.get(path, set()))),
                )
            )
        except _ForeignResetManifest:
            continue
    if len(validated_candidates) > 1:
        raise ResetRecoveryError(
            "Multiple interrupted resets were found; operator recovery is required"
        )
    pending = validated_candidates[0] if validated_candidates else None
    terminal_locators = tuple(
        (locator, locator_payloads[locator])
        for manifest, payload in records.items()
        if payload.get("status") in {"completed", "rolled_back"}
        for locator in sorted(locator_paths.get(manifest, set()))
    )
    return pending, terminal_locators


def _find_pending_reset_unlocked(config: Config) -> PendingReset | None:
    return _discover_reset_state_unlocked(config)[0]


def find_pending_reset(config: Config, barrier: ResetBarrier) -> PendingReset | None:
    """Return one trusted v2 in-progress reset while the maintenance barrier is held."""
    if barrier.owned_data_dir() != config.data_dir.resolve():
        raise ResetRecoveryError("Reset barrier does not protect the configured data directory")
    return _find_pending_reset_unlocked(config)


def interrupted_reset_diagnostic(config: Config) -> str | None:
    """Describe pending or unsafe reset state without locks, writes, or recovery actions."""
    try:
        pending = _find_pending_reset_unlocked(config)
    except ResetRecoveryError as exc:
        return f"blocked ({exc})"
    if pending is None:
        return None
    return (
        f"interrupted reset {pending.reset_id} is pending; exclusive startup will validate "
        "and attempt a lossless rollback"
    )


def _inspect_root(root: RecoveryRoot, reset_id: str) -> str:
    source = _optional_directory_identity(root.source, label="Reset source")
    quarantine = _optional_directory_identity(root.quarantine, label="Reset quarantine")
    failed = _optional_directory_identity(
        root.failed_fresh(reset_id), label="Interrupted fresh-data archive"
    )
    if quarantine is not None and quarantine != root.expected:
        raise ResetRecoveryError(
            "Interrupted reset quarantine identity does not match its manifest"
        )
    if source is not None and source == root.expected and quarantine is not None:
        raise ResetRecoveryError("Interrupted reset exposes the original root twice")
    if source is not None and quarantine is not None and failed is not None:
        raise ResetRecoveryError("Interrupted reset recovery paths are ambiguous")
    if source is None and quarantine is None:
        raise ResetRecoveryError("Interrupted reset is missing its original data root")
    if source is not None and quarantine is None and source != root.expected:
        raise ResetRecoveryError("Interrupted reset is missing its original quarantine")
    if source is not None and quarantine is not None:
        return "park_then_restore"
    if source is None and quarantine is not None:
        return "restore"
    return "restored"


def _inspect_absent_root(root: AbsentRoot, reset_id: str) -> str:
    source = _optional_directory_identity(root.source, label="Reset-created root")
    failed = _optional_directory_identity(
        root.failed_fresh(reset_id), label="Interrupted fresh-data archive"
    )
    if source is not None and failed is not None:
        raise ResetRecoveryError("Interrupted reset has an ambiguous originally-absent root")
    return "park" if source is not None else "absent"


def _retire_pending_locators(pending: PendingReset) -> None:
    if not pending.locators:
        return
    data_root = next(root.source for root in pending.roots if "data" in root.roles)
    expected = manifest_locator_payload(
        reset_id=pending.reset_id,
        manifest=pending.path,
        config_root=_validated_path(
            pending.payload.get("config_root"), label="manifest configuration root"
        ),
        data_root=data_root,
        manifest_payload=pending.payload,
    )
    for locator in pending.locators:
        retire_manifest_locator(locator, expected)


def recover_interrupted_reset(
    config: Config,
    barrier: ResetBarrier,
    lock: InstanceLock,
) -> bool:
    """Rollback one v2 interrupted reset without deleting old or partial-fresh data."""
    expected = config.data_dir.resolve()
    if barrier.owned_data_dir() != expected or lock.owned_data_dir() != expected:
        raise ResetRecoveryError("Reset recovery locks do not protect the configured data root")
    pending, terminal_locators = _discover_reset_state_unlocked(config)
    for locator, payload in terminal_locators:
        retire_manifest_locator(locator, payload)
    if pending is None:
        return False

    try:
        root_states = [(root, _inspect_root(root, pending.reset_id)) for root in pending.roots]
        absent_states = [
            (root, _inspect_absent_root(root, pending.reset_id)) for root in pending.absent_roots
        ]

        for root, state in reversed(absent_states):
            if state == "park":
                if _inspect_absent_root(root, pending.reset_id) != "park":
                    raise ResetRecoveryError("Originally-absent reset root changed during recovery")
                durable_rename_no_replace(root.source, root.failed_fresh(pending.reset_id))

        for root, state in reversed(root_states):
            if _inspect_root(root, pending.reset_id) != state:
                raise ResetRecoveryError("Interrupted reset root changed during recovery")
            if state == "park_then_restore":
                durable_rename_no_replace(root.source, root.failed_fresh(pending.reset_id))
                state = "restore"
            if state == "restore":
                if _inspect_root(root, pending.reset_id) != "restore":
                    raise ResetRecoveryError("Interrupted reset root changed during recovery")
                durable_rename_no_replace(root.quarantine, root.source)

        for root in pending.roots:
            if _inspect_root(root, pending.reset_id) != "restored":
                raise ResetRecoveryError("Interrupted reset root was not restored")
        for root in pending.absent_roots:
            if _inspect_absent_root(root, pending.reset_id) != "absent":
                raise ResetRecoveryError("Interrupted reset-created root was not archived")

        pending.payload.update(
            status="rolled_back",
            rolled_back_at=dt.datetime.now(dt.UTC).isoformat(),
            error_type="InterruptedResetRecovery",
        )
        write_manifest(pending.path, pending.payload)
        if not manifest_matches(pending.path, pending.payload):
            raise ResetRecoveryError("Rolled-back reset manifest could not be verified")
        _retire_pending_locators(pending)
        return True
    except (OSError, ResetRecoveryError) as exc:
        if pending.payload.get("status") == "rolled_back":
            try:
                if manifest_matches(pending.path, pending.payload):
                    _retire_pending_locators(pending)
                    return True
            except ResetRecoveryError as retirement_exc:
                raise ResetRecoveryError(
                    "Interrupted reset was rolled back, but its locator could not be retired"
                ) from retirement_exc
        if isinstance(exc, ResetRecoveryError):
            raise
        raise ResetRecoveryError(
            "Interrupted reset recovery was blocked; all recoverable data remains preserved"
        ) from exc


@contextmanager
def reset_sensitive_writer(config: Config) -> Iterator[None]:
    """Serialize a writer with both current and legacy-compatible reset ownership."""
    with (
        ResetBarrier(config.data_dir) as barrier,
        InstanceLock(config.data_dir) as lock,
    ):
        recover_interrupted_reset(config, barrier, lock)
        yield
