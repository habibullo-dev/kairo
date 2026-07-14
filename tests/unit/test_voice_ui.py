"""Full browser voice (Phase 15.5 Task 6) — the SAFE seams, keyless.

Browser-captured audio runs a voice turn through the SAME session (and thus the SAME
VoiceApprover — the screen stays the only approval surface); TTS playback synthesizes ONLY the
masked+capped safe caption, so a secret can never be voiced; status is honest (enabled/reason/
providers/playback). Live browser mic + audio playback is a manual Checkpoint-J2 step; here we pin
the endpoints, the safety framing, and the client structure with fakes."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

from jarvis.config import load_config
from jarvis.core.execution import ExecutionContext
from jarvis.ui.auth import SESSION_COOKIE, AuthManager
from jarvis.ui.server import STATIC_DIR, WORKSPACE_HEADER, create_app
from jarvis.ui.voice import UiVoice
from jarvis.voice import FakeCapture, MeetingCapture
from jarvis.voice.protocols import FakeTranscriber


class _FakeSession:
    def __init__(self) -> None:
        self.audio: bytes | None = None
        self.stt = FakeTranscriber(scripted=["dictated words"])

    async def handle_audio(self, audio: bytes):
        self.audio = audio
        return object() if audio else None  # a TurnResult stand-in (None ⇒ no turn)


class _FakeListener:
    def __init__(self, session: _FakeSession) -> None:
        self.session = session


class _FakeTTS:
    def __init__(self) -> None:
        self.got: str | None = None

    async def synthesize(self, text: str) -> bytes:
        self.got = text
        return b"FAKE-AUDIO-BYTES"


def _client(tmp_path: Path, *, voice: UiVoice | None):
    cfg = load_config(root=tmp_path, env_file=None)
    auth = AuthManager(token="tok")
    app = create_app(cfg, auth=auth)
    app.state.voice = voice
    return TestClient(app, base_url="http://127.0.0.1"), auth


def _wired() -> tuple[UiVoice, _FakeSession, _FakeTTS]:
    sess, tts = _FakeSession(), _FakeTTS()
    v = UiVoice(listener=_FakeListener(sess), tts=tts, stt_name="openai", tts_name="openai")
    return v, sess, tts


def _hdr(auth: AuthManager, *, post: bool = False) -> dict[str, str]:
    h = {"cookie": f"{SESSION_COOKIE}={auth.mint_session()}"}
    if post:
        h["origin"] = "http://127.0.0.1"
    return h


# --- status honesty --------------------------------------------------------
def test_status_off_reports_a_reason(tmp_path: Path) -> None:
    client, auth = _client(tmp_path, voice=None)
    s = client.get("/api/voice/status", headers=_hdr(auth)).json()
    assert s["enabled"] is False and s["reason"] and s["playback"] is False
    assert s["meeting_recording"] is False
    assert s["meeting_recording_epoch"] is None
    assert s["meeting_revision"] == 0 and s["meeting_recording_revision"] == 0


def test_status_on_reports_providers_and_playback(tmp_path: Path) -> None:
    v, _sess, _tts = _wired()
    client, auth = _client(tmp_path, voice=v)
    s = client.get("/api/voice/status", headers=_hdr(auth)).json()
    assert s["enabled"] is True and s["stt"] == "openai" and s["tts"] == "openai"
    assert s["playback"] is True and s["reason"] == ""
    assert s["meeting_available"] is False and s["meeting_reason"]
    assert s["meeting_recording"] is False
    assert s["meeting_recording_epoch"] is None
    assert s["meeting_revision"] == 0 and s["meeting_recording_revision"] == 0


def test_workspace_status_keeps_local_phase_and_global_mic_signal_separate(
    tmp_path: Path,
) -> None:
    voice, _session, _tts = _wired()
    voice.meeting = SimpleNamespace(state="transcribing")
    voice.capture = object()
    client, auth = _client(tmp_path, voice=None)
    workspace = SimpleNamespace(voice=voice)
    client.app.state.workspaces = SimpleNamespace(
        resolve=lambda **_kw: workspace,
        meeting_recording_active=True,
        meeting_recording_epoch="test-process",
        meeting_recording_revision=9,
    )
    response = client.get(
        "/api/voice/status",
        headers={**_hdr(auth), WORKSPACE_HEADER: "w" * 24},
    )
    assert response.status_code == 200
    assert response.json()["meeting"] == "transcribing"
    assert response.json()["meeting_recording"] is True
    assert response.json()["meeting_recording_epoch"] == "test-process"
    assert response.json()["meeting_recording_revision"] == 9


class _MeetingKnowledge:
    def __init__(self) -> None:
        self.bound_unattended = False
        self.ingested: list[dict] = []

    async def ingest(self, **kw):
        self.ingested.append(kw)
        return SimpleNamespace(
            action="ingested",
            source_id=73,
            chunks=1,
            review_status="unreviewed" if kw.get("quarantine") else "reviewed",
            title=kw.get("title"),
        )


def test_meeting_note_route_preserves_workspace_scope_and_returns_source(tmp_path: Path) -> None:
    knowledge = _MeetingKnowledge()
    meeting = MeetingCapture(knowledge, FakeTranscriber(scripted=["Project standup notes"]))
    voice = UiVoice(meeting=meeting, capture=FakeCapture(scripted=[b"wav-audio"]))
    client, auth = _client(tmp_path, voice=None)
    owner = auth.mint_session()
    workspace = SimpleNamespace(
        voice=voice,
        context=ExecutionContext(session_id=42, project_id=7),
        context_revision=3,
    )

    @asynccontextmanager
    async def voice_activity(_workspace, *, expected_context=None, expected_revision=None):
        assert expected_context == workspace.context
        assert expected_revision == workspace.context_revision
        yield

    @asynccontextmanager
    async def meeting_receipt_activity(_receipt_key):
        yield

    class _Lease:
        def __init__(self, source):
            self.source = source

        async def capture_utterance(self):
            return await self.source.capture_utterance()

        async def release(self):
            return None

    async def reserve_server_capture(source, *, meeting=False):
        assert meeting is True
        return _Lease(source)

    client.app.state.workspaces = SimpleNamespace(
        resolve=lambda **_kw: workspace,
        voice_activity=voice_activity,
        transition_lock=asyncio.Lock(),
        claim_matches=lambda candidate, context, revision: (
            candidate is workspace
            and context == workspace.context
            and revision == workspace.context_revision
        ),
        meeting_receipt_activity=meeting_receipt_activity,
        reserve_server_capture=reserve_server_capture,
    )
    response = client.post(
        "/api/voice/meeting",
        json={
            "title": "  Standup  ",
            "consent": True,
            "capture_id": "123e4567-e89b-42d3-a456-426614174000",
            "expected_context": {
                "session_id": 42,
                "project_id": 7,
                "context_revision": 3,
            },
        },
        headers={
            **_hdr(auth, post=True),
            "cookie": f"{SESSION_COOKIE}={owner}",
            WORKSPACE_HEADER: "w" * 24,
        },
    )

    assert response.status_code == 200
    assert response.json() == {
        "ok": True,
        "review_status": "unreviewed",
        "source_id": 73,
        "title": "Standup",
        "index_state": "ready",
        "source_status": "live",
    }
    assert knowledge.ingested[0]["project_id"] == 7
    assert knowledge.ingested[0]["source_session_id"] == 42
    assert knowledge.ingested[0]["quarantine"] is True


def test_meeting_route_is_unavailable_when_only_generic_voice_is_wired(tmp_path: Path) -> None:
    voice, _session, _tts = _wired()
    client, auth = _client(tmp_path, voice=voice)
    response = client.post(
        "/api/voice/meeting", json={"title": "No backend"}, headers=_hdr(auth, post=True)
    )
    assert response.status_code == 503


def test_meeting_route_requires_consent_before_opening_microphone(tmp_path: Path) -> None:
    knowledge = _MeetingKnowledge()
    capture = FakeCapture(scripted=[b"must-not-be-read"])
    voice = UiVoice(
        meeting=MeetingCapture(knowledge, FakeTranscriber(scripted=["must not run"])),
        capture=capture,
    )
    client, auth = _client(tmp_path, voice=voice)
    response = client.post(
        "/api/voice/meeting", json={"title": "No consent"}, headers=_hdr(auth, post=True)
    )
    assert response.status_code == 422
    assert capture.calls == 0
    assert knowledge.ingested == []


def test_meeting_route_rejects_invalid_capture_receipt_before_microphone(tmp_path: Path) -> None:
    knowledge = _MeetingKnowledge()
    capture = FakeCapture(scripted=[b"must-not-be-read"])
    voice = UiVoice(
        meeting=MeetingCapture(knowledge, FakeTranscriber(scripted=["must not run"])),
        capture=capture,
    )
    client, auth = _client(tmp_path, voice=voice)
    response = client.post(
        "/api/voice/meeting",
        json={"title": "Bad receipt", "consent": True, "capture_id": "not-a-uuid"},
        headers=_hdr(auth, post=True),
    )
    assert response.status_code == 422
    assert capture.calls == 0
    assert knowledge.ingested == []


# --- browser utterance routes through the same session (screen-only approval) ---
def test_utterance_feeds_the_voice_session(tmp_path: Path) -> None:
    v, sess, _tts = _wired()
    client, auth = _client(tmp_path, voice=v)
    r = client.post(
        "/api/voice/utterance", content=b"webm-audio-bytes", headers=_hdr(auth, post=True)
    )
    assert r.status_code == 200 and r.json()["ran"] is True
    assert sess.audio == b"webm-audio-bytes"  # fed to the SAME session (same loop + VoiceApprover)
    empty = client.post("/api/voice/utterance", content=b"", headers=_hdr(auth, post=True))
    assert empty.status_code == 400  # no audio ⇒ refused, never an empty turn


def test_utterance_unavailable_without_voice(tmp_path: Path) -> None:
    client, auth = _client(tmp_path, voice=None)
    r = client.post("/api/voice/utterance", content=b"x", headers=_hdr(auth, post=True))
    assert r.status_code == 503


def test_dictation_returns_editable_text_without_starting_a_voice_turn(tmp_path: Path) -> None:
    v, sess, _tts = _wired()
    client, auth = _client(tmp_path, voice=v)
    r = client.post(
        "/api/voice/utterance?mode=dictation",
        content=b"webm-audio-bytes",
        headers=_hdr(auth, post=True),
    )
    assert r.status_code == 200 and r.json() == {"ok": True, "transcript": "dictated words"}
    assert sess.audio is None  # dictation is review-only: it never calls VoiceSession.handle_audio
    bad = client.post(
        "/api/voice/utterance?mode=unknown", content=b"x", headers=_hdr(auth, post=True)
    )
    assert bad.status_code == 422


# --- TTS playback synthesizes ONLY the safe caption ------------------------
def test_tts_masks_secrets_before_synthesis(tmp_path: Path) -> None:
    v, _sess, tts = _wired()
    client, auth = _client(tmp_path, voice=v)
    poison = "the key is sk-livedeadbeef123456 keep it"
    r = client.post("/api/voice/tts", json={"text": poison}, headers=_hdr(auth, post=True))
    assert r.status_code == 200 and r.content == b"FAKE-AUDIO-BYTES"
    # the TTS provider NEVER received the secret — it was masked server-side before synthesis
    assert "sk-livedeadbeef123456" not in tts.got and "[redacted]" in tts.got


async def test_synthesize_caption_caps_length() -> None:
    v, _sess, tts = _wired()
    await v.synthesize_caption("x" * 5000)
    assert len(tts.got) <= UiVoice.TTS_MAX_CHARS  # a browser can't push a long string off-device


# --- client structure (browser mic + playback + permission/failure states) ---
def test_client_voice_module_is_safe_and_complete() -> None:
    js = (STATIC_DIR / "ui" / "voice.js").read_text(encoding="utf-8")
    assert "getUserMedia" in js and "MediaRecorder" in js  # browser capture
    assert "/api/voice/utterance" in js and "/api/voice/tts" in js
    assert "NotAllowedError" in js  # permission-denied handling
    assert "innerHTML" not in js and " onclick=" not in js  # moves audio + fixed states only
    assert "/api/approvals" not in js and "/api/turn" not in js  # never approves/commits
    assert 'mode === "dictation"' in js and "cancelCapture" in js


def test_talk_button_and_playback_wired_in_shell() -> None:
    app_js = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    index = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    assert "toggleTalk" in app_js and "playCaption" in app_js  # browser push-to-talk + playback
    assert 'id="st-play"' in index and "setPlayback" in app_js  # the spoken-replies toggle
    assert "/api/voice/listen" not in app_js  # the server-mic path is no longer the Talk button
