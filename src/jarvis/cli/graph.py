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


async def _run_suggest(project_id: int, limit: int) -> int:
    # Explicit-invoke extraction: gather bounded local material, run the ledgered utility model, and
    # write QUARANTINED suggestions (pending human review). Makes a live model call; adds nothing
    # durable — every proposal must be approved in the Memory tab / `jarvis graph review`.
    from jarvis.cli.repl import _utility_client
    from jarvis.config import load_config
    from jarvis.graph import GraphStore
    from jarvis.graph.suggest import gather_material, suggest, utility_extractor
    from jarvis.models.registry import ModelRegistry
    from jarvis.persistence.db import connect

    config = load_config()
    db = await connect(config.data_dir / "jarvis.db")
    try:
        store = GraphStore(db, asyncio.Lock())
        materials = await gather_material(store, project_id, limit=limit)
        if not materials:
            print(f"project {project_id}: no material to extract from (no run summaries).")
            return 0
        model = ModelRegistry(config.models.routes).route("utility").model
        extract = utility_extractor(_utility_client(config), model)
        ids = await suggest(store, materials, extract, project_id=project_id, extractor_model=model)
        print(f"project {project_id}: proposed {len(ids)} suggestion(s) from {len(materials)} "
              f"material item(s) — PENDING review (jarvis graph review / Memory tab).")
        return 0
    finally:
        await db.close()


def graph_cli(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="jarvis graph", description="Memory-graph rituals.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("rebuild", help="Delete + re-derive the derived edge cache (deterministic).")
    sg = sub.add_parser("suggest", help="Propose QUARANTINED memories from a project's material.")
    sg.add_argument("--project", type=int, required=True, help="project id to extract from")
    sg.add_argument("--limit", type=int, default=20, help="max material items to scan")
    args = ap.parse_args(argv)

    if args.cmd == "rebuild":
        from jarvis.config import load_config

        return asyncio.run(_run_rebuild(load_config().data_dir))
    if args.cmd == "suggest":
        return asyncio.run(_run_suggest(args.project, args.limit))
    return 1
