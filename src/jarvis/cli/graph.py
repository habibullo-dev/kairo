"""``jarvis graph …`` — terminal rituals over the memory graph (Phase 15).

    jarvis graph rebuild      delete + re-derive the derived edge cache from existing stores

Derive/read-only; adds no authority. More subcommands (suggest / review / reindex / merge / export)
land in later Phase-15 tasks. A thin delegate imported on demand from ``__main__`` (like ``eval`` /
``connect``) so ``--version`` / ``--help`` stay instant.
"""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path


async def _run_rebuild(data_dir: Path) -> int:
    from jarvis.graph import GraphStore
    from jarvis.graph.builder import rebuild
    from jarvis.persistence.db import connect

    db = await connect(data_dir / "jarvis.db")
    try:
        counts = await rebuild(GraphStore(db, asyncio.Lock()))
        total = sum(counts.values())
        print(f"graph rebuild: {total} derived edges")
        for kind in sorted(counts):
            print(f"  {kind}: {counts[kind]}")
        return 0
    finally:
        await db.close()


def graph_cli(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="jarvis graph", description="Memory-graph rituals.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("rebuild", help="Delete + re-derive the derived edge cache (deterministic).")
    args = ap.parse_args(argv)

    if args.cmd == "rebuild":
        from jarvis.config import load_config

        return asyncio.run(_run_rebuild(load_config().data_dir))
    return 1
