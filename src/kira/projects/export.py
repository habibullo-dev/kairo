"""Project memory ⇄ Markdown export/import (Phase 10 Task 4 / amendment A1).

A human ritual only (CLI ``kira project export|import``) — NEVER reachable by a tool,
agent, or orchestration role, so it can't become an exfiltration/injection channel. Two
security rules make import safe:

* **The path jail** (:func:`~kira.knowledge.wiki.safe_wiki_path`) confines every file to
  the export directory — a crafted filename can't escape or hit a sensitive path.
* **Inbound provenance is ignored.** Import forces ``project_id = <the target project>`` and
  ``source = 'import'`` (untrusted), and never trusts a file's ``project_id``/``source`` —
  so a hand-crafted file can't launder attacker text into a *trusted* or *cross-project*
  memory (pre-mortem #13). Dedup (in :meth:`MemoryService.remember`) is scoped to the
  target project, so a re-import collapses instead of piling up.

Export writes one ``<id>.md`` per live memory in the project's *own* scope (exact, not the
recall union — you export what belongs to the project, not the global memories it can see).
Front-matter is merged like the wiki's: managed keys regenerated, any foreign (hand-added)
keys preserved verbatim. Blocking filesystem work runs in a worker thread (the async
functions only await the store/service).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path

from kira.knowledge.wiki import render_page, safe_wiki_path, split_front_matter
from kira.memory.service import MemoryService
from kira.memory.store import Memory, MemoryStore

#: Front-matter keys the exporter owns/regenerates. Everything else in a file is preserved.
_MANAGED_KEYS = frozenset({"id", "type", "source", "confidence", "created"})
_TYPES = frozenset({"fact", "preference", "project", "episode"})


@dataclass
class ImportReport:
    """Outcome of importing a directory of memory files."""

    created: int = 0
    duplicate: int = 0
    skipped: list[str] = field(default_factory=list)  # "filename: reason"


def _write_memory_files(out_dir: Path, memories: list[Memory]) -> None:
    """Blocking: write one ``<id>.md`` per memory, preserving a file's foreign keys."""
    out_dir.mkdir(parents=True, exist_ok=True)
    for m in memories:
        target = safe_wiki_path(out_dir, f"{m.id}.md")
        existing_fm: dict = {}
        if target.exists():
            existing_fm, _ = split_front_matter(target.read_text(encoding="utf-8"))
        managed = {
            "id": m.id,
            "type": m.type,
            "source": m.source,
            "confidence": m.provenance.confidence,
            "created": m.created_at,
        }
        preserved = {k: v for k, v in existing_fm.items() if k not in _MANAGED_KEYS}
        target.write_text(render_page({**managed, **preserved}, m.content), encoding="utf-8")


def _read_memory_files(in_dir: Path) -> list[tuple[str, str | None, str]]:
    """Blocking: parse every ``*.md`` in ``in_dir`` → (name, mem_type|None-if-skip, content).

    Only the memory *type* is honored from a file (validated, defaulting to 'fact'); the
    file's project_id/source/confidence are NOT read here — import forces them."""
    if not in_dir.is_dir():
        return []
    out: list[tuple[str, str | None, str]] = []
    for path in sorted(in_dir.glob("*.md")):
        try:
            front_matter, body = split_front_matter(path.read_text(encoding="utf-8"))
        except OSError as exc:
            out.append((path.name, None, f"unreadable ({exc})"))
            continue
        content = body.strip()
        if not content:
            out.append((path.name, None, "empty body"))
            continue
        mem_type = front_matter.get("type")
        if mem_type not in _TYPES:
            mem_type = "fact"  # a hand-crafted/foreign file falls back to the safe default
        out.append((path.name, mem_type, content))
    return out


async def export_project_memories(store: MemoryStore, project_id: int, out_dir: Path) -> int:
    """Write one ``<id>.md`` per live memory in ``project_id``'s own scope to ``out_dir``.
    Preserves any foreign front-matter keys of a file already there. Returns the count."""
    # Exact project scope (include_global=False): export the project's OWN memories, not the
    # global ones it merely recalls alongside.
    memories = await store.all_live(project_id=project_id, include_global=False)
    await asyncio.to_thread(_write_memory_files, out_dir, memories)
    return len(memories)


async def import_project_memories(
    service: MemoryService, project_id: int | None, in_dir: Path
) -> ImportReport:
    """Import every ``*.md`` in ``in_dir`` into ``project_id`` (None == global). Content and
    ``type`` come from the file; ``project_id`` and ``source`` are FORCED (inbound values in
    the file are ignored — the security rule). Dedup is scoped to the target project."""
    report = ImportReport()
    for name, mem_type, content in await asyncio.to_thread(_read_memory_files, in_dir):
        if mem_type is None:
            report.skipped.append(f"{name}: {content}")  # content holds the skip reason
            continue
        result = await service.remember(
            content,
            mem_type,
            source="import",  # forced untrusted — never the file's claimed source
            project_id=project_id,  # forced target scope — never the file's claimed project
        )
        if result.action == "duplicate":
            report.duplicate += 1
        else:
            report.created += 1
    return report
