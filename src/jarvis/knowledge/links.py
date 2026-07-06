"""Wiki link extraction and resolution — pure, fence-aware, Obsidian-style.

Parses both standard Markdown links (``[text](page.md)``) and Obsidian wikilinks
(``[[Page]]`` / ``[[Page|alias]]``) so the derived link index (``kb_wiki_links``)
powers broken-link/orphan lint and backlinks for either style. External URLs,
anchors, and images are ignored — the index is about page-to-page structure.

Resolution mirrors Obsidian: a wikilink matches a page by filename stem, front-matter
``title``, or an ``alias`` (case-insensitive); a Markdown link resolves relative to
the linking page's directory. An unresolved target is recorded with ``to_path=None``
(a broken link the linter surfaces), never dropped.
"""

from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass
from pathlib import PurePosixPath

_FENCE = re.compile(r"^\s*(```|~~~)")
_INLINE_CODE = re.compile(r"`[^`]*`")
_MD_LINK = re.compile(r"(!?)\[([^\]]*)\]\(\s*([^)\s]+)(?:\s+\"[^\"]*\")?\s*\)")
_WIKILINK = re.compile(r"\[\[([^\]|#]+)(?:#[^\]|]*)?(?:\|([^\]]+))?\]\]")
_EXTERNAL = re.compile(r"^[a-z][a-z0-9+.-]*://|^mailto:|^tel:", re.IGNORECASE)


@dataclass(frozen=True)
class RawLink:
    """A link as written, before resolution to a page path."""

    to_raw: str
    link_text: str | None
    link_kind: str  # 'markdown' | 'wikilink'


@dataclass(frozen=True)
class PageRef:
    """A known wiki page's identity, for resolving links against it."""

    path: str  # wiki-relative posix path, e.g. 'topics/rust.md'
    stem: str  # filename without .md
    title: str | None
    aliases: tuple[str, ...] = ()


def extract_links(markdown: str) -> list[RawLink]:
    """Extract internal wiki/markdown links from a page body. Fence-aware (links
    inside code fences and inline code spans are ignored)."""
    links: list[RawLink] = []
    in_fence = False
    for line in markdown.splitlines():
        if _FENCE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        text = _INLINE_CODE.sub("", line)
        for match in _WIKILINK.finditer(text):
            target = match.group(1).strip()
            alias = (match.group(2) or "").strip() or None
            if target:
                links.append(RawLink(to_raw=target, link_text=alias, link_kind="wikilink"))
        for match in _MD_LINK.finditer(text):
            if match.group(1) == "!":  # image, not a page link
                continue
            target = match.group(3).strip()
            if not target or target.startswith("#") or _EXTERNAL.match(target):
                continue
            target = target.split("#", 1)[0]  # drop any '#anchor'
            if target:
                links.append(
                    RawLink(
                        to_raw=target,
                        link_text=match.group(2).strip() or None,
                        link_kind="markdown",
                    )
                )
    return links


def resolve_candidates(link: RawLink, from_path: str, pages: list[PageRef]) -> list[str]:
    """All page paths a link could resolve to (0 = broken, >1 = ambiguous wikilink)."""
    if link.link_kind == "markdown":
        target = link.to_raw if link.to_raw.lower().endswith(".md") else f"{link.to_raw}.md"
        candidate = posixpath.normpath((PurePosixPath(from_path).parent / target).as_posix())
        return [candidate] if candidate in {p.path for p in pages} else []
    needle = link.to_raw.strip().lower()
    return [
        p.path
        for p in pages
        if needle in {p.stem.lower(), (p.title or "").lower(), *(a.lower() for a in p.aliases)}
    ]


def resolve_target(link: RawLink, from_path: str, pages: list[PageRef]) -> str | None:
    """Resolve a link's target to a known page path, or ``None`` if unresolved.

    Markdown links resolve relative to ``from_path``'s directory; wikilinks match by
    stem / title / alias. Ambiguous wikilinks resolve to the shortest path (and the
    linter flags the ambiguity via :func:`resolve_candidates`)."""
    candidates = resolve_candidates(link, from_path, pages)
    if not candidates:
        return None
    return min(candidates, key=lambda path: (len(path), path))
