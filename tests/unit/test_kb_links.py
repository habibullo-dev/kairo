"""Link extraction + resolution: pure, fence-aware, Obsidian-style. No fixtures."""

from __future__ import annotations

from jarvis.knowledge.links import PageRef, RawLink, extract_links, resolve_target


def _kinds(md: str) -> list[tuple[str, str]]:
    return [(link.link_kind, link.to_raw) for link in extract_links(md)]


def test_extracts_wikilinks_and_markdown_links() -> None:
    md = "See [[Rust Async]] and [Tokio](topics/tokio.md) for details.\n"
    assert ("wikilink", "Rust Async") in _kinds(md)
    assert ("markdown", "topics/tokio.md") in _kinds(md)


def test_wikilink_alias_and_anchor() -> None:
    (link,) = extract_links("[[Rust Async|the async page]]\n")
    assert link.to_raw == "Rust Async" and link.link_text == "the async page"
    (anchored,) = extract_links("[[Rust Async#Runtimes]]\n")
    assert anchored.to_raw == "Rust Async"  # anchor stripped for resolution


def test_ignores_external_images_and_anchors() -> None:
    md = (
        "[ext](https://example.com) [mail](mailto:a@b.c) [anchor](#section) "
        "![img](pic.png) [ok](page.md)\n"
    )
    kinds = _kinds(md)
    assert ("markdown", "page.md") in kinds
    assert all(raw not in {"https://example.com", "mailto:a@b.c", "pic.png"} for _, raw in kinds)
    assert not any(raw.startswith("#") for _, raw in kinds)


def test_fence_and_inline_code_ignored() -> None:
    md = "real [[Live]] link\n\n```\n[[NotALink]] and [x](y.md)\n```\n\n`[[AlsoNot]]` inline\n"
    kinds = _kinds(md)
    assert ("wikilink", "Live") in kinds
    assert ("wikilink", "NotALink") not in kinds
    assert ("wikilink", "AlsoNot") not in kinds


# --- resolution ------------------------------------------------------------

PAGES = [
    PageRef(path="topics/rust.md", stem="rust", title="Rust Async", aliases=("async rust",)),
    PageRef(path="topics/tokio.md", stem="tokio", title="Tokio", aliases=()),
    PageRef(path="index.md", stem="index", title="Index", aliases=()),
]


def _wiki(raw: str, frm: str = "index.md") -> str | None:
    return resolve_target(RawLink(raw, None, "wikilink"), frm, PAGES)


def _md(raw: str, frm: str) -> str | None:
    return resolve_target(RawLink(raw, None, "markdown"), frm, PAGES)


def test_wikilink_resolves_by_stem_title_alias() -> None:
    assert _wiki("tokio") == "topics/tokio.md"  # by stem
    assert _wiki("Rust Async") == "topics/rust.md"  # by title
    assert _wiki("async rust") == "topics/rust.md"  # by alias
    assert _wiki("nonexistent") is None


def test_markdown_link_resolves_relative_to_page() -> None:
    assert _md("tokio.md", "topics/rust.md") == "topics/tokio.md"  # within topics/
    assert _md("../index.md", "topics/rust.md") == "index.md"  # climbs out
    assert _md("ghost.md", "topics/rust.md") is None  # unresolved (broken)


def test_ambiguous_wikilink_picks_shortest_path() -> None:
    pages = [
        PageRef(path="a/dup.md", stem="dup", title=None),
        PageRef(path="dup.md", stem="dup", title=None),
    ]
    assert resolve_target(RawLink("dup", None, "wikilink"), "x.md", pages) == "dup.md"
