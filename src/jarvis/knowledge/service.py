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
import hashlib
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from jarvis.knowledge import converters
from jarvis.knowledge import links as _links
from jarvis.knowledge.chunking import chunk_markdown, embed_text
from jarvis.knowledge.store import KnowledgeStore, NewChunk, Source, WikiLink
from jarvis.knowledge.wiki import (
    build_front_matter,
    render_page,
    safe_wiki_path,
    slugify,
    split_front_matter,
)
from jarvis.observability import get_logger
from jarvis.paths import is_sensitive_path, resolve_path

_H1 = re.compile(r"^\s*#\s+(.+?)\s*#*\s*$", re.MULTILINE)


def utc_now() -> _dt.datetime:
    return _dt.datetime.now(_dt.UTC)


class KnowledgeError(Exception):
    """A knowledge operation the service refuses; the message is written for the model."""


@dataclass(frozen=True)
class IngestResult:
    """Outcome of an ingest, for the tool/REPL to report."""

    action: str  # 'ingested' | 'duplicate' | 'superseded'
    source_id: int
    chunks: int
    review_status: str  # 'reviewed' | 'unreviewed'
    title: str | None = None


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

    # --- ingest ------------------------------------------------------------

    async def ingest(
        self,
        *,
        path: str | None = None,
        url: str | None = None,
        text: str | None = None,
        title: str | None = None,
        created_by: str = "user",
        source_session_id: int | None = None,
    ) -> IngestResult:
        """Ingest exactly one of a file ``path``, a ``url``, or freeform ``text`` into
        an immutable raw artifact + deterministic markdown + a chunk index.

        Order matters for crash-consistency: the raw artifact is written *before* the
        DB row, so a crash leaves a harmless orphan file (swept by ``kb rebuild``),
        never a row pointing at nothing. An unattended run (``bound_unattended``)
        stages the source ``unreviewed`` — quarantined from search until a human runs
        ``kb review`` (ADR-0004)."""
        given = [("path", path), ("url", url), ("text", text)]
        provided = [name for name, value in given if value is not None]
        if len(provided) != 1:
            raise KnowledgeError(
                f"ingest needs exactly one of path / url / text (got {provided or 'none'})"
            )
        self.ensure_dirs()

        if path is not None:
            kind, origin, raw_bytes, ext, seed = await self._read_file_source(path)
        elif url is not None:
            kind, origin, raw_bytes, ext, seed = await self._read_url_source(url)
        else:
            kind, origin = "note", "note"
            raw_bytes, ext, seed = text.encode("utf-8"), ".md", (title or "note")

        content_hash = hashlib.sha256(raw_bytes).hexdigest()
        existing = await self.store.find_by_hash(content_hash)
        if existing is not None:  # content_hash is UNIQUE — identical bytes are a no-op
            return IngestResult("duplicate", existing.id, 0, existing.review_status, existing.title)

        # raw artifact FIRST (orphan-file-not-dangling-row on crash)
        stem = f"{content_hash[:16]}-{slugify(seed or origin)[:60]}"
        raw_rel = f"raw/{stem}{ext}"
        (self.knowledge_dir / raw_rel).write_bytes(raw_bytes)

        conversion = await self._convert(kind, path, raw_bytes)
        markdown_rel = f"markdown/{content_hash[:16]}.md"
        (self.knowledge_dir / markdown_rel).write_text(conversion.markdown, encoding="utf-8")

        review_status = "unreviewed" if self.bound_unattended else "reviewed"
        # A changed file/url supersedes its prior live version — but only when the new
        # source is itself reviewed (an unattended re-ingest must not silently replace
        # trusted content; it stages for review instead).
        prior: Source | None = None
        if kind in ("file", "url") and review_status == "reviewed":
            prior = await self.store.find_live_by_origin(origin)

        source_id = await self.store.add_source(
            kind=kind,
            origin=origin,
            title=title or conversion.title,
            content_hash=content_hash,
            raw_path=raw_rel,
            markdown_path=markdown_rel,
            markdown_hash=hashlib.sha256(conversion.markdown.encode("utf-8")).hexdigest(),
            converter=conversion.converter,
            converter_version=conversion.converter_version,
            byte_size=len(raw_bytes),
            review_status=review_status,
            created_by=created_by,
            source_session_id=source_session_id,
        )
        if prior is not None:
            await self.store.supersede_source(prior.id, source_id)

        new_chunks = await self._chunk_and_embed(conversion.markdown)
        await self.store.replace_chunks(
            source_id=source_id, chunks=new_chunks, embedding_model=self.embedder.model
        )
        self.log.info(
            "kb_ingested",
            source_id=source_id,
            kind=kind,
            origin=origin,
            content_hash=content_hash[:16],
            converter=conversion.converter,
            chunks=len(new_chunks),
            review_status=review_status,
            superseded=prior.id if prior else None,
        )
        action = "superseded" if prior is not None else "ingested"
        return IngestResult(
            action, source_id, len(new_chunks), review_status, title or conversion.title
        )

    async def _read_file_source(self, path: str) -> tuple[str, str, bytes, str, str]:
        """Resolve + validate a file path (defense-in-depth floor), return its bytes."""
        resolved = resolve_path(path, self.root)
        if is_sensitive_path(resolved):
            raise KnowledgeError(f"refusing to ingest a sensitive path: {resolved}")
        if not resolved.is_file():
            raise KnowledgeError(f"not a file: {resolved}")
        size = resolved.stat().st_size
        if size > self.config.max_ingest_bytes:
            raise KnowledgeError(
                f"file is {size:,} bytes, over the {self.config.max_ingest_bytes:,}-byte cap"
            )
        ext = resolved.suffix or ".bin"
        return "file", str(resolved), resolved.read_bytes(), ext, resolved.stem

    async def _read_url_source(self, url: str) -> tuple[str, str, bytes, str, str]:
        """Fetch a URL (SSRF-guarded) and return its raw bytes."""
        try:
            raw_bytes, ctype = await converters.fetch_url(
                url, timeout_seconds=self.config.convert_timeout_seconds
            )
        except converters.ConversionError as exc:
            raise KnowledgeError(str(exc)) from exc
        if len(raw_bytes) > self.config.max_ingest_bytes:
            raise KnowledgeError(
                f"page is {len(raw_bytes):,} bytes, over the "
                f"{self.config.max_ingest_bytes:,}-byte cap"
            )
        ext = ".html" if ctype and "html" in ctype else ".txt"
        return "url", url, raw_bytes, ext, urlparse(url).netloc or "web"

    async def _convert(self, kind: str, path: str | None, raw_bytes: bytes):
        """Convert a source's bytes to markdown: files through the killable subprocess
        sandbox; urls through the (in-process) trafilatura/markitdown web path; notes
        pass through."""
        if kind == "file":
            resolved = resolve_path(path, self.root)
            try:
                return await converters.convert_file_sandboxed(
                    resolved,
                    max_bytes=self.config.max_ingest_bytes,
                    pdf_converter=self.config.pdf_converter,
                    timeout_seconds=self.config.convert_timeout_seconds,
                )
            except converters.ConversionError as exc:
                raise KnowledgeError(str(exc)) from exc
        if kind == "url":
            return converters.html_to_markdown(raw_bytes.decode("utf-8", errors="replace"))
        # note: the text is already markdown/plaintext
        return converters.ConversionResult(
            converters.strip_front_matter(raw_bytes.decode("utf-8", errors="replace")),
            None,
            "passthrough",
            "1",
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

        new_chunks = await self._chunk_and_embed(body)
        await self.store.replace_chunks(
            wiki_path=wiki_path, chunks=new_chunks, embedding_model=self.embedder.model
        )

    async def _chunk_and_embed(self, markdown: str) -> list[NewChunk]:
        """Chunk markdown and embed each chunk (with its heading path prefixed).
        Shared by wiki-page reindex and source ingest."""
        chunks = chunk_markdown(
            markdown, max_chars=self.config.chunk_chars, min_chars=self.config.min_chunk_chars
        )
        if not chunks:
            return []
        vectors = await self.embedder.embed_documents([embed_text(c) for c in chunks])
        return [
            NewChunk(heading_path=c.heading_path, seq=c.seq, text=c.text, embedding=v)
            for c, v in zip(chunks, vectors, strict=True)
        ]

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
