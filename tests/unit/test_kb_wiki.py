"""Wiki jail + front-matter + write_page: the containment contract (safety prereq #2).

Written before any tool wiring. The jail and front-matter helpers are pure; write_page
is exercised against a real store + FakeEmbedder in a temp wiki dir."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from jarvis.config import KnowledgeConfig
from jarvis.knowledge.service import KnowledgeError, KnowledgeService
from jarvis.knowledge.store import KnowledgeStore
from jarvis.knowledge.wiki import (
    WikiPathError,
    build_front_matter,
    safe_wiki_path,
    split_front_matter,
)
from jarvis.memory.embeddings import FakeEmbedder
from jarvis.persistence.db import connect

FIXED = dt.datetime(2026, 7, 6, 12, 0, tzinfo=dt.UTC)
_OPEN_DBS: list = []


@pytest.fixture(autouse=True)
async def _close_dbs():
    yield
    while _OPEN_DBS:
        await _OPEN_DBS.pop().close()


# --- the jail (pure) -------------------------------------------------------


def test_jail_allows_nested_relative_md(tmp_path: Path) -> None:
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    target = safe_wiki_path(wiki, "topics/rust.md")
    assert target == (wiki / "topics" / "rust.md").resolve()


@pytest.mark.parametrize(
    "page",
    [
        "../escape.md",  # traversal
        "/etc/evil.md",  # absolute posix
        "C:\\Windows\\x.md",  # absolute drive
        "\\\\server\\share\\x.md",  # UNC
        "page.md:stream",  # NTFS ADS (also not .md-suffixed)
        "CON.md",  # reserved device name
        "nul.md",  # reserved device name
        "folder /page.md",  # component with a trailing space (Windows strips it)
        "notmarkdown.txt",  # wrong suffix
        "~/secret.md",  # home expansion
        "",  # empty
    ],
)
def test_jail_rejects_unsafe_pages(tmp_path: Path, page: str) -> None:
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    with pytest.raises(WikiPathError):
        safe_wiki_path(wiki, page)


def test_jail_rejects_trailing_dot_component(tmp_path: Path) -> None:
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    with pytest.raises(WikiPathError):
        safe_wiki_path(wiki, "bad./page.md")


def test_jail_rejects_symlink_escape(tmp_path: Path) -> None:
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        (wiki / "link").symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not available on this platform/user")
    with pytest.raises(WikiPathError):
        safe_wiki_path(wiki, "link/escape.md")


# --- front-matter merge (pure) ---------------------------------------------


def test_front_matter_preserves_unknown_keys_and_stable_id() -> None:
    existing = {
        "id": "stable-slug",
        "title": "Old Title",
        "created": "2026-01-01",
        "cssclass": "wide",  # an Obsidian/plugin key Jarvis doesn't know
        "tags": ["rust", "async"],
        "aliases": ["async rust"],
    }
    merged = build_front_matter(
        existing,
        title="New Title",
        source_ids=[1, 2],
        created_by="agent",
        now="2026-07-06",
        slug_seed="rust",
    )
    assert merged["id"] == "stable-slug"  # never regenerated
    assert merged["created"] == "2026-01-01"  # preserved
    assert merged["updated"] == "2026-07-06"  # stamped
    assert merged["title"] == "New Title"
    assert merged["source_ids"] == [1, 2]
    assert merged["cssclass"] == "wide"  # unknown key survives
    assert merged["tags"] == ["rust", "async"]


def test_front_matter_generates_id_when_absent() -> None:
    merged = build_front_matter(
        {}, title="X", source_ids=[], created_by="user", now="2026-07-06", slug_seed="My Page"
    )
    assert merged["id"] == "my-page"
    assert merged["created"] == merged["updated"] == "2026-07-06"


def test_split_front_matter_rejects_non_mapping() -> None:
    fm, body = split_front_matter("---\n- just\n- a list\n---\nbody")
    assert fm == {} and "body" in body  # a non-mapping block is treated as content


# --- write_page (service) --------------------------------------------------


async def _service(tmp_path: Path) -> KnowledgeService:
    store = KnowledgeStore(await connect(tmp_path / "kb.db"))
    _OPEN_DBS.append(store.db)
    svc = KnowledgeService(
        store,
        FakeEmbedder(),
        KnowledgeConfig(),
        knowledge_dir=tmp_path / "knowledge",
        root=tmp_path,
        now=lambda: FIXED,
    )
    svc.ensure_dirs()
    return svc


async def _a_reviewed_source(svc: KnowledgeService, origin="s.txt") -> int:
    return await svc.store.add_source(
        kind="file",
        origin=origin,
        title="S",
        content_hash=origin,
        raw_path=f"raw/{origin}",
        markdown_path=f"markdown/{origin}.md",
        markdown_hash="h",
        converter="passthrough",
        converter_version="1",
        byte_size=10,
        created_by="user",
    )


async def test_write_page_creates_page_with_generated_front_matter(tmp_path: Path) -> None:
    svc = await _service(tmp_path)
    sid = await _a_reviewed_source(svc)
    path = await svc.write_page(
        "topics/rust.md", "# Rust Async\n\nTokio is a runtime.", source_ids=[sid]
    )
    assert path.exists()
    fm, body = split_front_matter(path.read_text(encoding="utf-8"))
    assert fm["title"] == "Rust Async"  # derived from the H1
    assert fm["source_ids"] == [sid]
    assert fm["created"] == "2026-07-06" and fm["created_by"] == "agent"
    assert "Tokio is a runtime." in body
    # chunks were indexed for the page
    assert "topics/rust.md" in await svc.store.wiki_paths_with_chunks()


async def test_write_page_drops_content_front_matter(tmp_path: Path) -> None:
    svc = await _service(tmp_path)
    # the model tries to forge provenance inside the content body
    await svc.write_page(
        "p.md", "---\nid: forged\nsource_ids: [999]\n---\n# Real\n\nbody", source_ids=[]
    )
    fm, body = split_front_matter((svc.wiki_dir / "p.md").read_text(encoding="utf-8"))
    assert fm["id"] != "forged"  # forged front-matter dropped; id generated by Jarvis
    assert fm["source_ids"] == []  # not the forged [999]
    assert "forged" not in body


async def test_write_page_obsidian_roundtrip_preserves_edits(tmp_path: Path) -> None:
    svc = await _service(tmp_path)
    path = await svc.write_page("p.md", "# Page\n\nv1 body")
    # simulate a human editing the vault in Obsidian: add a plugin key + tags
    text = path.read_text(encoding="utf-8")
    text = text.replace("created_by: agent", "created_by: agent\ncssclass: wide\ntags: [a, b]")
    path.write_text(text, encoding="utf-8")
    original_id = split_front_matter(path.read_text(encoding="utf-8"))[0]["id"]

    # Jarvis rewrites the page — the human's keys must survive
    await svc.write_page("p.md", "# Page\n\nv2 body")
    fm, body = split_front_matter(path.read_text(encoding="utf-8"))
    assert fm["cssclass"] == "wide"  # unknown key preserved
    assert fm["tags"] == ["a", "b"]
    assert fm["id"] == original_id  # stable id not regenerated
    assert "v2 body" in body


async def test_write_page_indexes_links(tmp_path: Path) -> None:
    svc = await _service(tmp_path)
    await svc.write_page("tokio.md", "# Tokio\n\nA runtime.")
    await svc.write_page(
        "rust.md", "# Rust\n\nSee [[Tokio]] and [missing](ghost.md).", source_ids=[]
    )
    links = await svc.store.links_from("rust.md")
    resolved = {ln.to_raw: ln.to_path for ln in links}
    assert resolved["Tokio"] == "tokio.md"  # wikilink resolved
    assert resolved["ghost.md"] is None  # broken link recorded, not dropped
    assert await svc.store.backlinks("tokio.md") == ["rust.md"]


async def test_write_page_reindex_replaces_not_appends(tmp_path: Path) -> None:
    svc = await _service(tmp_path)
    await svc.write_page("a.md", "# A\n\nlinks [[B]]")
    await svc.write_page("a.md", "# A\n\nno links now")
    assert await svc.store.links_from("a.md") == []  # old link replaced, not accumulated


async def test_write_page_rejects_unknown_source_id(tmp_path: Path) -> None:
    svc = await _service(tmp_path)
    with pytest.raises(KnowledgeError, match="does not exist"):
        await svc.write_page("p.md", "# P\n\nbody", source_ids=[999])


async def test_write_page_rejects_unreviewed_source(tmp_path: Path) -> None:
    svc = await _service(tmp_path)
    sid = await svc.store.add_source(
        kind="url",
        origin="https://x.test",
        title="U",
        content_hash="u",
        raw_path="raw/u",
        markdown_path="markdown/u.md",
        markdown_hash="h",
        converter="trafilatura",
        converter_version="1",
        byte_size=10,
        review_status="unreviewed",
        created_by="agent",
    )
    with pytest.raises(KnowledgeError, match="unreviewed"):
        await svc.write_page("p.md", "# P\n\nbody", source_ids=[sid])


async def test_write_page_jail_violation_raises(tmp_path: Path) -> None:
    svc = await _service(tmp_path)
    with pytest.raises(WikiPathError):
        await svc.write_page("../escape.md", "# X\n\nbody")
