"""Meeting-note capture mode (Phase 7, Task 7 — checkpoint §1.5 / D8).

A captured note is an **untrusted knowledge source**, not a command stream. The current
``CaptureSource`` records one explicitly consented, endpointed utterance with observable state
and never runs unattended; the resulting transcript is ingested through the normal KB path but
quarantined **unreviewed** (reusing
the ADR-0004 quarantine, since the audio may contain speech from anyone in the room);
searchable only after ``kb review``. **No auto-actions**: this class holds no agent loop
and no scheduler, so a meeting's "action items" can never self-execute — turning one into a
task is a separate, human-approved step.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import TYPE_CHECKING

from jarvis.knowledge.service import IngestResult
from jarvis.observability import get_logger

if TYPE_CHECKING:
    from jarvis.knowledge.service import KnowledgeService
    from jarvis.voice.listening import CaptureSource
    from jarvis.voice.protocols import STTProvider

IDLE = "idle"
RECORDING = "recording"
TRANSCRIBING = "transcribing"
SAVING = "saving"


class NoSpeechDetectedError(RuntimeError):
    """A finalized meeting-note transcript contained no speech worth persisting."""


class MeetingCapture:
    """Records one consented meeting note and files it as an unreviewed KB source.

    ``on_state`` observes recording/transcribing/saving state (a UI shows the indicator).
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
        self._capture_lock = asyncio.Lock()

    def _set(self, state: str) -> None:
        self.state = state
        if self.on_state is not None:
            self.on_state(state)

    def _require_attended(self) -> None:
        if not self.attended:
            raise RuntimeError(
                "no unattended recording; meeting capture requires a present, consenting human"
            )

    async def capture(
        self,
        audio: bytes,
        *,
        title: str | None = None,
        capture_id: str | None = None,
        source_session_id: int | None = None,
        project_id: int | None = None,
    ) -> IngestResult:
        """Transcribe already-recorded audio and file it as an unreviewed KB source."""
        self._require_attended()
        receipt = capture_id or str(uuid.uuid4())
        async with self._capture_lock:
            existing = await self._existing_result(
                receipt,
                source_session_id=source_session_id,
                project_id=project_id,
            )
            if existing is not None:
                return existing
            try:
                return await self._transcribe_and_ingest(
                    audio,
                    title,
                    capture_id=receipt,
                    source_session_id=source_session_id,
                    project_id=project_id,
                )
            finally:
                self._set(IDLE)

    async def capture_from(
        self,
        source: CaptureSource,
        *,
        title: str | None = None,
        capture_id: str | None = None,
        source_session_id: int | None = None,
        project_id: int | None = None,
    ) -> IngestResult:
        """Record one consented, endpointed note, then transcribe and quarantine it.

        Owning the capture here keeps ``recording`` aligned with the actual open-microphone
        interval.  ``capture()`` remains the import/already-recorded seam and therefore begins at
        ``transcribing`` instead.
        """
        self._require_attended()
        receipt = capture_id or str(uuid.uuid4())
        async with self._capture_lock:
            existing = await self._existing_result(
                receipt,
                source_session_id=source_session_id,
                project_id=project_id,
            )
            if existing is not None:
                return existing
            self._set(RECORDING)
            try:
                audio = await source.capture_utterance()
                return await self._transcribe_and_ingest(
                    audio,
                    title,
                    capture_id=receipt,
                    source_session_id=source_session_id,
                    project_id=project_id,
                )
            finally:
                self._set(IDLE)

    async def reconcile(
        self,
        capture_id: str,
        *,
        source_session_id: int | None = None,
        project_id: int | None = None,
    ) -> IngestResult | None:
        """Return an existing durable receipt before any microphone lease is requested."""
        self._require_attended()
        async with self._capture_lock:
            return await self._existing_result(
                capture_id,
                source_session_id=source_session_id,
                project_id=project_id,
            )

    async def _transcribe_and_ingest(
        self,
        audio: bytes,
        title: str | None,
        *,
        capture_id: str,
        source_session_id: int | None,
        project_id: int | None,
    ) -> IngestResult:
        self._set(TRANSCRIBING)
        transcript = await self.stt.transcribe(audio)
        if not transcript.is_final or not transcript.text.strip():
            raise NoSpeechDetectedError("no finalized speech was detected")
        self._set(SAVING)
        return await self._ingest(
            transcript.text,
            title,
            capture_id=capture_id,
            source_session_id=source_session_id,
            project_id=project_id,
        )

    @staticmethod
    def _origin(capture_id: str, *, source_session_id: int | None, project_id: int | None) -> str:
        scope = f"project:{project_id}" if project_id is not None else "global"
        return f"meeting-capture:{scope}:{capture_id}"

    async def _existing_result(
        self,
        capture_id: str,
        *,
        source_session_id: int | None,
        project_id: int | None,
    ) -> IngestResult | None:
        """Reconcile a durable receipt without ever reopening the microphone."""
        store = getattr(self.knowledge, "store", None)
        find = getattr(store, "find_by_origin", None)
        if not callable(find):
            return None
        origin = self._origin(
            capture_id,
            source_session_id=source_session_id,
            project_id=project_id,
        )
        source = await find(origin, project_id=project_id)
        if source is None:
            return None
        index_state = "ready"
        existing_chunks = await store.chunks_for_source(source.id)
        chunks = len(existing_chunks)
        current_model = getattr(getattr(self.knowledge, "embedder", None), "model", None)
        needs_index = not existing_chunks or any(
            chunk.embedding_model != current_model for chunk in existing_chunks
        )
        if source.status == "live" and needs_index:
            try:
                chunks = await self.knowledge.ensure_source_index(source.id)
            except Exception:  # noqa: BLE001 - primary source is durable; index stays pending
                index_state = "pending"
        # Rejection can race an unlocked receipt repair while embedding is in flight. The store
        # conditionally refuses terminal-source chunk writes; refresh the primary record so the
        # receipt response also reports that terminal lifecycle rather than a stale live snapshot.
        refreshed = await store.get_source(source.id)
        if refreshed is not None:
            source = refreshed
        if source.status != "live":
            chunks = 0
            index_state = "ready"
        self.log.info(
            "meeting_capture_reconciled",
            source_id=source.id,
            source_status=source.status,
            index_state=index_state,
        )
        return IngestResult(
            action="duplicate",
            source_id=source.id,
            chunks=chunks,
            review_status=source.review_status,
            title=source.title,
            index_state=index_state,
            source_status=source.status,
        )

    async def _ingest(
        self,
        text: str,
        title: str | None,
        *,
        capture_id: str,
        source_session_id: int | None,
        project_id: int | None,
    ) -> IngestResult:
        # Quarantine the transcript UNREVIEWED — untrusted audio content, not trusted input.
        # ``quarantine=True`` is per-ingest rather than a shared mutable mode, so captures in two
        # workspaces cannot accidentally change one another's trust level.
        origin = self._origin(
            capture_id,
            source_session_id=source_session_id,
            project_id=project_id,
        )
        try:
            result = await self.knowledge.ingest(
                text=text,
                title=title or "Meeting note",
                created_by="user",
                source_session_id=source_session_id,
                project_id=project_id,
                origin_override=origin,
                quarantine=True,
            )
        except Exception:
            # The source row is the durable commit point; embedding/chunks are rebuildable. If
            # failure happened after that row committed, report the exact source as index-pending
            # instead of inviting a duplicate recording. Failures before commit still propagate.
            existing = await self._existing_result(
                capture_id,
                source_session_id=source_session_id,
                project_id=project_id,
            )
            if existing is None:
                raise
            result = existing
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
                        title=result.title or "Meeting note",
                        created_by="user",
                        local_path=self.knowledge.knowledge_dir / source.markdown_path,
                        sensitivity="quarantined",
                        project_id=None,
                    )
            except Exception:  # noqa: BLE001 - artifact bookkeeping must never fail a capture
                self.log.warning("meeting_artifact_register_failed", source_id=result.source_id)
        return result
