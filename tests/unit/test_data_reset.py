"""Whole-instance reset is offline, owner-gated, quarantine-first, and reversible."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from jarvis.cli import reset as reset_module
from jarvis.cli.reset import DataResetError, reset_all_data
from jarvis.config import load_config
from jarvis.connectors.consent import LOCKED_PROVIDERS, locked_integrations
from jarvis.persistence import SessionStore
from jarvis.persistence.db import connect
from jarvis.persistence.instance_lock import InstanceLock
from jarvis.ui.owner_auth import Argon2PasswordHasher, OwnerAuthService

PASSWORD = "A unique reset passphrase 2026!"


def _matching(directory: Path, pattern: str) -> list[Path]:
    return list(directory.glob(pattern)) if directory.exists() else []


def _entries(directory: Path) -> list[Path]:
    return list(directory.iterdir())


async def _seed_instance(root: Path):
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

    db = await connect(config.data_dir / "jarvis.db")
    auth = OwnerAuthService(
        db,
        SessionStore(db).lock,
        hasher=Argon2PasswordHasher(time_cost=1, memory_cost=1024, parallelism=1),
    )
    grant = await auth.issue_auth_grant("enroll")
    await auth.enroll(grant.token, "habib", PASSWORD)
    await db.close()
    return config


async def test_reset_quarantines_old_roots_bootstraps_fresh_ownerless_instance(
    tmp_path: Path,
) -> None:
    config = await _seed_instance(tmp_path)
    result = await reset_all_data(config, PASSWORD)

    assert (tmp_path / ".env").read_text(encoding="utf-8").startswith("TELEGRAM_BOT_TOKEN")
    assert (tmp_path / "config" / "settings.yaml").is_file()
    assert (tmp_path / "source-sentinel.txt").read_text(encoding="utf-8") == "source stays"
    assert locked_integrations(config.data_dir) == LOCKED_PROVIDERS
    assert not (config.knowledge_dir / "project.md").exists()
    assert not (config.logs_dir / "kairo.log").exists()

    data_quarantine = next(path for path in result.quarantines if (path / "jarvis.db").exists())
    assert (data_quarantine / "connectors" / "google_token.json").read_text(
        encoding="utf-8"
    ) == "QUARANTINED-TOKEN"
    assert (data_quarantine / "knowledge" / "project.md").is_file()
    assert any((path / "kairo.log").is_file() for path in result.quarantines)

    fresh = await connect(config.data_dir / "jarvis.db")
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


async def test_wrong_password_and_live_instance_leave_all_roots_untouched(tmp_path: Path) -> None:
    config = await _seed_instance(tmp_path)
    with pytest.raises(DataResetError, match="password"):
        await reset_all_data(config, "A wrong but sufficiently long reset password")
    assert (config.knowledge_dir / "project.md").is_file()
    assert not _matching(tmp_path, ".*.kairo-quarantine-*")
    assert not (tmp_path / ".kairo-reset-manifests").exists()

    with InstanceLock(config.data_dir), pytest.raises(DataResetError, match="already running"):
        await reset_all_data(config, PASSWORD)
    assert (config.data_dir / "jarvis.db").is_file()


async def test_bootstrap_failure_restores_every_quarantine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = await _seed_instance(tmp_path)

    async def fail_bootstrap(_database: Path) -> int:
        raise RuntimeError("injected bootstrap failure")

    monkeypatch.setattr(reset_module, "_bootstrap_fresh_database", fail_bootstrap)
    with pytest.raises(DataResetError, match="original Kairo data was restored"):
        await reset_all_data(config, PASSWORD)

    assert (config.data_dir / "jarvis.db").is_file()
    assert (config.knowledge_dir / "project.md").read_text(encoding="utf-8") == "old knowledge"
    assert (config.logs_dir / "kairo.log").read_text(encoding="utf-8") == "old log"
    assert not _matching(tmp_path, ".*.kairo-quarantine-*")
    manifests = _matching(tmp_path / ".kairo-reset-manifests", "*.json")
    assert len(manifests) == 1
    assert json.loads(manifests[0].read_text(encoding="utf-8"))["status"] == "rolled_back"


async def test_external_knowledge_requires_separate_explicit_consent(tmp_path: Path) -> None:
    config = await _seed_instance(tmp_path)
    external = tmp_path / "external-vault"
    external.mkdir()
    (external / "private-note.md").write_text("preserve me", encoding="utf-8")
    config.knowledge.dir = external

    with pytest.raises(DataResetError, match="separate confirmation"):
        await reset_all_data(config, PASSWORD)
    assert (external / "private-note.md").is_file()
    assert (config.data_dir / "jarvis.db").is_file()

    result = await reset_all_data(config, PASSWORD, include_external_knowledge=True)
    vault_quarantine = next(
        path for path in result.quarantines if (path / "private-note.md").is_file()
    )
    assert vault_quarantine.parent == external.parent
    assert external.is_dir() and not _entries(external)


def test_reset_cli_refuses_noninteractive_input(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(reset_module.sys, "stdin", SimpleNamespace(isatty=lambda: False))
    assert reset_module.reset_cli(["data"]) == 1
    assert "interactively" in capsys.readouterr().out


def test_main_dispatches_reset_before_provider_key_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import jarvis.__main__ as entry

    monkeypatch.setattr(sys, "argv", ["jarvis", "reset", "data"])
    monkeypatch.setattr(reset_module, "reset_cli", lambda argv: 9 if argv == ["data"] else 1)
    with pytest.raises(SystemExit) as exited:
        entry.main()
    assert exited.value.code == 9
