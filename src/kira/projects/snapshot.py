"""Deterministic, project-scoped knowledge snapshots for proactive analysis.

A snapshot identifies the live source corpus, not one graph-build attempt.  The graph watermark is
recorded alongside the content hash but deliberately excluded from that hash: derived edges are a
delete/rebuild cache whose row ids change on an otherwise identical finalize.  Repeating finalize
over identical project content must therefore resolve to the same expensive-analysis identity.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from kira.graph.store import GraphStore
from kira.knowledge.store import KnowledgeStore, Source

MAX_SNAPSHOT_SOURCES = 20_000


class SnapshotError(RuntimeError):
    """The project cannot be sealed into a useful bounded analysis snapshot."""


@dataclass(frozen=True)
class SnapshotSource:
    """One live source in a snapshot.

    ``logical_path`` is a browser/project label, never the managed raw/markdown path.  Source ids
    are evidence pointers but are not part of the content identity, so a byte-identical rebuild of
    the same logical project remains stable across databases.
    """

    source_id: int
    logical_path: str
    kind: str
    content_hash: str
    markdown_hash: str
    byte_size: int
    review_status: str


@dataclass(frozen=True)
class ProjectSnapshot:
    project_id: int
    snapshot_hash: str
    graph_watermark: int
    sources: tuple[SnapshotSource, ...]
    coverage: dict[str, int]


def _logical_path(source: Source, project_id: int) -> str:
    prefix = f"chat-upload:{project_id}:"
    if source.origin.startswith(prefix):
        value = source.origin[len(prefix) :]
    elif source.title:
        value = source.title
    else:
        # Keep non-browser origins out of downstream displays while retaining a stable identity.
        opaque = hashlib.sha256(source.origin.encode("utf-8")).hexdigest()[:16]
        value = f"{source.kind}:{opaque}"
    normalized = value.replace("\\", "/").strip("/")
    return normalized or f"source:{source.id}"


def _manifest_entry(source: Source, project_id: int) -> dict[str, object]:
    return {
        "path": _logical_path(source, project_id),
        "kind": source.kind,
        "content_hash": source.content_hash,
        "markdown_hash": source.markdown_hash,
        "converter": source.converter,
        "converter_version": source.converter_version,
        "review_status": source.review_status,
    }


async def seal_snapshot(
    knowledge: KnowledgeStore,
    graph: GraphStore,
    project_id: int,
    *,
    max_sources: int = MAX_SNAPSHOT_SOURCES,
) -> ProjectSnapshot:
    """Seal the current live project corpus into one stable snapshot.

    The function is read-only and intentionally runs after graph rebuild.  It records a bodies-free
    manifest plus graph counts/watermark; source bodies, managed paths, and embeddings never enter
    the snapshot identity or result.
    """

    if project_id <= 0:
        raise SnapshotError("project_id must be positive")
    if max_sources <= 0:
        raise SnapshotError("max_sources must be positive")
    sources = await knowledge.list_sources(status="live", project_id=project_id)
    if not sources:
        raise SnapshotError("project has no live knowledge sources")
    if len(sources) > max_sources:
        raise SnapshotError(
            f"project has {len(sources)} live sources; analysis cap is {max_sources}"
        )

    manifest = [_manifest_entry(source, project_id) for source in sources]
    manifest.sort(
        key=lambda item: (
            str(item["path"]).casefold(),
            str(item["path"]),
            str(item["kind"]),
            str(item["content_hash"]),
            str(item["markdown_hash"]),
            str(item["converter"]),
            str(item["converter_version"]),
            str(item["review_status"]),
        )
    )
    encoded = json.dumps(
        {"version": 1, "sources": manifest},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    snapshot_hash = hashlib.sha256(encoded).hexdigest()

    graph_row = await (
        await graph.db.execute(
            "SELECT COALESCE(MAX(id), 0), COUNT(*), "
            "COALESCE(SUM(CASE WHEN edge_kind='imports' THEN 1 ELSE 0 END), 0) "
            "FROM graph_edges WHERE project_id=? AND status='live'",
            (project_id,),
        )
    ).fetchone()
    watermark = int(graph_row[0]) if graph_row else 0
    edge_count = int(graph_row[1]) if graph_row else 0
    import_edges = int(graph_row[2]) if graph_row else 0
    reviewed = sum(source.review_status == "reviewed" for source in sources)

    source_refs = tuple(
        SnapshotSource(
            source_id=source.id,
            logical_path=_logical_path(source, project_id),
            kind=source.kind,
            content_hash=source.content_hash,
            markdown_hash=source.markdown_hash,
            byte_size=source.byte_size,
            review_status=source.review_status,
        )
        for source in sorted(
            sources,
            key=lambda source: (
                _logical_path(source, project_id).casefold(),
                _logical_path(source, project_id),
                source.id,
            ),
        )
    )
    return ProjectSnapshot(
        project_id=project_id,
        snapshot_hash=snapshot_hash,
        graph_watermark=watermark,
        sources=source_refs,
        coverage={
            "files_total": len(sources),
            "files_reviewed": reviewed,
            "files_unreviewed": len(sources) - reviewed,
            "bytes_total": sum(source.byte_size for source in sources),
            "graph_edges": edge_count,
            "import_edges": import_edges,
        },
    )
