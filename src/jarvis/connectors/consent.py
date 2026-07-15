"""Fail-closed connector consent after a whole-instance data reset.

OAuth tokens move with the quarantined data root, but environment-backed Telegram credentials
do not.  A fresh instance therefore carries this small local lock until each provider is
deliberately reconnected from the terminal.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

LOCKED_PROVIDERS = frozenset({"google", "kakao", "telegram"})
_MARKER_NAME = ".integration-consent.json"


def integration_consent_path(data_dir: Path) -> Path:
    return data_dir / _MARKER_NAME


def _read_locked(data_dir: Path) -> set[str]:
    marker = integration_consent_path(data_dir)
    if not marker.exists():
        return set()
    try:
        payload = json.loads(marker.read_text(encoding="utf-8"))
        values = payload["locked_providers"]
        if payload.get("version") != 1 or not isinstance(values, list):
            raise ValueError("invalid integration consent marker")
        parsed = {str(value) for value in values}
        if not parsed.issubset(LOCKED_PROVIDERS):
            raise ValueError("unknown provider in integration consent marker")
        return parsed
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        # A damaged marker must never turn into implicit consent.
        return set(LOCKED_PROVIDERS)


def locked_integrations(data_dir: Path) -> frozenset[str]:
    return frozenset(_read_locked(data_dir))


def integration_is_locked(data_dir: Path, provider: str) -> bool:
    return provider in _read_locked(data_dir)


def _write_marker(data_dir: Path, locked: set[str]) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    marker = integration_consent_path(data_dir)
    payload = {
        "version": 1,
        "reason": "reset_all_kira_data",
        "locked_providers": sorted(locked),
    }
    fd, temporary = tempfile.mkstemp(prefix=f".{marker.name}.", dir=data_dir)
    temporary_path = Path(temporary)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, marker)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise


def lock_all_integrations(data_dir: Path) -> None:
    _write_marker(data_dir, set(LOCKED_PROVIDERS))


def unlock_integration(data_dir: Path, provider: str) -> None:
    if provider not in LOCKED_PROVIDERS:
        raise ValueError(f"Unknown integration provider: {provider}")
    marker = integration_consent_path(data_dir)
    if not marker.exists():
        return
    locked = _read_locked(data_dir)
    locked.discard(provider)
    if locked:
        _write_marker(data_dir, locked)
    else:
        marker.unlink(missing_ok=True)
