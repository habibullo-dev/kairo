"""Deterministic graph-first context for the project-intelligence council.

The host maps structure before any model runs: file inventory, language/area counts, central files,
and verified local imports.  It never reads source bodies.  Labels are untrusted and normalized,
the final overview is secret-redacted, and coverage states exactly what was omitted.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from pathlib import PurePosixPath

from jarvis.graph.service import dependency_subgraph, subgraph
from jarvis.graph.store import GraphStore
from jarvis.knowledge.secrets import scan_text
from jarvis.projects.snapshot import ProjectSnapshot


@dataclass(frozen=True)
class ProjectContextOverview:
    text: str
    coverage: dict[str, int | bool]


def _label(value: object, *, cap: int = 240) -> str:
    text = " ".join(str(value or "").replace("\x00", "").split())
    return text[:cap] or "(unnamed)"


def _top_area(path: str) -> str:
    parts = PurePosixPath(path).parts
    return parts[0] if len(parts) > 1 else "root"


async def build_project_overview(
    snapshot: ProjectSnapshot,
    graph: GraphStore,
    *,
    max_files: int = 400,
    max_nodes: int = 120,
    max_chars: int = 20_000,
) -> ProjectContextOverview:
    """Build one bounded, bodies-free overview for a sealed project snapshot."""

    if min(max_files, max_nodes, max_chars) <= 0:
        raise ValueError("project overview limits must be positive")
    max_nodes = min(max_nodes, 120)
    structure = await subgraph(
        graph, snapshot.project_id, depth=2, limit=max_nodes, view="structure"
    )
    dependencies = await dependency_subgraph(graph, snapshot.project_id, limit=max_nodes)

    files = list(snapshot.sources[:max_files])
    extensions = Counter(
        PurePosixPath(source.logical_path).suffix.lower() or "(none)"
        for source in snapshot.sources
    )
    areas = Counter(_top_area(source.logical_path) for source in snapshot.sources)
    lines = [
        "PROJECT SNAPSHOT OVERVIEW (host-derived; labels are untrusted data)",
        f"snapshot: {snapshot.snapshot_hash}",
        f"project_id: {snapshot.project_id}",
        f"files: {len(snapshot.sources)} total; {len(files)} listed",
        f"graph: {len(structure.get('nodes', []))} nodes; "
        f"{len(structure.get('edges', []))} edges; watermark {snapshot.graph_watermark}",
        "extensions: "
        + ", ".join(f"{_label(name)}={count}" for name, count in extensions.most_common(20)),
        "top-level areas: "
        + ", ".join(f"{_label(name)}={count}" for name, count in areas.most_common(20)),
        "",
        "FILE INVENTORY (path metadata only)",
    ]
    lines.extend(
        f"- {_label(source.logical_path)} [{_label(source.kind)}; {source.review_status}]"
        for source in files
    )
    omitted_files = len(snapshot.sources) - len(files)
    if omitted_files:
        lines.append(f"- ... {omitted_files} additional files omitted by context cap")

    dep_nodes = dependencies.get("nodes", [])
    dep_edges = dependencies.get("edges", [])
    labels = {node.get("id"): _label(node.get("label")) for node in dep_nodes}
    lines.extend(["", "CENTRAL FILES (verified local dependency degree)"])
    for node in dep_nodes[:30]:
        lines.append(f"- {labels.get(node.get('id'))} degree={int(node.get('degree') or 0)}")
    if not dep_nodes:
        lines.append("- no supported local dependency nodes were derived")

    lines.extend(["", "VERIFIED LOCAL IMPORT EDGES"])
    for edge in dep_edges[:160]:
        src = labels.get(edge.get("src"), _label(edge.get("src")))
        dst = labels.get(edge.get("dst"), _label(edge.get("dst")))
        lines.append(f"- {src} -> {dst}")
    if not dep_edges:
        lines.append("- no supported local imports were resolved")
    omitted_edges = max(0, len(dep_edges) - 160)
    if omitted_edges:
        lines.append(f"- ... {omitted_edges} additional import edges omitted by context cap")

    raw = "\n".join(lines)
    chars_truncated = len(raw) > max_chars
    if chars_truncated:
        suffix = "\n... overview truncated by character cap"
        raw = (raw[: max(0, max_chars - len(suffix))] + suffix)[:max_chars]
    scan = scan_text(raw)
    coverage: dict[str, int | bool] = {
        **snapshot.coverage,
        "files_listed": len(files),
        "files_omitted": omitted_files,
        "structure_nodes": len(structure.get("nodes", [])),
        "structure_edges": len(structure.get("edges", [])),
        "dependency_nodes": len(dep_nodes),
        "dependency_edges": len(dep_edges),
        "dependency_edges_omitted": omitted_edges,
        "context_secret_hits": scan.total_hits,
        "context_truncated": chars_truncated
        or bool(structure.get("truncated"))
        or bool(dependencies.get("truncated")),
        "context_chars": len(scan.redacted_text),
    }
    return ProjectContextOverview(text=scan.redacted_text, coverage=coverage)
