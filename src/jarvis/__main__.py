"""Entry point for `python -m jarvis` and the `jarvis` console script."""

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
    parser = argparse.ArgumentParser(prog="jarvis", description="A from-scratch agentic assistant.")
    parser.add_argument("--version", action="version", version=f"jarvis {__version__}")
    parser.add_argument(
        "--resume", action="store_true", help="Resume the most recent session (task 10)."
    )
    args = parser.parse_args()

    # Imports deferred so `--version`/`--help` stay instant and never need a key.
    from rich.console import Console

    from jarvis.cli.repl import run_repl
    from jarvis.config import ConfigError, load_config
    from jarvis.observability import configure_logging

    console = Console()
    try:
        config = load_config(require=("anthropic",))
    except ConfigError as exc:
        console.print(f"[red]Configuration error:[/] {exc}")
        sys.exit(1)

    config.ensure_dirs()
    configure_logging(config.logs_dir)

    try:
        asyncio.run(run_repl(config, resume=args.resume, console=console))
    except KeyboardInterrupt:
        console.print("\nBye.")


if __name__ == "__main__":
    main()
