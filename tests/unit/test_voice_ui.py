"""Full browser voice (Phase 15.5 Task 6) — the SAFE seams, keyless.

Browser-captured audio runs a voice turn through the SAME session (and thus the SAME
VoiceApprover — the screen stays the only approval surface); TTS playback synthesizes ONLY the
masked+capped safe caption, so a secret can never be voiced; status is honest (enabled/reason/
providers/playback). Live browser mic + audio playback is a manual Checkpoint-J2 step; here we pin
the endpoints, the safety framing, and the client structure with fakes."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from jarvis.config import load_config
from jarvis.ui.auth import SESSION_COOKIE, AuthManager
from jarvis.ui.server import STATIC_DIR, create_app
from jarvis.ui.voice import UiVoice
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


def test_status_on_reports_providers_and_playback(tmp_path: Path) -> None:
    v, _sess, _tts = _wired()
    client, auth = _client(tmp_path, voice=v)
    s = client.get("/api/voice/status", headers=_hdr(auth)).json()
    assert s["enabled"] is True and s["stt"] == "openai" and s["tts"] == "openai"
    assert s["playback"] is True and s["reason"] == ""


# --- browser utterance routes through the same session (screen-only approval) ---
def test_utterance_feeds_the_voice_session(tmp_path: Path) -> None:
    v, sess, _tts = _wired()
    client, auth = _client(tmp_path, voice=v)
    r = client.post("/api/voice/utterance", content=b"webm-audio-bytes",
                    headers=_hdr(auth, post=True))
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
    r = client.post("/api/voice/utterance?mode=dictation", content=b"webm-audio-bytes",
                    headers=_hdr(auth, post=True))
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
    assert "mode === \"dictation\"" in js and "cancelCapture" in js


def test_talk_button_and_playback_wired_in_shell() -> None:
    app_js = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    index = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    assert "toggleTalk" in app_js and "playCaption" in app_js  # browser push-to-talk + playback
    assert 'id="st-play"' in index and "setPlayback" in app_js  # the spoken-replies toggle
    assert "/api/voice/listen" not in app_js  # the server-mic path is no longer the Talk button
