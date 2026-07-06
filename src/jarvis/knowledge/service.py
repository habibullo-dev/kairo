"""KnowledgeService: the semantics layer over the store, converters, and chunker.

This task (Milestone 4 §6) implements **wiki page writing** — the jailed, provenance-
generating, link-and-chunk-reindexing path — plus the helpers ingest/query/lint build
on later. The safety-critical pieces it composes (the path jail, the front-matter
merge, SSRF-guarded conversion) live in their own pure modules and are tested there.

Two invariants write_page upholds:

* **Provenance is never content-derived.** Front-matter in the model-supplied
  ``content`` is dropped; the stored front-matter is generated from database state
  (validated ``source_ids``) and the on-disk page's own preserved keys.
* **The vault stays human-first.** An Obsidian edit survives a Jarvis rewrite
  (unknown front-matter keys preserved; a stable ``id`` never regenerated), and the
  chunk/link indexes are rebuilt from the page body, never the other way round.
"""

from __future__ import annotations

import datetime as _dt
import re
from collections.abc import Callable
from pathlib import Path

from jarvis.knowledge import links as _links
from jarvis.knowledge.chunking import chunk_markdown, embed_text
from jarvis.knowledge.store import KnowledgeStore, NewChunk, WikiLink
from jarvis.knowledge.wiki import (
    build_front_matter,
    render_page,
    safe_wiki_path,
    split_front_matter,
)
from jarvis.observability import get_logger

_H1 = re.compile(r"^\s*#\s+(.+?)\s*#*\s*$", re.MULTILINE)


def utc_now() -> _dt.datetime:
    return _dt.datetime.now(_dt.UTC)


class KnowledgeError(Exception):
    """A knowledge operation the service refuses; the message is written for the model."""


class KnowledgeService:
    def __init__(
        self,
        store: KnowledgeStore,
        embedder,
        config,
        *,
        knowledge_dir: Path,
        root: Path,
        now: Callable[[], _dt.datetime] = utc_now,
    ) -> None:
        self.store = store
        self.embedder = embedder
        self.config = config  # KnowledgeConfig
        self.root = root  # workspace root, for resolving ingest file paths (Task 7)
        self.knowledge_dir = knowledge_dir
        self.wiki_dir = knowledge_dir / "wiki"
        self.raw_dir = knowledge_dir / "raw"
        self.markdown_dir = knowledge_dir / "markdown"
        self.now = now
        # Set True by the JobRunner around an unattended run (Task 7/10): ingests are
        # quarantined 'unreviewed'. A plain attribute is safe — runs are serialized
        # by the turn lock (mirrors TaskService.bound_session_id).
        self.bound_unattended = False
        self.log = get_logger("jarvis.knowledge")

    def ensure_dirs(self) -> None:
        for d in (self.wiki_dir, self.raw_dir, self.markdown_dir):
            d.mkdir(parents=True, exist_ok=True)

    # --- wiki pages --------------------------------------------------------

    async def write_page(
        self,
        page: str,
        content: str,
        *,
        source_ids: list[int] | None = None,
        created_by: str = "agent",
    ) -> Path:
        """Write (or rewrite) a wiki page, jailed to the wiki dir, with Jarvis-owned
        front-matter and a rebuilt link + chunk index. Raises :class:`KnowledgeError`
        (jail violation or unknown/unreviewed source id) with a model-readable message."""
        target = safe_wiki_path(self.wiki_dir, page)  # containment first
        ids = source_ids or []
        await self._validate_source_ids(ids)

        # Drop any front-matter the model put in `content` — provenance is DB-derived,
        # never carried up from content. Only the body survives.
        _, body = split_front_matter(content)

        existing_fm: dict = {}
        if target.exists():
            existing_fm, _ = split_front_matter(target.read_text(encoding="utf-8"))

        today = self.now().strftime("%Y-%m-%d")
        stem = target.stem
        title = existing_fm.get("title") or _first_heading(body) or stem.replace("-", " ")
        front_matter = build_front_matter(
            existing_fm,
            title=title,
            source_ids=ids,
            created_by=created_by,
            now=today,
            slug_seed=stem,
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(render_page(front_matter, body), encoding="utf-8")

        wiki_path = target.relative_to(self.wiki_dir.resolve()).as_posix()
        await self._reindex_page(wiki_path, body)
        self.log.info("kb_page_written", page=wiki_path, source_ids=ids, created_by=created_by)
        return target

    async def _validate_source_ids(self, source_ids: list[int]) -> None:
        """Every cited source must exist, be live, and be reviewed — a page must not
        claim provenance from an unknown, superseded, or unreviewed source."""
        for sid in source_ids:
            source = await self.store.get_source(sid)
            if source is None:
                raise KnowledgeError(f"source #{sid} does not exist")
            if source.status != "live":
                raise KnowledgeError(f"source #{sid} is {source.status}, not live — cannot cite it")
            if source.review_status != "reviewed":
                raise KnowledgeError(
                    f"source #{sid} is unreviewed (run `kb review` to approve it) — cannot cite it"
                )

    async def _reindex_page(self, wiki_path: str, body: str) -> None:
        """Rebuild this page's chunk index and outbound link index from its body."""
        pages = self._page_index()
        raw_links = _links.extract_links(body)
        resolved = [
            WikiLink(
                from_path=wiki_path,
                to_path=_links.resolve_target(link, wiki_path, pages),
                to_raw=link.to_raw,
                link_text=link.link_text,
                link_kind=link.link_kind,
            )
            for link in raw_links
        ]
        await self.store.replace_links(wiki_path, resolved)

        chunks = chunk_markdown(
            body, max_chars=self.config.chunk_chars, min_chars=self.config.min_chunk_chars
        )
        if chunks:
            vectors = await self.embedder.embed_documents([embed_text(c) for c in chunks])
            new_chunks = [
                NewChunk(heading_path=c.heading_path, seq=c.seq, text=c.text, embedding=v)
                for c, v in zip(chunks, vectors, strict=True)
            ]
        else:
            new_chunks = []
        await self.store.replace_chunks(
            wiki_path=wiki_path, chunks=new_chunks, embedding_model=self.embedder.model
        )

    def _page_index(self) -> list[_links.PageRef]:
        """Scan the wiki dir into PageRefs (path/stem/title/aliases) for link resolution."""
        refs: list[_links.PageRef] = []
        if not self.wiki_dir.exists():
            return refs
        wiki_root = self.wiki_dir.resolve()
        for md_file in sorted(wiki_root.rglob("*.md")):
            fm, _ = split_front_matter(md_file.read_text(encoding="utf-8", errors="replace"))
            refs.append(
                _links.PageRef(
                    path=md_file.relative_to(wiki_root).as_posix(),
                    stem=md_file.stem,
                    title=fm.get("title") if isinstance(fm.get("title"), str) else None,
                    aliases=_as_str_tuple(fm.get("aliases")),
                )
            )
        return refs


def _first_heading(body: str) -> str | None:
    match = _H1.search(body)
    return match.group(1).strip() if match else None


def _as_str_tuple(value) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value,)
    if isinstance(value, list):
        return tuple(str(v) for v in value)
    return ()
