"""Obsidian export invariants (Phase 15 Task 10): deterministic (byte-identical re-export),
non-destructive (marker-guarded — never clobber a user/unmarked file), namespace-contained
(_graph/ + _memory/ only), private-excluded, secret-redacted. Keyless: a temp DB + temp vault."""

from __future__ import annotations

import asyncio
from pathlib import Path

import numpy as np
import pytest

from kira.graph import GraphStore
from kira.graph.obsidian import MARKER, export
from kira.memory import MemoryStore, Provenance
from kira.persistence.db import connect
from kira.projects import ProjectStore

_OPEN: list = []
_EMB = np.array([0.1, 0.2, 0.3], dtype=np.float32)


@pytest.fixture(autouse=True)
async def _close():
    yield
    while _OPEN:
        await _OPEN.pop().close()


async def _stores(tmp_path: Path):
    db = await connect(tmp_path / "g.db")
    _OPEN.append(db)
    lock = asyncio.Lock()
    await ProjectStore(db, lock).create(name="P")  # project 1
    return GraphStore(db, lock), MemoryStore(db, lock)


async def _node(store: GraphStore, kind: str, title: str, **kw) -> int:
    return await store.add_node(
        kind=kind, title=title, summary=f"about {title}", trust_class="reviewed",
        created_by="user", project_id=1, **kw)


async def _mem(mem: MemoryStore, content: str) -> int:
    return await mem.add(
        type="fact", content=content, embedding=_EMB, embedding_model="m", source="user",
        provenance=Provenance(confidence=0.9), project_id=1)


# --- determinism -----------------------------------------------------------
async def test_export_is_deterministic_and_idempotent(tmp_path: Path) -> None:
    store, mem = await _stores(tmp_path)
    await _node(store, "person", "Ada Lovelace")
    await _mem(mem, "Ada designed the first algorithm")
    wiki = tmp_path / "wiki"

    r1 = await export(store, mem, wiki, write=True)
    first = {p: (wiki / p).read_bytes() for p in
             ("_graph/person-1-ada-lovelace.md", "_memory/project-1.md")}
    r2 = await export(store, mem, wiki, write=True)
    second = {p: (wiki / p).read_bytes() for p in first}

    assert r1.applied and all(a.status == "write" for a in r1.actions)
    assert first == second  # byte-identical re-export
    assert all(a.status == "unchanged" for a in r2.actions)  # idempotent
    assert b"generated_by: kira-graph" in first["_graph/person-1-ada-lovelace.md"]
    assert b"generated_by: kira-graph" in first["_memory/project-1.md"]


async def test_dry_run_writes_nothing(tmp_path: Path) -> None:
    store, mem = await _stores(tmp_path)
    await _node(store, "topic", "Engines")
    wiki = tmp_path / "wiki"
    report = await export(store, mem, wiki, write=False)
    assert not report.applied
    assert report.actions and not wiki.exists()  # planned, but nothing on disk


# --- non-destructive: marker guard -----------------------------------------
async def test_never_overwrites_a_user_or_unmarked_file(tmp_path: Path) -> None:
    store, mem = await _stores(tmp_path)
    await _node(store, "person", "Ada Lovelace")
    wiki = tmp_path / "wiki"
    target = wiki / "_graph" / "person-1-ada-lovelace.md"
    target.parent.mkdir(parents=True)
    target.write_text("# my own notes\nhand-written, no marker\n", encoding="utf-8")

    report = await export(store, mem, wiki, write=True)

    act = next(a for a in report.actions if a.path == "_graph/person-1-ada-lovelace.md")
    assert act.status == "skip-user-file"
    assert "hand-written" in target.read_text(encoding="utf-8")  # untouched


async def test_overwrites_our_own_marked_file(tmp_path: Path) -> None:
    store, mem = await _stores(tmp_path)
    nid = await _node(store, "person", "Ada")
    wiki = tmp_path / "wiki"
    await export(store, mem, wiki, write=True)  # creates a marked page
    await store.update_node(nid, summary="a revised summary")

    report = await export(store, mem, wiki, write=True)
    act = next(a for a in report.actions if a.path.startswith("_graph/"))
    assert act.status == "write"  # a marked file is ours to regenerate
    assert MARKER in (wiki / "_graph" / "person-1-ada.md").read_text(encoding="utf-8")
    assert "a revised summary" in (wiki / "_graph" / "person-1-ada.md").read_text(encoding="utf-8")


async def test_rewrites_exact_legacy_marker_and_canonicalizes_it(tmp_path: Path) -> None:
    store, mem = await _stores(tmp_path)
    await _node(store, "person", "Ada")
    wiki = tmp_path / "wiki"
    await export(store, mem, wiki, write=True)
    target = wiki / "_graph" / "person-1-ada.md"
    target.write_text(
        target.read_text(encoding="utf-8").replace("kira-graph", "kairo-graph"),
        encoding="utf-8",
    )

    report = await export(store, mem, wiki, write=True)

    action = next(a for a in report.actions if a.path == "_graph/person-1-ada.md")
    rewritten = target.read_text(encoding="utf-8")
    assert action.status == "write"
    assert "generated_by: kira-graph" in rewritten
    assert "kairo-graph" not in rewritten
    second = await export(store, mem, wiki, write=True)
    second_action = next(a for a in second.actions if a.path == "_graph/person-1-ada.md")
    assert second_action.status == "unchanged"


@pytest.mark.parametrize(
    "marker_yaml",
    (
        "generated_by: Kira-graph",
        "generated_by: kairo-graph-extra",
        "generated_by:\n  - kairo-graph",
    ),
)
async def test_near_miss_or_non_string_marker_is_never_owned(
    tmp_path: Path, marker_yaml: str
) -> None:
    store, mem = await _stores(tmp_path)
    await _node(store, "person", "Ada")
    wiki = tmp_path / "wiki"
    target = wiki / "_graph" / "person-1-ada.md"
    target.parent.mkdir(parents=True)
    original = f"---\n{marker_yaml}\n---\n\nuser-authored\n"
    target.write_text(original, encoding="utf-8")

    report = await export(store, mem, wiki, write=True)

    action = next(a for a in report.actions if a.path == "_graph/person-1-ada.md")
    assert action.status == "skip-user-file"
    assert target.read_text(encoding="utf-8") == original


# --- containment + private exclusion + redaction ---------------------------
async def test_all_targets_stay_in_reserved_namespaces(tmp_path: Path) -> None:
    store, mem = await _stores(tmp_path)
    await _node(store, "person", "A")
    await _mem(mem, "some memory")
    report = await export(store, mem, tmp_path / "wiki", write=False)
    assert all(a.path.startswith(("_graph/", "_memory/")) for a in report.actions)


async def test_private_nodes_are_excluded(tmp_path: Path) -> None:
    store, mem = await _stores(tmp_path)
    await _node(store, "person", "Public Person", sensitivity="low")
    await _node(store, "person", "Secret Person", sensitivity="private")
    report = await export(store, mem, tmp_path / "wiki", write=False)
    paths = [a.path for a in report.actions]
    assert any("public-person" in p for p in paths)
    assert not any("secret-person" in p for p in paths)  # private never projected


async def test_secret_shaped_content_is_redacted(tmp_path: Path) -> None:
    store, mem = await _stores(tmp_path)
    await _mem(mem, "the deploy key is sk-livedeadbeef123456 keep it safe")
    wiki = tmp_path / "wiki"
    report = await export(store, mem, wiki, write=True)

    act = next(a for a in report.actions if a.path == "_memory/project-1.md")
    body = (wiki / "_memory" / "project-1.md").read_text(encoding="utf-8")
    assert act.redacted is True
    assert "sk-livedeadbeef123456" not in body and "[redacted]" in body


# --- wikilinks between exported entities ------------------------------------
async def test_asserted_edges_become_wikilinks(tmp_path: Path) -> None:
    store, mem = await _stores(tmp_path)
    a = await _node(store, "topic", "Analytical Engine")
    b = await _node(store, "topic", "Difference Engine")
    await store.upsert_edge(
        src_kind="topic", src_id=str(a), dst_kind="topic", dst_id=str(b), edge_kind="relates_to",
        origin="asserted", trust_class="reviewed", created_by="user",
        created_at="2026-03-01T00:00:00+00:00", project_id=1)
    wiki = tmp_path / "wiki"
    await export(store, mem, wiki, write=True)

    page_a = (wiki / "_graph" / f"topic-{a}-analytical-engine.md").read_text(encoding="utf-8")
    page_b = (wiki / "_graph" / f"topic-{b}-difference-engine.md").read_text(encoding="utf-8")
    assert f"[[topic-{b}-difference-engine]]" in page_a
    assert f"[[topic-{a}-analytical-engine]]" in page_b  # neighbors are bidirectional
