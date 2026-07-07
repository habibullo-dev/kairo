"""Project-scoped memory: the adversarial no-leak suite (Phase 10 Task 4 / A1).

The load-bearing guarantees, each a test:
* Recall in project A never returns project B's or another project's memories; global recall
  returns ONLY global memories (no project leaks into the global chat).
* A project memory is visible in its own project alongside global memories (recall union).
* Dedup is EXACT scope: a project write can't supersede a global memory or another project's,
  and vice versa — no silent cross-scope data loss.
* Reflection attributes memories to the session's project.

Keyless: FakeEmbedder (bag-of-words hash — identical text ⇒ identical vector, so scope, not
similarity, is what's under test) + a scripted FakeClient for dedup adjudication."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from jarvis.config import MemoryConfig
from jarvis.core import FakeClient, text_message
from jarvis.memory.embeddings import FakeEmbedder
from jarvis.memory.reflection import reflect
from jarvis.memory.service import MemoryService
from jarvis.memory.store import MemoryStore
from jarvis.persistence.db import connect
from jarvis.projects import ProjectStore

_OPEN: list = []


@pytest.fixture(autouse=True)
async def _close():
    yield
    while _OPEN:
        await _OPEN.pop().close()


async def _service(tmp_path: Path, *, responses: list | None = None) -> MemoryService:
    db = await connect(tmp_path / "m.db")
    _OPEN.append(db)
    lock = asyncio.Lock()
    projects = ProjectStore(db, lock)
    await projects.create(name="Project A")  # id 1
    await projects.create(name="Project B")  # id 2
    client = FakeClient(responses) if responses is not None else None
    return MemoryService(
        store=MemoryStore(db, lock),
        embedder=FakeEmbedder(),
        config=MemoryConfig(),
        utility_client=client,
    )


# --- recall scoping (no cross-project leak) --------------------------------


async def test_project_memory_not_recalled_in_other_project_or_global(tmp_path: Path) -> None:
    svc = await _service(tmp_path, responses=[])
    await svc.remember("the alpha widget ships friday", "fact", source="user", project_id=1)

    # Visible in project A (its own scope).
    in_a = await svc.recall("the alpha widget ships friday", project_id=1)
    assert any("alpha widget" in h.memory.content for h in in_a)
    # NOT visible in project B.
    in_b = await svc.recall("the alpha widget ships friday", project_id=2)
    assert all("alpha widget" not in h.memory.content for h in in_b)
    # NOT visible in the global scope (project_id=None) — the pre-mortem #8 leak.
    in_global = await svc.recall("the alpha widget ships friday", project_id=None)
    assert all("alpha widget" not in h.memory.content for h in in_global)


async def test_global_memory_visible_everywhere(tmp_path: Path) -> None:
    svc = await _service(tmp_path, responses=[])
    await svc.remember("the user prefers metric units", "preference", source="user")  # global

    for scope in (1, 2, None):
        hits = await svc.recall("the user prefers metric units", project_id=scope)
        assert any("metric units" in h.memory.content for h in hits), f"missing in scope {scope}"


async def test_auto_recall_block_has_no_cross_project_content(tmp_path: Path) -> None:
    svc = await _service(tmp_path, responses=[])
    await svc.remember("secret canary belongs to project A only", "fact", project_id=1)
    # A global chat's auto-recall block must not contain the project-A content (string-level).
    block = await svc.auto_recall_context("tell me the secret canary", project_id=None)
    assert block is None or "canary belongs to project A" not in block


# --- dedup exact scope (no cross-scope supersede) --------------------------


async def test_project_write_cannot_supersede_global(tmp_path: Path) -> None:
    # A 'supersede' verdict would fire IF the global neighbor were in scope — but dedup uses
    # exact project scope, so the project write never even sees the global memory. Script a
    # 'supersede' response to prove it is NOT consulted (the global row stays live).
    svc = await _service(tmp_path, responses=[text_message("supersede")])
    g = await svc.remember("deploy target is us-east-1", "fact", source="user")  # global
    await svc.remember("deploy target is us-east-1", "fact", source="user", project_id=1)
    # The global memory is untouched (still live) — a project write can't retire it.
    globals_live = await svc.store.all_live(project_id=None)
    assert any(m.id == g.memory_id and m.status == "live" for m in globals_live)


async def test_global_write_cannot_supersede_project(tmp_path: Path) -> None:
    svc = await _service(tmp_path, responses=[text_message("supersede")])
    p = await svc.remember("api key rotation is monthly", "fact", project_id=1)
    await svc.remember("api key rotation is monthly", "fact", source="user")  # global write
    # Project A's memory is untouched by the global write.
    a_live = await svc.store.all_live(project_id=1, include_global=False)
    assert any(m.id == p.memory_id and m.status == "live" for m in a_live)


async def test_dedup_within_same_project_still_works(tmp_path: Path) -> None:
    # Exact scope doesn't break normal dedup: two identical writes in the SAME project dedupe.
    svc = await _service(tmp_path, responses=[text_message("duplicate")])
    first = await svc.remember("standup is at 10am", "fact", project_id=1)
    dup = await svc.remember("standup is at 10am", "fact", project_id=1)
    assert dup.action == "duplicate" and dup.memory_id == first.memory_id


# --- reflection attribution ------------------------------------------------


async def test_reflection_attributes_to_session_project(tmp_path: Path) -> None:
    from jarvis.core import tool_use_message
    from jarvis.core.client import ToolCall
    from jarvis.persistence import SessionStore

    svc = await _service(tmp_path, responses=[])
    # A real session bound to project B (memories.source_session_id has a FK to sessions).
    sessions = SessionStore(svc.store.db, svc.store.lock)
    sid = await sessions.create_session(project_id=2)
    transcript = [
        {"role": "user", "content": "for this project, the release train is biweekly"},
        {"role": "assistant", "content": "Noted."},
    ]
    forced = tool_use_message(
        [
            ToolCall(
                "s1",
                "save_memories",
                {"memories": [{"type": "project", "content": "The release train is biweekly."}]},
            )
        ]
    )
    await reflect(
        transcript=transcript,
        session_id=sid,
        service=svc,
        client=FakeClient([forced]),
        model="fake",
        project_id=2,  # the session belonged to project B
    )
    # The reflected memory is scoped to project B, not global — so it never leaks globally.
    b_only = await svc.store.all_live(project_id=2, include_global=False)
    assert any("release train is biweekly" in m.content for m in b_only)
    globals_live = await svc.store.all_live(project_id=None)
    assert all("release train is biweekly" not in m.content for m in globals_live)
