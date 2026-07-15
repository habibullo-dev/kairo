"""Entry point for the Kira console script and the legacy ``python -m jarvis`` route."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import sys

from jarvis import __version__


def _force_utf8_stdio() -> None:
    """Windows consoles/pipes often default to cp1252, which can't encode the
    model's Unicode output (em-dashes, ✓, …) and crashes on write. Force UTF-8
    with replacement so rendering never dies on an unencodable character."""
    for stream in (sys.stdout, sys.stderr):
        with contextlib.suppress(Exception):
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]


def main() -> None:
    _force_utf8_stdio()

    # `kira eval …` is the live eval gate (incl. the chunked profile). It lives under
    # tests/evals and is a dev/CI ritual, so it's a thin delegate imported only on demand.
    # The console script runs from an editable install, so add the repo root (which holds
    # tests/) to sys.path before importing; a real wheel has no tests/ and prints the hint.
    argv = sys.argv[1:]
    if argv and argv[0] == "eval":
        import pathlib

        repo_root = str(pathlib.Path(__file__).resolve().parents[2])
        if repo_root not in sys.path:
            sys.path.insert(0, repo_root)
        try:
            from tests.evals.runner import cli as eval_cli
        except ModuleNotFoundError:
            print("`kira eval` runs from the repo checkout (the eval harness is under tests/).")
            sys.exit(2)
        sys.exit(eval_cli(argv[1:]))

    # ``kira doctor`` is a local, read-only first-run diagnostic. It never starts the REPL,
    # creates runtime directories, migrates SQLite, contacts a provider, or prints secret values.
    if argv and argv[0] == "doctor":
        from jarvis.cli.doctor import doctor_cli

        sys.exit(doctor_cli(argv[1:]))

    # `kira connect <provider>` is the terminal ritual for granting account access (OAuth /
    # notifier config). A thin delegate imported on demand so --version/--help stay instant.
    if argv and argv[0] == "connect":
        from jarvis.cli.connect import connect_cli

        sys.exit(connect_cli(argv[1:]))

    # ``kira backup create|verify`` is an explicit local recovery ritual. It has no model,
    # connector, scheduler, or restore-overwrite path.
    if argv and argv[0] == "backup":
        from jarvis.cli.backup import backup_cli

        sys.exit(backup_cli(argv[1:]))

    # ``kira reset data`` is an attended, offline-only, quarantine-first reset. It runs
    # before provider-key validation and acquires the same exclusive lock as the workstation.
    if argv and argv[0] == "reset":
        from jarvis.cli.reset import reset_cli

        sys.exit(reset_cli(argv[1:]))

    # `kira graph <cmd>` — memory-graph rituals (rebuild the derived edge cache, …). Derive/
    # read-only; a thin delegate imported on demand.
    if argv and argv[0] == "graph":
        from jarvis.cli.graph import graph_cli

        sys.exit(graph_cli(argv[1:]))

    # `kira dream run <job>` — run ONE proposal-only dreaming job ATTENDED (Phase 16). Never
    # schedules; the output is a proposal reviewed in the Notification Center. On-demand delegate.
    if argv and argv[0] == "dream":
        from jarvis.cli.dream import dream_cli

        sys.exit(dream_cli(argv[1:]))

    parser = argparse.ArgumentParser(prog="kira", description="Kira workplace assistant.")
    parser.add_argument("--version", action="version", version=f"kira {__version__}")
    parser.add_argument(
        "--resume", action="store_true", help="Resume the most recent session (task 10)."
    )
    parser.add_argument(
        "--voice",
        action="store_true",
        help="Push-to-talk voice interface (read-only by default; risky actions confirm "
        "on screen). Requires voice.enabled: true and the voice extra.",
    )
    parser.add_argument(
        "--ui",
        action="store_true",
        help="Workstation UI — an authenticated local web surface on loopback (approvals "
        "explicit + audited; no new authority). Requires ui.enabled: true and the ui extra.",
    )
    args = parser.parse_args()

    # Imports deferred so `--version`/`--help` stay instant and never need a key.
    from rich.console import Console

    from jarvis.cli.repl import run_repl, run_ui, run_voice
    from jarvis.config import ConfigError, load_config
    from jarvis.observability import configure_logging
    from jarvis.persistence.database_identity import DatabaseIdentityError, migrate_live_database
    from jarvis.persistence.instance_lock import (
        InstanceAlreadyRunning,
        InstanceLock,
        ResetBarrier,
        ResetMaintenanceBusy,
    )
    from jarvis.persistence.reset_recovery import (
        ResetRecoveryError,
        interrupted_reset_diagnostic,
        recover_interrupted_reset,
    )

    console = Console()
    try:
        config = load_config()
        if interrupted_reset_diagnostic(config) is None:
            config.require("anthropic")
    except ConfigError as exc:
        console.print(f"[red]Configuration error:[/] {exc}")
        sys.exit(1)

    try:
        with (
            ResetBarrier(config.data_dir) as barrier,
            InstanceLock(config.data_dir) as lock,
        ):
            recover_interrupted_reset(config, barrier, lock)
            config.require("anthropic")
            config.ensure_dirs()
            database = migrate_live_database(lock)
            barrier.release()
            configure_logging(config.logs_dir, **config.logging.model_dump())
            if args.ui:
                asyncio.run(run_ui(config, console=console, database=database))
            elif args.voice:
                asyncio.run(run_voice(config, console=console, database=database))
            else:
                asyncio.run(
                    run_repl(config, resume=args.resume, console=console, database=database)
                )
    except ConfigError as exc:
        console.print(f"[red]Configuration error:[/] {exc}")
        sys.exit(1)
    except (InstanceAlreadyRunning, ResetMaintenanceBusy, ResetRecoveryError) as exc:
        console.print(f"[red]Startup blocked:[/] {exc}")
        sys.exit(1)
    except DatabaseIdentityError as exc:
        console.print(f"[red]Database startup blocked:[/] {exc}")
        sys.exit(1)
    except KeyboardInterrupt:
        console.print("\nBye.")


if __name__ == "__main__":
    main()
