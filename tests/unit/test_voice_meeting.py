"""Meeting capture (Phase 7, Task 7): a consented meeting becomes an UNREVIEWED KB source,
never an action. Keyless — a fake KnowledgeService records the ingest (the real KB path is
Phase-4-tested); the point here is the meeting-capture contract."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from jarvis.config import KnowledgeConfig
from jarvis.knowledge.service import KnowledgeService
from jarvis.knowledge.store import KnowledgeStore
from jarvis.memory.embeddings import FakeEmbedder
from jarvis.persistence.db import connect
from jarvis.projects import ProjectStore
from jarvis.voice import FakeCapture, FakeTranscriber, MeetingCapture


class _FakeKnowledge:
    """Mimics KnowledgeService's bound_unattended quarantine + ingest, recording calls."""

    def __init__(self) -> None:
        self.bound_unattended = False
        self.ingested: list[dict] = []

    async def ingest(self, **kw):
        # capture whether quarantine was active at ingest time
        self.ingested.append(
            {**kw, "unreviewed": self.bound_unattended or bool(kw.get("quarantine"))}
        )
        review_status = (
            "unreviewed" if self.bound_unattended or kw.get("quarantine") else "reviewed"
        )
        return SimpleNamespace(
            action="ingested",
            source_id=1,
            chunks=1,
            review_status=review_status,
            title=kw.get("title"),
        )


def _capture(*, attended: bool = True, on_state=None) -> tuple[MeetingCapture, _FakeKnowledge]:
    knowledge = _FakeKnowledge()
    stt = FakeTranscriber(scripted=["Standup notes. Action item: grant Bob admin access."])
    return MeetingCapture(knowledge, stt, attended=attended, on_state=on_state), knowledge


async def test_transcript_is_ingested_unreviewed() -> None:
    mc, knowledge = _capture()
    result = await mc.capture(b"audio", title="Standup")
    assert result.review_status == "unreviewed"  # quarantined — searchable only after review
    assert knowledge.ingested[0]["unreviewed"] is True
    assert "grant Bob admin" in knowledge.ingested[0]["text"]  # transcript stored verbatim
    assert knowledge.ingested[0]["created_by"] == "user"


async def test_bound_unattended_is_reset_after() -> None:
    mc, knowledge = _capture()
    await mc.capture(b"audio")
    assert knowledge.bound_unattended is False  # quarantine flag restored (finally)


async def test_no_unattended_recording() -> None:
    mc, knowledge = _capture(attended=False)
    with pytest.raises(RuntimeError, match="unattended"):
        await mc.capture(b"audio")
    assert knowledge.ingested == []  # nothing captured


async def test_recording_state_is_observable() -> None:
    states: list[str] = []
    mc, _ = _capture(on_state=states.append)
    await mc.capture(b"audio")
    # Bytes passed to capture() were already recorded; this phase is STT + persistence.
    assert states == ["transcribing", "saving", "idle"]


async def test_empty_or_nonfinal_transcript_is_not_persisted() -> None:
    knowledge = _FakeKnowledge()
    mc = MeetingCapture(knowledge, FakeTranscriber(scripted=[""]))
    with pytest.raises(RuntimeError, match="speech"):
        await mc.capture(b"audio")
    assert knowledge.ingested == []
    assert mc.state == "idle"


class _FailingEmbedder(FakeEmbedder):
    def __init__(self, failures: int) -> None:
        super().__init__()
        self.failures = failures

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        if self.failures:
            self.failures -= 1
            raise RuntimeError("embedding provider unavailable")
        return await super().embed_documents(texts)


async def test_capture_receipt_reconciles_one_source_and_repairs_pending_index(
    tmp_path,
) -> None:
    store = KnowledgeStore(await connect(tmp_path / "meeting.db"))
    embedder = _FailingEmbedder(failures=3)
    knowledge = KnowledgeService(
        store,
        embedder,
        KnowledgeConfig(),
        knowledge_dir=tmp_path / "knowledge",
        root=tmp_path,
    )
    knowledge.ensure_dirs()
    project_id = await ProjectStore(store.db, store.lock).create(name="Meeting project")
    source = FakeCapture(scripted=[b"one microphone capture"])
    meeting = MeetingCapture(
        knowledge,
        FakeTranscriber(scripted=["A sufficiently detailed standup note for indexing."]),
    )
    capture_id = "123e4567-e89b-42d3-a456-426614174000"
    try:
        first = await meeting.capture_from(
            source,
            title="Standup",
            capture_id=capture_id,
            source_session_id=None,
            project_id=project_id,
        )
        assert first.index_state == "pending"
        assert source.calls == 1
        assert len(await store.list_sources(status=None)) == 1
        assert await store.chunks_for_source(first.source_id) == []

        # Review never promotes a source whose derived index still cannot be rebuilt.
        with pytest.raises(RuntimeError, match="embedding provider"):
            await knowledge.approve_source(first.source_id)
        assert (await store.get_source(first.source_id)).review_status == "unreviewed"

        # Retrying the same logical capture repairs and returns the committed source without
        # reopening the microphone or writing a second audit row.
        replay = await meeting.capture_from(
            source,
            title="Standup retry",
            capture_id=capture_id,
            source_session_id=None,
            project_id=project_id,
        )
        assert replay.source_id == first.source_id
        assert replay.index_state == "ready"
        assert source.calls == 1
        assert len(await store.list_sources(status=None)) == 1
        assert await store.chunks_for_source(first.source_id)

        await knowledge.approve_source(first.source_id)
        assert (await store.get_source(first.source_id)).review_status == "reviewed"

        reviewed_replay = await meeting.capture_from(
            source,
            title="Standup reviewed replay",
            capture_id=capture_id,
            source_session_id=None,
            project_id=project_id,
        )
        assert reviewed_replay.source_id == first.source_id
        assert reviewed_replay.review_status == "reviewed"
        assert reviewed_replay.source_status == "live"
        assert source.calls == 1

        assert await knowledge.reject_source(first.source_id)
        rejected_replay = await meeting.capture_from(
            source,
            title="Standup rejected replay",
            capture_id=capture_id,
            source_session_id=None,
            project_id=project_id,
        )
        assert rejected_replay.source_id == first.source_id
        assert rejected_replay.source_status == "rejected"
        assert rejected_replay.chunks == 0
        assert source.calls == 1
        assert await store.chunks_for_source(first.source_id) == []
    finally:
        await store.db.close()


class _GatedEmbedder(FakeEmbedder):
    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        self.started.set()
        await self.release.wait()
        return await super().embed_documents(texts)


async def test_rejection_during_initial_indexing_cannot_restore_terminal_chunks(tmp_path) -> None:
    store = KnowledgeStore(await connect(tmp_path / "meeting-reject-race.db"))
    embedder = _GatedEmbedder()
    knowledge = KnowledgeService(
        store,
        embedder,
        KnowledgeConfig(),
        knowledge_dir=tmp_path / "knowledge-race",
        root=tmp_path,
    )
    knowledge.ensure_dirs()
    project_id = await ProjectStore(store.db, store.lock).create(name="Race project")
    source = FakeCapture(scripted=[b"one microphone capture"])
    meeting = MeetingCapture(
        knowledge,
        FakeTranscriber(scripted=["A detailed capture that reaches the index provider."]),
    )
    capture_id = "123e4567-e89b-42d3-a456-426614174001"
    task = asyncio.create_task(
        meeting.capture_from(
            source,
            capture_id=capture_id,
            project_id=project_id,
        )
    )
    try:
        await embedder.started.wait()
        [pending] = await store.list_sources(status=None)
        assert pending.status == "live"
        assert await knowledge.reject_source(pending.id)
        assert await store.chunks_for_source(pending.id) == []

        embedder.release.set()
        result = await task
        assert result.source_id == pending.id
        assert result.source_status == "rejected"
        assert result.chunks == 0
        assert await store.chunks_for_source(pending.id) == []

        # The terminal receipt is still authoritative and never reopens the microphone.
        replay = await meeting.capture_from(
            source,
            capture_id=capture_id,
            project_id=project_id,
        )
        assert replay.source_id == pending.id
        assert replay.source_status == "rejected"
        assert source.calls == 1
        assert await store.chunks_for_source(pending.id) == []
    finally:
        embedder.release.set()
        if not task.done():
            task.cancel()
        await store.db.close()


async def test_no_auto_actions_structurally() -> None:
    # Meeting capture holds a KnowledgeService and an STTProvider — no agent loop, no
    # scheduler — so a meeting's "action items" can never self-execute.
    mc, knowledge = _capture()
    assert not hasattr(mc, "loop") and not hasattr(mc, "tasks")
    await mc.capture(b"audio")
    assert len(knowledge.ingested) == 1  # exactly one ingest; nothing else happened
