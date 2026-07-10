"""``jarvis backup`` — explicit local recovery rituals, never a background job."""

from __future__ import annotations

import argparse
from pathlib import Path

from jarvis.persistence.backup import BackupError, create_backup, verify_backup


def backup_cli(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="jarvis backup", description="Create or verify a local, secret-excluding Kairo backup."
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("create", help="Create a timestamped backup under data/backups/.")
    verify = sub.add_parser("verify", help="Verify hashes and SQLite integrity without restoring.")
    verify.add_argument("backup", help="Path to one backup directory.")
    args = parser.parse_args(argv)

    try:
        if args.command == "create":
            from jarvis.config import load_config

            config = load_config()
            config.ensure_dirs()
            path = create_backup(config.data_dir)
            print(f"Backup created: {path}")
            return 0
        result = verify_backup(Path(args.backup))
        print(
            f"Backup verified: {result['backup']} "
            f"(schema v{result['database_user_version']}, {result['files']} files)"
        )
        return 0
    except BackupError as exc:
        print(f"Backup error: {exc}")
        return 1
