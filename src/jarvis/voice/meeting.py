"""Meeting capture mode (Phase 7, Task 7 — checkpoint §1.5 / D8).

A captured meeting is an **untrusted knowledge source**, not a command stream. Recording
is explicitly consented (start/stop, observable state) and never unattended; the resulting
transcript is ingested through the normal KB path but quarantined **unreviewed** (reusing
the ADR-0004 quarantine, since the audio may contain speech from anyone in the room);
searchable only after ``kb review``. **No auto-actions**: this class holds no agent loop
and no scheduler, so a meeting's "action items" can never self-execute — turning one into a
task is a separate, human-approved step.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from jarvis.observability import get_logger

if TYPE_CHECKING:
    from jarvis.knowledge.service import IngestResult, KnowledgeService
    from jarvis.voice.protocols import STTProvider

IDLE = "idle"
RECORDING = "recording"


class MeetingCapture:
    """Records one consented meeting and files its transcript as an unreviewed KB source.

    ``on_state`` observes recording state (a UI shows the indicator — no silent capture).
    ``attended`` must be True; an unattended context is refused (no unattended recording).
    Deliberately takes only a ``KnowledgeService`` and an ``STTProvider`` — no loop, no
    tasks — so it *cannot* act on what it hears."""

    def __init__(
        self,
        knowledge: KnowledgeService,
        stt: STTProvider,
        *,
        on_state=None,
        attended: bool = True,
        retain_audio: bool = False,
        artifacts=None,
        log=None,
    ) -> None:
        self.knowledge = knowledge
        self.stt = stt
        self.on_state = on_state
        self.attended = attended
        self.retain_audio = retain_audio  # default: keep the transcript, discard raw audio
        self.log = log or get_logger("jarvis.voice.meeting")
        self.artifacts = artifacts  # Phase 11: optional ArtifactStore (None ⇒ no indexing)
        self.state = IDLE

    def _set(self, state: str) -> None:
        self.state = state
        if self.on_state is not None:
            self.on_state(state)

    async def capture(self, audio: bytes, *, title: str | None = None) -> IngestResult:
        """Transcribe a consented recording and file it as an unreviewed KB source."""
        if not self.attended:
            raise RuntimeError(
                "no unattended recording; meeting capture requires a present, consenting human"
            )
        self._set(RECORDING)
        try:
            transcript = await self.stt.transcribe(audio)
        finally:
            self._set(IDLE)
        return await self._ingest(transcript.text, title)

    async def _ingest(self, text: str, title: str | None) -> IngestResult:
        # Quarantine the transcript UNREVIEWED — untrusted audio content, not trusted input.
        # Reuses the KB's unattended-quarantine path (ADR-0004): searchable only after a
        # human runs `kb review`. No action is taken on the content here.
        self.knowledge.bound_unattended = True
        try:
            result = await self.knowledge.ingest(
                text=text, title=title or "Meeting transcript", created_by="user"
            )
        finally:
            self.knowledge.bound_unattended = False
        self.log.info(
            "meeting_captured", review_status=result.review_status, source_id=result.source_id
        )
        # Phase 11: index a meeting note as an artifact ONLY when its KB source is reviewed.
        # Transcripts are quarantined (unreviewed) at capture per ADR-0004; registering one while
        # unreviewed would make quarantined audio content discoverable in search + servable via
        # the content route, defeating the quarantine. So this does not fire until a source is
        # promoted (the review-promotion artifact hook is future work). Fail-soft.
        if self.artifacts is not None and result.review_status == "reviewed":
            try:
                source = await self.knowledge.store.get_source(result.source_id)
                if source is not None:
                    await self.artifacts.register(
                        origin_type="meeting",
                        origin_id=str(result.source_id),
                        kind="meeting_note",
                        title=result.title or "Meeting transcript",
                        created_by="user",
                        local_path=self.knowledge.knowledge_dir / source.markdown_path,
                        sensitivity="quarantined",
                        project_id=None,
                    )
            except Exception:  # noqa: BLE001 - artifact bookkeeping must never fail a capture
                self.log.warning("meeting_artifact_register_failed", source_id=result.source_id)
        return result
