"""Meeting capture (Phase 7, Task 7): a consented meeting becomes an UNREVIEWED KB source,
never an action. Keyless — a fake KnowledgeService records the ingest (the real KB path is
Phase-4-tested); the point here is the meeting-capture contract."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from jarvis.voice import FakeTranscriber, MeetingCapture


class _FakeKnowledge:
    """Mimics KnowledgeService's bound_unattended quarantine + ingest, recording calls."""

    def __init__(self) -> None:
        self.bound_unattended = False
        self.ingested: list[dict] = []

    async def ingest(self, **kw):
        # capture whether quarantine was active at ingest time
        self.ingested.append({**kw, "unreviewed": self.bound_unattended})
        review_status = "unreviewed" if self.bound_unattended else "reviewed"
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
    assert states == ["recording", "idle"]


async def test_no_auto_actions_structurally() -> None:
    # Meeting capture holds a KnowledgeService and an STTProvider — no agent loop, no
    # scheduler — so a meeting's "action items" can never self-execute.
    mc, knowledge = _capture()
    assert not hasattr(mc, "loop") and not hasattr(mc, "tasks")
    await mc.capture(b"audio")
    assert len(knowledge.ingested) == 1  # exactly one ingest; nothing else happened
