"""AttentionStore + the lifecycle state machine (Phase 16 Task 1).

Pins: idempotent create by dedupe_key (a producer re-run doesn't duplicate); only legal state
moves (open→done/dismiss/snooze/expire, snoozed→open/…, terminals frozen); trust_class validated
(dreaming defaults to untrusted model_generated); list scopes by state/kind/priority/project
(cross-project isolation); open_counts for the minimized push. Keyless via tmp SQLite."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from kira.attention import (
    AttentionKind,
    AttentionPriority,
    AttentionState,
    AttentionStore,
    InvalidTransition,
)
from kira.persistence.db import connect

_OPEN: list = []


@pytest.fixture(autouse=True)
async def _close():
    yield
    while _OPEN:
        await _OPEN.pop().close()


async def _store(tmp_path: Path) -> AttentionStore:
    db = await connect(tmp_path / "a.db")
    _OPEN.append(db)
    lock = asyncio.Lock()
    from kira.projects import ProjectStore

    projects = ProjectStore(db, lock)
    for name in ("One", "Two"):  # ids 1, 2 (FK: attention_items.project_id → projects.id)
        await projects.create(name=name)
    return AttentionStore(db, lock)


async def _add(s: AttentionStore, **kw) -> int:
    """Create with terse defaults so the tests stay readable."""
    kw.setdefault("kind", AttentionKind.PROPOSAL)
    kw.setdefault("source", "dreaming")
    kw.setdefault("title", "x")
    return await s.create(**kw)


async def test_create_and_get_defaults_untrusted_open(tmp_path: Path) -> None:
    s = await _store(tmp_path)
    item = await s.get(await _add(s, title="Nightly review"))
    assert item is not None
    assert item.state is AttentionState.OPEN
    assert item.trust_class == "model_generated"  # dreaming/agent content is untrusted by default
    assert item.priority is AttentionPriority.NORMAL  # defaults bias to digest, not urgent


async def test_dedupe_key_is_idempotent(tmp_path: Path) -> None:
    s = await _store(tmp_path)
    a = await _add(s, kind=AttentionKind.REVIEW, dedupe_key="rev:2026-07-10")
    b = await _add(s, kind=AttentionKind.REVIEW, dedupe_key="rev:2026-07-10")
    assert a == b  # a re-run of tonight's producer returns the same row — no re-nag
    assert len(await s.list()) == 1
    c = await _add(s, kind=AttentionKind.REVIEW, dedupe_key="rev:2026-07-11")  # new window ⇒ new
    assert c != a and len(await s.list()) == 2


async def test_create_if_new_reports_the_durable_insert_once(tmp_path: Path) -> None:
    s = await _store(tmp_path)
    first, first_created = await s.create_if_new(
        kind=AttentionKind.REVIEW,
        source="dreaming",
        title="Nightly review",
        dedupe_key="review:2026-07-13",
    )
    second, second_created = await s.create_if_new(
        kind=AttentionKind.REVIEW,
        source="dreaming",
        title="Nightly review",
        dedupe_key="review:2026-07-13",
    )
    assert (first_created, second_created) == (True, False)
    assert first == second and len(await s.list()) == 1


async def test_unknown_trust_class_rejected(tmp_path: Path) -> None:
    s = await _store(tmp_path)
    with pytest.raises(ValueError):
        await _add(s, kind=AttentionKind.ALERT, source="system", trust_class="vibes")


async def test_lifecycle_done_dismiss_snooze_reopen(tmp_path: Path) -> None:
    s = await _store(tmp_path)
    i1 = await _add(s)
    done = await s.mark_done(i1)
    assert done.state is AttentionState.DONE and done.resolved_at is not None
    with pytest.raises(InvalidTransition):  # terminal — cannot move again
        await s.dismiss(i1)

    i2 = await _add(s)
    snoozed = await s.snooze(i2, until="2026-07-11T08:00:00+00:00")
    assert snoozed.state is AttentionState.SNOOZED and snoozed.snooze_until.startswith("2026-07-11")
    reopened = await s.reopen(i2)
    assert reopened.state is AttentionState.OPEN and reopened.snooze_until is None


async def test_resolve_dispatch(tmp_path: Path) -> None:
    s = await _store(tmp_path)
    i = await _add(s, kind=AttentionKind.APPROVAL, source="intent")
    assert (await s.resolve(i, "dismiss")).state is AttentionState.DISMISSED
    with pytest.raises(ValueError):  # unknown action
        await s.resolve(await _add(s, kind=AttentionKind.ALERT, source="system"), "nuke")
    with pytest.raises(ValueError):  # snooze needs 'until'
        await s.resolve(await _add(s, kind=AttentionKind.REVIEW), "snooze")


async def test_list_scopes_by_state_kind_and_project(tmp_path: Path) -> None:
    s = await _store(tmp_path)
    await _add(s, title="p1", project_id=1)
    await _add(s, kind=AttentionKind.ALERT, source="system", title="a1", project_id=2,
               priority=AttentionPriority.URGENT)
    await s.mark_done(await _add(s, title="p2", project_id=1))
    assert len(await s.list(state=AttentionState.OPEN)) == 2
    assert len(await s.list(project_id=1)) == 2  # cross-project isolation: only project 1's rows
    assert len(await s.list(project_id=2)) == 1
    assert len(await s.list(kind=AttentionKind.ALERT)) == 1
    assert len(await s.list(priority=AttentionPriority.URGENT)) == 1


async def test_open_counts_by_kind(tmp_path: Path) -> None:
    s = await _store(tmp_path)
    await _add(s)
    await _add(s)
    await s.mark_done(await _add(s, kind=AttentionKind.APPROVAL, source="intent"))  # resolved ⇒ 0
    counts = await s.open_counts()
    assert counts == {"proposal": 2}  # the minimized push shows kind + COUNT, never a body
