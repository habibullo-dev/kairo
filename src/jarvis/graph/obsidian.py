"""Obsidian bridge — deterministic export of the graph + memory into the vault (Phase 15 Task 10).

The vault (``knowledge/wiki/``) is already Obsidian-openable. This projects the graph's asserted
ENTITIES and each project's APPROVED MEMORIES into two reserved namespaces — ``_graph/`` (one page
per asserted node, front-matter = metadata, body = summary + ``[[wikilinks]]``) and ``_memory/``
(per-project memory index) — visible in Obsidian's own graph view. Guarantees (plan §10):

* **Deterministic:** same DB ⇒ byte-identical files. Ordering is by row id; every timestamp in a
  page comes from the SOURCE row (never the wall clock — the Phase-14 lesson), so a re-export is a
  no-op diff.
* **Non-destructive:** we write ONLY inside ``_graph/``/``_memory``, ONLY over files that carry our
  ``generated_by: kairo-graph`` front-matter marker. A user-authored or unmarked file at a target
  path is SKIPPED and reported, never overwritten. Containment is :func:`safe_wiki_path` (which also
  refuses the sensitive-path floor).
* **No secrets / no private:** nodes marked ``sensitivity=private`` are excluded; every rendered
  page is passed through a secret-shape redaction belt before it is written.
* **Dry-run by default:** :func:`export` plans every action; only ``write=True`` touches disk.

Import stays the EXISTING doors — a page the user edits is picked up by wiki reindex; a
``[[wikilink]]`` to a ``_graph`` entity becomes a derived edge on the next rebuild. A live two-way
file-watcher/merge engine is deliberately deferred.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from jarvis.graph.store import ANY_PROJECT, GraphStore
from jarvis.knowledge.wiki import render_page, safe_wiki_path, slugify, split_front_matter
from jarvis.memory.store import MemoryStore

MARKER = "kairo-graph"  # front-matter generated_by value — our overwrite permission slip
_NS = ("_graph/", "_memory/")  # the ONLY namespaces this exporter may write into

# Secret-shape backstop, mirroring jarvis.voice.render._SECRET_RE (kept local so the graph package
# does not depend on the voice package). Sources are already bodies-free/reviewed — this is a belt.
_SECRET_RE = re.compile(
    r"(sk-[A-Za-z0-9_-]{6,}|ghp_[A-Za-z0-9]{8,}|xox[baprs]-[A-Za-z0-9-]{8,}"
    r"|AKIA[0-9A-Z]{12,}|eyJ[A-Za-z0-9._-]{10,}|[A-Fa-f0-9]{32,})"
)


@dataclass
class ExportAction:
    """One planned/applied projection for a single vault page."""

    path: str      # wiki-relative posix path (always under a reserved namespace)
    status: str    # "write" | "unchanged" | "skip-user-file"
    redacted: bool = False


@dataclass
class ExportReport:
    actions: list[ExportAction] = field(default_factory=list)
    applied: bool = False

    def summary(self) -> str:
        w = sum(a.status == "write" for a in self.actions)
        u = sum(a.status == "unchanged" for a in self.actions)
        s = sum(a.status == "skip-user-file" for a in self.actions)
        r = sum(a.redacted for a in self.actions)
        verb = "wrote" if self.applied else "would write"
        return (f"{verb} {w}, unchanged {u}, skipped(user file) {s}, redacted {r} "
                f"— {len(self.actions)} target(s)")


def _mask(text: str) -> tuple[str, bool]:
    masked = _SECRET_RE.sub("[redacted]", text)
    return masked, masked != text


def _entity_basename(node) -> str:
    return f"{node.kind}-{node.id}-{slugify(node.title)}"


def _render_entity(node, connections: list[str]) -> str:
    fm: dict = {"generated_by": MARKER, "title": node.title, "kind": node.kind,
                "node_id": node.id, "trust_class": node.trust_class}
    if node.sensitivity:
        fm["sensitivity"] = node.sensitivity
    if node.project_id is not None:
        fm["project_id"] = node.project_id
    if node.labels:
        fm["aliases"] = list(node.labels)
    fm["created"], fm["updated"] = node.created_at, node.updated_at  # SOURCE-row times, not now()
    lines = [node.summary.strip()] if node.summary.strip() else []
    if connections:
        lines += ["", "## Connections", *[f"- [[{b}]]" for b in connections]]
    return render_page(fm, "\n".join(lines))


def _render_memory(project_id: int | None, mems: list) -> str:
    fm = {"generated_by": MARKER, "kind": "memory-index", "count": len(mems)}
    if project_id is not None:
        fm["project_id"] = project_id
    heading = f"Memory — {'Global' if project_id is None else f'Project {project_id}'}"
    lines = [f"# {heading}", ""]
    for m in mems:  # ordered by id (all_live) ⇒ deterministic
        conf = m.provenance.confidence
        tail = f" (confidence {conf:.2f})" if conf is not None else ""
        lines.append(f"- **[{m.type}]** {m.content.strip()} — _{m.source} · {m.created_at}_{tail}")
    return render_page(fm, "\n".join(lines))


async def export(
    store: GraphStore,
    memory: MemoryStore,
    wiki_dir,
    *,
    project_id: object = ANY_PROJECT,
    write: bool = False,
) -> ExportReport:
    """Project graph entities + memories into the vault. Returns a plan; writes only if ``write``.
    Deterministic and non-destructive (marker-guarded, namespace-contained, secret-redacted)."""
    pages: dict[str, str] = {}

    # --- entities → _graph/ (skip private; wikilink only to other exported entities) ---
    nodes = [n for n in await store.list_nodes(project_id=project_id) if n.sensitivity != "private"]
    ref_to_base = {(n.kind, str(n.id)): _entity_basename(n) for n in nodes}
    for n in nodes:
        conns: set[str] = set()
        for e in await store.neighbors(n.kind, str(n.id)):
            other = (e.dst_kind, e.dst_id) if (e.src_kind, e.src_id) == (n.kind, str(n.id)) \
                else (e.src_kind, e.src_id)
            if other in ref_to_base and other != (n.kind, str(n.id)):
                conns.add(ref_to_base[other])
        pages[f"_graph/{_entity_basename(n)}.md"] = _render_entity(n, sorted(conns))

    # --- memories → _memory/{project}.md (grouped, deterministic) ---
    # MemoryStore has its OWN ANY_PROJECT sentinel — translate "all" across the store boundary
    # (passing the graph sentinel would bind an object() and blow up).
    from jarvis.memory.store import ANY_PROJECT as _MEM_ANY

    mem_scope = _MEM_ANY if project_id is ANY_PROJECT else project_id
    groups: dict[int | None, list] = {}
    for m in await memory.all_live(project_id=mem_scope):
        groups.setdefault(m.project_id, []).append(m)
    for pid, mems in groups.items():
        slug = "global" if pid is None else f"project-{pid}"
        pages[f"_memory/{slug}.md"] = _render_memory(pid, mems)

    # --- plan / apply (sorted for a stable report; each guarded + redacted) ---
    report = ExportReport(applied=write)
    for rel in sorted(pages):
        assert rel.startswith(_NS), rel  # belt: never outside the reserved namespaces
        target = safe_wiki_path(wiki_dir, rel)  # jail + sensitive-path refusal
        text, redacted = _mask(pages[rel])
        if target.exists():
            existing = target.read_text(encoding="utf-8")
            if split_front_matter(existing)[0].get("generated_by") != MARKER:
                report.actions.append(ExportAction(rel, "skip-user-file"))
                continue  # a user-authored / unmarked file — never clobber
            if existing == text:
                report.actions.append(ExportAction(rel, "unchanged", redacted))
                continue
        if write:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
        report.actions.append(ExportAction(rel, "write", redacted))
    return report
