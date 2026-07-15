"""``kira backup`` — explicit local recovery rituals, never a background job."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from jarvis.persistence.backup import BackupError, create_backup, verify_backup


def backup_cli(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="kira backup",
        description=(
            "Create or verify a private Kira backup that excludes known credential stores, "
            "configuration, logs, and sensitive filenames."
        ),
        epilog=(
            "Backups can still contain private or secret user-authored content. Protect them as "
            "private data. Verification is read-only; restore is not supported."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser(
        "create",
        help="Create a timestamped backup under data/backups/ (Kira must be stopped).",
    )
    verify = sub.add_parser("verify", help="Verify hashes and SQLite integrity without restoring.")
    verify.add_argument("backup", help="Path to one backup directory.")
    args = parser.parse_args(argv)

    try:
        if args.command == "create":
            from jarvis.config import ConfigError, load_config

            try:
                config = load_config()
            except ConfigError as exc:
                print(f"Kira backup configuration error: {exc}", file=sys.stderr)
                return 1
            from jarvis.persistence.instance_lock import (
                InstanceAlreadyRunning,
                ResetMaintenanceBusy,
            )
            from jarvis.persistence.reset_recovery import (
                ResetRecoveryError,
                reset_sensitive_writer,
            )

            try:
                with reset_sensitive_writer(config):
                    path = create_backup(config.data_dir)
            except (InstanceAlreadyRunning, ResetMaintenanceBusy, ResetRecoveryError) as exc:
                raise BackupError(str(exc)) from exc
            print(f"Kira backup created: {path}")
            return 0
        result = verify_backup(Path(args.backup))
        format_label = (
            "legacy backup format v1"
            if result["schema_version"] == 1
            else f"Kira backup format v{result['schema_version']}"
        )
        print(
            f"Kira backup verified: {result['backup']} ({format_label}; database schema "
            f"v{result['database_user_version']}; {result['files']} files)"
        )
        return 0
    except BackupError as exc:
        print(f"Kira backup error: {exc}", file=sys.stderr)
        return 1
