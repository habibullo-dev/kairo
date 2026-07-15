"""``kira graph …`` — terminal rituals over the memory graph (Phase 15).

    kira graph rebuild      delete + re-derive the derived edge cache from existing stores
    kira graph dedup        report likely-duplicate entities (no writes)
    kira graph merge        fold one asserted node into another (reversible, journaled)
    kira graph split        pull a node back out of the canonical it was merged into
    kira graph undo         reverse a journaled merge by id

Derive/read-only, EXCEPT the human-invoked merge/split/undo, which mutate asserted rows reversibly
(nodes retracted never deleted; edges re-pointed and restorable) and are CLI-only — no route, so
the graph UI gains no new authority. ``export`` lands in Task 10. A thin delegate imported on demand
from ``__main__`` (like ``eval`` / ``connect``) so ``--version`` / ``--help`` stay instant.
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
    # durable — every proposal must be approved in the Memory tab / `kira graph review`.
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
              f"material item(s) — PENDING review (uv run kira graph review / Memory tab).")
        return 0
    finally:
        await db.close()


def graph_cli(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="kira graph", description="Memory-graph rituals.")
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
    dd = sub.add_parser("dedup", help="Report likely-duplicate entities (report-only, no writes).")
    dd.add_argument("--project", type=int, help="restrict to this project (default: all + global)")
    dd.add_argument("--threshold", type=float, default=0.90, help="cosine floor for 'similar'")
    mg = sub.add_parser("merge", help="Fold one asserted node into another (reversible).")
    mg.add_argument("--into", type=int, required=True, metavar="ID", help="surviving node id")
    mg.add_argument("merged", type=int, help="node id to fold in (retracted, never deleted)")
    sp = sub.add_parser("split", help="Reverse the most recent merge that folded this node away.")
    sp.add_argument("node", type=int, help="node id to pull back out")
    un = sub.add_parser("undo", help="Reverse a journaled merge by its id.")
    un.add_argument("merge_id", type=int, help="graph_merges journal id")
    ex = sub.add_parser("export", help="Project entities + memory into the Obsidian vault.")
    ex.add_argument("--project", type=int, help="restrict to this project (default: all + global)")
    ex.add_argument("--write", action="store_true", help="apply (default: dry-run diff summary)")
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
    if args.cmd == "dedup":
        return asyncio.run(_run_dedup(args.project, args.threshold))
    if args.cmd == "merge":
        return asyncio.run(_run_merge(args.into, args.merged))
    if args.cmd == "split":
        return asyncio.run(_run_split(args.node))
    if args.cmd == "undo":
        return asyncio.run(_run_undo(args.merge_id))
    if args.cmd == "export":
        return asyncio.run(_run_export(args.project, args.write))
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
            print("usage: uv run kira graph review --project N | --approve ID | --reject ID")
            return 2
        return 0
    finally:
        await db.close()


async def _graph_db():
    from jarvis.config import load_config
    from jarvis.graph import GraphStore
    from jarvis.persistence.db import connect

    db = await connect(load_config().data_dir / "jarvis.db")
    return db, GraphStore(db, asyncio.Lock())


async def _run_dedup(project_id: int | None, threshold: float) -> int:
    from jarvis.graph.merge import find_duplicates
    from jarvis.graph.store import ANY_PROJECT

    db, store = await _graph_db()
    try:
        scope = project_id if project_id is not None else ANY_PROJECT
        cands = await find_duplicates(store, project_id=scope, threshold=threshold)
        if not cands:
            print("no duplicate candidates found (report-only).")
            return 0
        print(
            f"{len(cands)} candidate pair(s) — REPORT ONLY "
            "(confirm with `uv run kira graph merge`):"
        )
        for c in cands:
            print(f"  [{c.kind}] #{c.a_id} {c.a_title!r} ~ #{c.b_id} {c.b_title!r} "
                  f"({c.reason} {c.score:.3f})")
        return 0
    finally:
        await db.close()


async def _run_merge(canonical_id: int, merged_id: int) -> int:
    db, store = await _graph_db()
    try:
        mid = await store.merge_nodes(
            canonical_id=canonical_id, merged_id=merged_id, created_by="user")
        print(f"merged #{merged_id} into #{canonical_id} (journal #{mid}); "
              f"reverse with `uv run kira graph undo {mid}` or "
              f"`uv run kira graph split {merged_id}`.")
        return 0
    except ValueError as e:
        print(f"merge refused: {e}")
        return 2
    finally:
        await db.close()


async def _run_split(node_id: int) -> int:
    from jarvis.graph.merge import split

    db, store = await _graph_db()
    try:
        mid = await split(store, node_id)
        if mid is None:
            print(f"node #{node_id} was not merged into anything — nothing to split.")
            return 2
        print(f"split #{node_id} back out (reversed merge journal #{mid}).")
        return 0
    finally:
        await db.close()


async def _run_undo(merge_id: int) -> int:
    db, store = await _graph_db()
    try:
        ok = await store.undo_merge(merge_id)
        state = "reversed" if ok else "no-op (unknown or already undone)"
        print(f"undo merge #{merge_id}: {state}")
        return 0 if ok else 2
    finally:
        await db.close()


async def _run_export(project_id: int | None, write: bool) -> int:
    from jarvis.config import load_config
    from jarvis.graph.obsidian import export
    from jarvis.graph.store import ANY_PROJECT
    from jarvis.memory import MemoryStore

    config = load_config()
    db, store = await _graph_db()
    try:
        scope = project_id if project_id is not None else ANY_PROJECT
        report = await export(
            store, MemoryStore(db, store.lock), config.knowledge_dir / "wiki",
            project_id=scope, write=write)
        print(f"graph export{'' if write else ' (dry-run — no files written)'}: {report.summary()}")
        for a in report.actions:
            flag = " [redacted]" if a.redacted else ""
            print(f"  {a.status:>14}  {a.path}{flag}")
        return 0
    finally:
        await db.close()
