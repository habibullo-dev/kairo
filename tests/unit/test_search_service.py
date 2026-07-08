"""Federated search service (Phase 11 T3): cross-domain hydration, plain-text snippet projection
(chat JSON de-noised), chat dedupe-per-session, cross-project scoping, and empty-query. Keyless."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from jarvis.memory.store import MemoryStore
from jarvis.persistence.artifacts import ArtifactStore
from jarvis.persistence.db import connect
from jarvis.persistence.sessions import SessionStore
from jarvis.projects.store import ProjectStore
from jarvis.search import search

_OPEN: list = []
_EMB = [1.0, 2.0, 3.0]


@pytest.fixture(autouse=True)
async def _close():
    yield
    while _OPEN:
        await _OPEN.pop().close()


async def _setup(tmp_path: Path):
    db = await connect(tmp_path / "search.db")
    _OPEN.append(db)
    lock = asyncio.Lock()
    projects = ProjectStore(db, lock)
    sessions = SessionStore(db, lock)
    memory = MemoryStore(db, lock)
    artifacts = ArtifactStore(db, lock, data_dir=tmp_path / "data", managed_roots={})
    return db, projects, sessions, memory, artifacts


def _domains(results: list[dict]) -> set[str]:
    return {r["domain"] for r in results}


async def test_federated_search_hydrates_and_denoises_chat_json(tmp_path: Path) -> None:
    db, projects, sessions, memory, artifacts = await _setup(tmp_path)
    a = await projects.create(name="Alpha")

    sid = await sessions.create_session(project_id=a)
    # Two matching messages in ONE session (dedupe test) + a tool_use block (de-noise test).
    await sessions.save_messages(sid, [
        {"role": "user", "content": [
            {"type": "text", "text": "searchcanary alpha one here"},
            {"type": "tool_use", "name": "run_shell", "input": {"cmd": "ls"}},
        ]},
        {"role": "assistant", "content": [{"type": "text", "text": "searchcanary alpha two"}]},
    ])
    await memory.add(type="fact", content="searchcanary memory note", embedding=_EMB,
                     embedding_model="fake", source="user", project_id=a)
    await artifacts.register(origin_type="x", origin_id="a1", kind="digest",
                             title="searchcanary artifact", external_uri="https://e/x",
                             created_by="agent", project_id=a)

    results = await search(db, "searchcanary", project_id=a)
    assert {"chats", "memories", "artifacts"} <= _domains(results)

    chats = [r for r in results if r["domain"] == "chats"]
    assert len(chats) == 1  # deduped to one result per session
    assert chats[0]["ref_id"] == sid
    # Snippet is projected plain prose — the JSON/tool scaffolding must not leak.
    assert "searchcanary alpha" in chats[0]["snippet"]
    assert "tool_use" not in chats[0]["snippet"] and "run_shell" not in chats[0]["snippet"]


async def test_search_is_project_scoped(tmp_path: Path) -> None:
    db, projects, sessions, memory, _artifacts = await _setup(tmp_path)
    a = await projects.create(name="Alpha")
    b = await projects.create(name="Bravo")
    sa = await sessions.create_session(project_id=a)
    sb = await sessions.create_session(project_id=b)
    await sessions.save_messages(sa, [{"role": "user", "content": "searchcanary alpha"}])
    await sessions.save_messages(sb, [{"role": "user", "content": "searchcanary bravo"}])

    a_results = await search(db, "searchcanary", project_id=a, include_global=False)
    a_refs = {(r["domain"], r["ref_id"]) for r in a_results}
    assert ("chats", sa) in a_refs
    assert ("chats", sb) not in a_refs  # project B's chat never surfaces for a project-A search

    b_results = await search(db, "searchcanary", project_id=b, include_global=False)
    b_refs = {(r["domain"], r["ref_id"]) for r in b_results}
    assert ("chats", sb) in b_refs and ("chats", sa) not in b_refs


async def test_snippet_is_capped_and_plain(tmp_path: Path) -> None:
    db, projects, _sessions, memory, _artifacts = await _setup(tmp_path)
    a = await projects.create(name="Alpha")
    long_body = ("padding " * 60) + "searchcanary needle " + ("tail " * 60)
    await memory.add(type="fact", content=long_body, embedding=_EMB, embedding_model="fake",
                     source="user", project_id=a)
    results = await search(db, "searchcanary", project_id=a)
    mem = [r for r in results if r["domain"] == "memories"][0]
    assert "searchcanary" in mem["snippet"]
    assert len(mem["snippet"]) <= 220  # _SNIPPET_CHARS + ellipses, never the full body
    assert "\n" not in mem["snippet"]  # whitespace collapsed


async def test_chat_match_only_in_scaffolding_is_dropped(tmp_path: Path) -> None:
    db, projects, sessions, _memory, _artifacts = await _setup(tmp_path)
    a = await projects.create(name="Alpha")
    sid = await sessions.create_session(project_id=a)
    # The query term lives ONLY in a tool_use block (scaffolding the user never typed).
    await sessions.save_messages(sid, [
        {"role": "user", "content": [
            {"type": "text", "text": "the quarterly plan looks fine"},
            {"type": "tool_use", "name": "run_shell", "input": {"cmd": "scaffoldingtoken run"}},
        ]},
    ])
    # It matches the raw-JSON FTS index but NOT the visible prose → dropped (no phantom row).
    assert await search(db, "scaffoldingtoken", project_id=a) == []
    # A term in the visible prose is still found, with a real snippet.
    hits = await search(db, "quarterly", project_id=a)
    assert [h["domain"] for h in hits] == ["chats"]
    assert "quarterly" in hits[0]["snippet"]


async def test_quarantined_artifact_never_searchable(tmp_path: Path) -> None:
    db, projects, _sessions, _memory, artifacts = await _setup(tmp_path)
    a = await projects.create(name="Alpha")
    await artifacts.register(origin_type="meeting", origin_id="m1", kind="meeting_note",
                             title="quarantinedcanary transcript", external_uri="kairo://x",
                             created_by="user", sensitivity="quarantined", project_id=a)
    await artifacts.register(origin_type="wiki", origin_id="w1", kind="wiki_page",
                             title="quarantinedcanary page", external_uri="kairo://y",
                             created_by="agent", project_id=a)
    titles = [h["title"] for h in await search(db, "quarantinedcanary", project_id=a)]
    assert any("page" in t for t in titles)  # the normal artifact surfaces
    assert not any("transcript" in t for t in titles)  # the quarantined one never does


async def test_blank_query_returns_nothing(tmp_path: Path) -> None:
    db, projects, sessions, _memory, _artifacts = await _setup(tmp_path)
    a = await projects.create(name="Alpha")
    sid = await sessions.create_session(project_id=a)
    await sessions.save_messages(sid, [{"role": "user", "content": "searchcanary"}])
    assert await search(db, "") == []
    assert await search(db, "   ") == []
    assert await search(db, "!!!") == []
