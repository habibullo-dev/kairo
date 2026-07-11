"""KnowledgeStore tests: sources, chunk/link derived indexes, search filtering.

Keyless — hand-built unit vectors give deterministic cosine ordering. Connections
are closed by an autouse fixture (an unclosed aiosqlite connection hangs pytest)."""

from __future__ import annotations

from pathlib import Path

import aiosqlite
import pytest

from jarvis.knowledge.store import KnowledgeStore, NewChunk, WikiLink
from jarvis.persistence.db import connect
from jarvis.persistence.fts import integrity_check_all, query_domain
from jarvis.projects import ProjectStore

MODEL = "voyage-3-large"
_OPEN_DBS: list = []


@pytest.fixture(autouse=True)
async def _close_dbs():
    yield
    while _OPEN_DBS:
        await _OPEN_DBS.pop().close()


async def _store(tmp_path: Path) -> KnowledgeStore:
    store = KnowledgeStore(await connect(tmp_path / "kb.db"))
    _OPEN_DBS.append(store.db)
    return store


async def _add_source(store: KnowledgeStore, origin: str, **kw) -> int:
    return await store.add_source(
        kind=kw.get("kind", "file"),
        origin=origin,
        title=kw.get("title"),
        content_hash=kw.get("content_hash", origin),  # unique-per-origin default
        raw_path=kw.get("raw_path", f"raw/{origin}"),
        markdown_path=kw.get("markdown_path", f"markdown/{origin}.md"),
        markdown_hash=kw.get("markdown_hash", "md-" + origin),
        converter=kw.get("converter", "passthrough"),
        converter_version=kw.get("converter_version", "1.0"),
        byte_size=kw.get("byte_size", 100),
        review_status=kw.get("review_status", "reviewed"),
        created_by=kw.get("created_by", "user"),
        project_id=kw.get("project_id"),
    )


async def _chunk(store: KnowledgeStore, *, source_id=None, wiki_path=None, vec, model=MODEL):
    text = f"chunk of {source_id or wiki_path}"
    await store.replace_chunks(
        source_id=source_id,
        wiki_path=wiki_path,
        chunks=[NewChunk(heading_path="", seq=0, text=text, embedding=vec)],
        embedding_model=model,
    )


# --- sources: origin lifecycle, supersede lineage --------------------------


async def test_identical_bytes_can_have_distinct_logical_origins(tmp_path: Path) -> None:
    store = await _store(tmp_path)
    sid = await _add_source(store, "a.txt", content_hash="abc123")
    found = await store.find_by_hash("abc123")
    assert found is not None and found.id == sid
    assert await store.find_by_hash("nope") is None
    # A folder may contain duplicated boilerplate at different paths; its logical source identity
    # is the scoped origin, not the raw hash.
    other = await _add_source(store, "b.txt", content_hash="abc123")
    assert other != sid


async def test_supersede_lineage(tmp_path: Path) -> None:
    store = await _store(tmp_path)
    old = await _add_source(store, "doc.txt", content_hash="v1")
    new = await _add_source(store, "doc.txt", content_hash="v2")
    await _chunk(store, source_id=old, vec=[1.0, 0.0])
    await store.supersede_source(old, new)
    old_row = await store.get_source(old)
    assert old_row.status == "superseded" and old_row.superseded_by == new
    # The audit row remains, but the stale derived cache does not.
    assert await store.chunks_for_source(old) == []
    # the live-by-origin lookup now returns the new row
    live = await store.find_live_by_origin("doc.txt")
    assert live is not None and live.id == new


async def test_reject_and_review_status(tmp_path: Path) -> None:
    store = await _store(tmp_path)
    sid = await _add_source(store, "u.txt", review_status="unreviewed")
    await _chunk(store, source_id=sid, vec=[1.0, 0.0])
    await store.set_review_status(sid, "reviewed")
    assert (await store.get_source(sid)).review_status == "reviewed"
    assert await store.reject_source(sid) is True
    assert (await store.get_source(sid)).status == "rejected"
    assert await store.chunks_for_source(sid) == []
    assert await store.reject_source(sid) is False  # already rejected


async def test_detach_folder_purges_derived_chunks_and_fts_but_keeps_audit_sources(
    tmp_path: Path,
) -> None:
    store = await _store(tmp_path)
    project_id = await ProjectStore(store.db, store.lock).create(name="Imported folder")
    detached_source = await _add_source(
        store,
        f"chat-upload:{project_id}:wrong/readme.md",
        project_id=project_id,
    )
    retained_source = await _add_source(
        store,
        f"chat-upload:{project_id}:right/readme.md",
        project_id=project_id,
    )
    await store.replace_chunks(
        source_id=detached_source,
        chunks=[NewChunk("", 0, "folder-detach-cache-canary", [1.0, 0.0])],
        embedding_model=MODEL,
    )
    await store.replace_chunks(
        source_id=retained_source,
        chunks=[NewChunk("", 0, "retained-folder-canary", [0.0, 1.0])],
        embedding_model=MODEL,
    )
    assert len(
        await query_domain(
            store.db, "knowledge", "folder-detach-cache-canary", project_id=project_id,
            include_global=False,
        )
    ) == 1

    result = await store.reject_project_folder_import(project_id=project_id, root="wrong")

    assert result.sources_rejected == 1
    assert result.chunks_cleared == 1
    source = await store.get_source(detached_source)
    assert source is not None and source.status == "rejected"  # audit provenance persists
    assert await store.chunks_for_source(detached_source) == []
    assert len(await store.chunks_for_source(retained_source)) == 1
    assert await query_domain(
        store.db, "knowledge", "folder-detach-cache-canary", project_id=project_id,
        include_global=False,
    ) == []
    await integrity_check_all(store.db)
    await store.db.commit()  # integrity_check_all issues FTS maintenance statements directly.

    # A prior interrupted release could leave a rejected folder with derived rows.  Detach is
    # safe to retry: it repairs that cache without resurrecting or deleting the audit source.
    await store.replace_chunks(
        source_id=detached_source,
        chunks=[NewChunk("", 0, "retry-detach-cache-canary", [1.0, 0.0])],
        embedding_model=MODEL,
    )
    retry = await store.reject_project_folder_import(project_id=project_id, root="wrong")
    assert retry.sources_rejected == 0 and retry.chunks_cleared == 1
    assert await store.chunks_for_source(detached_source) == []


# --- chunk owner CHECK -----------------------------------------------------


async def test_chunk_exactly_one_owner_check(tmp_path: Path) -> None:
    store = await _store(tmp_path)
    now = "2026-01-01T00:00:00+00:00"
    # both owners set -> CHECK violation
    with pytest.raises(aiosqlite.IntegrityError):
        await store.db.execute(
            "INSERT INTO kb_chunks (source_id, wiki_path, heading_path, seq, text, embedding, "
            "embedding_model, created_at) VALUES (1, 'p.md', '', 0, 't', x'00', ?, ?)",
            (MODEL, now),
        )
    # neither owner set -> CHECK violation
    with pytest.raises(aiosqlite.IntegrityError):
        await store.db.execute(
            "INSERT INTO kb_chunks (source_id, wiki_path, heading_path, seq, text, embedding, "
            "embedding_model, created_at) VALUES (NULL, NULL, '', 0, 't', x'00', ?, ?)",
            (MODEL, now),
        )


# --- search: filtering + cosine ordering -----------------------------------


async def test_search_excludes_and_orders(tmp_path: Path) -> None:
    store = await _store(tmp_path)
    q = [1.0, 0.0, 0.0, 0.0]

    live = await _add_source(store, "live.txt")
    await _chunk(store, source_id=live, vec=[1.0, 0.0, 0.0, 0.0])  # best match

    sup_old = await _add_source(store, "sup.txt", content_hash="so")
    sup_new = await _add_source(store, "sup.txt", content_hash="sn")
    await _chunk(store, source_id=sup_old, vec=[1.0, 0.0, 0.0, 0.0])
    await store.supersede_source(sup_old, sup_new)  # its chunk must vanish from search

    rej = await _add_source(store, "rej.txt")
    await _chunk(store, source_id=rej, vec=[1.0, 0.0, 0.0, 0.0])
    await store.reject_source(rej)

    unrev = await _add_source(store, "unrev.txt", review_status="unreviewed")
    await _chunk(store, source_id=unrev, vec=[1.0, 0.0, 0.0, 0.0])

    foreign = await _add_source(store, "foreign.txt")
    await _chunk(store, source_id=foreign, vec=[1.0, 0.0, 0.0, 0.0], model="other-model")

    await _chunk(store, wiki_path="page.md", vec=[0.8, 0.6, 0.0, 0.0])  # curated, always eligible

    hits = await store.search(q, MODEL, top_k=10, min_similarity=0.0)
    owners = {(h.chunk.source_id, h.chunk.wiki_path) for h in hits}
    assert (live, None) in owners
    assert (None, "page.md") in owners
    # excluded despite identical (perfect) similarity:
    assert (sup_old, None) not in owners  # superseded source
    assert (rej, None) not in owners  # rejected source
    assert (unrev, None) not in owners  # unreviewed (quarantined) by default
    assert (foreign, None) not in owners  # different embedding model
    # ordering: the exact-match live chunk outranks the off-axis wiki chunk
    assert hits[0].chunk.source_id == live
    assert hits[0].score > hits[1].score


async def test_search_include_unreviewed(tmp_path: Path) -> None:
    store = await _store(tmp_path)
    unrev = await _add_source(store, "u.txt", review_status="unreviewed")
    await _chunk(store, source_id=unrev, vec=[1.0, 0.0, 0.0, 0.0])
    assert await store.search([1.0, 0.0, 0.0, 0.0], MODEL, top_k=5, min_similarity=0.0) == []
    hits = await store.search(
        [1.0, 0.0, 0.0, 0.0], MODEL, top_k=5, min_similarity=0.0, include_unreviewed=True
    )
    assert len(hits) == 1 and hits[0].chunk.source_id == unrev


async def test_search_carries_citation_context(tmp_path: Path) -> None:
    store = await _store(tmp_path)
    sid = await _add_source(store, "paper.txt", kind="file", title="A Paper", created_by="agent")
    await _chunk(store, source_id=sid, vec=[1.0, 0.0, 0.0, 0.0])
    (hit,) = await store.search([1.0, 0.0, 0.0, 0.0], MODEL, top_k=5, min_similarity=0.0)
    assert hit.source_kind == "file"
    assert hit.source_origin == "paper.txt"
    assert hit.source_title == "A Paper"
    assert hit.source_created_by == "agent"


# --- replace_chunks / replace_links: atomicity + backlinks -----------------


async def test_replace_chunks_replaces_per_owner(tmp_path: Path) -> None:
    store = await _store(tmp_path)
    sid = await _add_source(store, "a.txt")
    await store.replace_chunks(
        source_id=sid,
        chunks=[NewChunk("", 0, "one", [1, 0]), NewChunk("", 1, "two", [0, 1])],
        embedding_model=MODEL,
    )
    assert len(await store.chunks_for_source(sid)) == 2
    # a re-index replaces, never appends
    await store.replace_chunks(
        source_id=sid, chunks=[NewChunk("", 0, "only", [1, 0])], embedding_model=MODEL
    )
    remaining = await store.chunks_for_source(sid)
    assert len(remaining) == 1 and remaining[0].text == "only"


async def test_replace_chunks_atomic_on_failure(tmp_path: Path, monkeypatch) -> None:
    store = await _store(tmp_path)
    sid = await _add_source(store, "a.txt")
    await store.replace_chunks(
        source_id=sid, chunks=[NewChunk("", 0, "original", [1, 0])], embedding_model=MODEL
    )

    async def _boom(*_a, **_kw):
        raise RuntimeError("insert failed mid-replace")

    monkeypatch.setattr(store.db, "executemany", _boom)
    with pytest.raises(RuntimeError):
        await store.replace_chunks(
            source_id=sid, chunks=[NewChunk("", 0, "new", [0, 1])], embedding_model=MODEL
        )
    # the DELETE was rolled back with the failed INSERT — original chunk intact
    remaining = await store.chunks_for_source(sid)
    assert len(remaining) == 1 and remaining[0].text == "original"


async def test_replace_links_and_backlinks(tmp_path: Path) -> None:
    store = await _store(tmp_path)
    await store.replace_links(
        "a.md",
        [
            WikiLink("a.md", "b.md", "b.md", "B", "markdown"),
            WikiLink("a.md", None, "Nonexistent", "Nonexistent", "wikilink"),
        ],
    )
    await store.replace_links("c.md", [WikiLink("c.md", "b.md", "B Page", "B", "wikilink")])
    assert sorted(await store.backlinks("b.md")) == ["a.md", "c.md"]
    # broken link (to_path NULL) is recorded, not dropped
    a_links = await store.links_from("a.md")
    assert any(ln.to_path is None and ln.to_raw == "Nonexistent" for ln in a_links)


async def test_replace_links_atomic_on_failure(tmp_path: Path, monkeypatch) -> None:
    store = await _store(tmp_path)
    await store.replace_links("a.md", [WikiLink("a.md", "b.md", "b.md", None, "markdown")])

    async def _boom(*_a, **_kw):
        raise RuntimeError("insert failed")

    monkeypatch.setattr(store.db, "executemany", _boom)
    with pytest.raises(RuntimeError):
        await store.replace_links("a.md", [WikiLink("a.md", "z.md", "z.md", None, "markdown")])
    # original link survives the rolled-back replace
    assert await store.backlinks("b.md") == ["a.md"]
