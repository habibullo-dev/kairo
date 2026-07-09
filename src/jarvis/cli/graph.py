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
    rv = sub.add_parser("review", help="List / approve / reject quarantined suggestions.")
    rv.add_argument("--project", type=int, help="list pending suggestions for this project")
    rv.add_argument("--approve", type=int, metavar="ID", help="approve a suggestion by id")
    rv.add_argument("--reject", type=int, metavar="ID", help="reject a suggestion by id")
    ri = sub.add_parser("reindex", help="Embed entities + unindexed memories (content-hash keyed).")
    ri.add_argument("--dry-run", action="store_true", help="report what would be embedded + spend")
    args = ap.parse_args(argv)

    if args.cmd == "rebuild":
        from jarvis.config import load_config

        return asyncio.run(_run_rebuild(load_config().data_dir))
    if args.cmd == "suggest":
        return asyncio.run(_run_suggest(args.project, args.limit))
    if args.cmd == "review":
        return asyncio.run(_run_review(args.project, args.approve, args.reject))
    if args.cmd == "reindex":
        return asyncio.run(_run_reindex(args.dry_run))
    return 1


async def _run_reindex(dry_run: bool) -> int:
    from jarvis.config import load_config
    from jarvis.graph import GraphStore
    from jarvis.graph.index import CostAwareEmbedder, reindex
    from jarvis.memory import VoyageEmbedder
    from jarvis.observability.cost import load_pricing
    from jarvis.persistence.db import connect

    config = load_config()
    db = await connect(config.data_dir / "jarvis.db")
    try:
        store = GraphStore(db, asyncio.Lock())
        pricing = load_pricing(config.root / "config" / "pricing.yaml")
        embedder = CostAwareEmbedder(VoyageEmbedder.from_config(config), pricing)
        report = await reindex(store, embedder, dry_run=dry_run)
        tag = " (dry-run — no spend)" if dry_run else ""
        print(f"graph reindex{tag}: {report}")
        return 0
    finally:
        await db.close()


async def _run_review(project_id: int | None, approve_id: int | None, reject_id: int | None) -> int:
    from jarvis.config import load_config
    from jarvis.graph import GraphStore
    from jarvis.graph.review import approve, reject
    from jarvis.graph.service import suggestions_view
    from jarvis.persistence.db import connect

    db = await connect(load_config().data_dir / "jarvis.db")
    try:
        store = GraphStore(db, asyncio.Lock())
        if approve_id is not None:
            print(await approve(store, approve_id, resolved_by="cli"))
        elif reject_id is not None:
            print(await reject(store, reject_id, resolved_by="cli"))
        elif project_id is not None:
            view = await suggestions_view(store, project_id)
            print(f"project {project_id}: {len(view['suggestions'])} pending suggestion(s)")
            for s in view["suggestions"]:
                print(f"  #{s['id']} [{s['kind']} · {s['trust_class']}] {s['preview']!r}")
        else:
            print("usage: jarvis graph review --project N | --approve ID | --reject ID")
            return 2
        return 0
    finally:
        await db.close()
