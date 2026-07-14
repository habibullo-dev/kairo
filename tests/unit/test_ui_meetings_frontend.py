"""Truthfulness and interaction pins for the short meeting-note browser surface."""

from __future__ import annotations

from jarvis.ui.server import STATIC_DIR

MEETINGS_JS = (STATIC_DIR / "screens" / "meetings.js").read_text(encoding="utf-8")
APP_JS = (STATIC_DIR / "app.js").read_text(encoding="utf-8")


def test_meeting_screen_describes_the_actual_capture_boundary() -> None:
    assert "short spoken note" in MEETINGS_JS
    assert "30-second" in MEETINGS_JS
    assert "stops after silence" in MEETINGS_JS
    assert "workstation microphone" in MEETINGS_JS
    assert "full meeting" not in MEETINGS_JS
    assert "Start recording this meeting" not in MEETINGS_JS


def test_meeting_screen_requires_consent_and_tracks_each_phase() -> None:
    assert 'id="mtg-consent"' in MEETINGS_JS
    assert "everyone present consents" in MEETINGS_JS
    assert "consent: true" in MEETINGS_JS
    for state in ("requesting", "recording", "transcribing", "saving"):
        assert state in MEETINGS_JS
    assert "inFlight" in MEETINGS_JS
    assert "r.data.ok" in MEETINGS_JS
    assert "try" in MEETINGS_JS and "catch" in MEETINGS_JS
    assert "confirm(" not in MEETINGS_JS
    assert "crypto.randomUUID()" in MEETINGS_JS
    assert "sessionStorage" in MEETINGS_JS
    assert "capture_id" in MEETINGS_JS


def test_meeting_screen_uses_exact_capability_and_links_the_created_source() -> None:
    assert "meeting_available" in MEETINGS_JS
    assert "source_id" in MEETINGS_JS
    assert 'href = "#vault"' in MEETINGS_JS


def test_meeting_lifecycle_has_a_dedicated_visible_event_channel() -> None:
    assert 'msg.kind === "meeting_state"' in APP_JS
    assert 'msg.kind === "meeting_recording"' in APP_JS
    assert 'busEmit("meeting_state"' in APP_JS
    assert 'busOn("meeting_state"' in MEETINGS_JS
    assert "[data-meeting-rec-dot]" in APP_JS
    assert "meeting-recording-status" in APP_JS


def test_meeting_status_failure_preserves_the_last_authoritative_mic_state() -> None:
    assert "const voiceStatus = await api.get" in APP_JS
    assert "meetingEventGeneration" in APP_JS
    assert "meetingStatusIsFresh" in APP_JS
    assert "meetingRecordingStatusIsFresh" in APP_JS
    assert "previousVoice.meeting" in APP_JS
    assert "previousVoice.meeting_recording" in APP_JS
    assert "meeting_recording_revision" in APP_JS


def test_delayed_meeting_render_cannot_replace_a_newer_route() -> None:
    assert "renderGeneration" in MEETINGS_JS
    assert 'api.state?.route !== "meetings"' in MEETINGS_JS
    assert "Checking voice provider and microphone availability" in MEETINGS_JS
    assert "await api.voiceStatus()" in MEETINGS_JS


def test_meeting_privacy_and_busy_copy_do_not_overpromise() -> None:
    assert "Transcript text may be sent to configured Knowledge providers" in MEETINGS_JS
    assert "Transcription stays on this workstation" not in MEETINGS_JS
    assert "Checking the capture receipt and preparing audio" in MEETINGS_JS
    assert 'id="mtg-controls"' in MEETINGS_JS
    assert 'id="mtg-capture-card" aria-busy=' not in MEETINGS_JS
    assert "voice_status" in MEETINGS_JS
    assert "Waiting for the authenticated workspace context" in MEETINGS_JS


def test_meeting_receipt_scope_matches_server_and_clear_uses_frozen_key() -> None:
    assert "return `kairo:meeting-capture:${scope}`" in MEETINGS_JS
    assert "session-${session}" not in MEETINGS_JS
    assert "clearCaptureReceipt(receipt)" in MEETINGS_JS
    assert "sessionStorage.removeItem(receipt.key)" in MEETINGS_JS
