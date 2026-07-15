// Browser voice controller (Phase 15.5). Push-to-talk from the BROWSER mic (getUserMedia +
// MediaRecorder) → POST /api/voice/utterance → the SAME server voice session (STT → framed
// untrusted turn → safe caption) through the unchanged VoiceApprover. Optional playback fetches
// the SAFE caption's audio from /api/voice/tts and plays it. This module NEVER approves or commits
// a risky action — the screen (the Gate modal) remains the only approval surface; voice only
// PREPARES a turn. It also never renders raw content: it moves audio + fixed state strings only.

import { readMigrated, writeStored } from "./storage.js";

const PLAYBACK_KEY = "kira:voice:playback";
const LEGACY_PLAYBACK_KEYS = ["kairo:voice:playback"];

let _rec = null;
let _stream = null;
let _chunks = [];
let _audio = null;
let _discard = false;
let _captureActive = false;
let _captureRevision = 0;
let _requestController = null;
let _playbackRevision = 0;

// Is the browser capable of capturing audio at all? (A remote/insecure origin or an old browser
// can't — we say so plainly rather than failing silently.)
export function canCapture() {
  return !!(navigator.mediaDevices && navigator.mediaDevices.getUserMedia && window.MediaRecorder);
}

export function playbackOn() {
  return readMigrated("local", PLAYBACK_KEY, LEGACY_PLAYBACK_KEYS) === "1";
}

export function setPlayback(on) {
  writeStored("local", PLAYBACK_KEY, on ? "1" : "0");
}

export function recording() {
  return !!(_rec && _rec.state === "recording");
}

// Stop capture without sending it anywhere. Dictation is review-first, and a voice turn remains
// cancellable before any transcript reaches the server. The recorder's stop handler releases the
// mic and moves to idle, but skips the audio POST entirely.
export function cancelCapture(onState) {
  if (!_captureActive && !recording() && !_requestController) return false;
  const wasRecording = recording();
  _captureRevision += 1;
  // MediaRecorder dispatches `stop` asynchronously. Keep this operation occupied until that
  // callback has released its stream so a replacement capture cannot be clobbered by cleanup
  // from the retired recorder.
  _captureActive = wasRecording;
  _discard = true;
  _requestController?.abort();
  _requestController = null;
  if (wasRecording) _rec.stop();
  else if (_stream) {
    _stream.getTracks().forEach((track) => track.stop());
    _stream = null;
  }
  onState?.("idle");
  return true;
}

// Start browser capture or stop+submit an utterance. Conversation retains the existing safe
// VoiceSession path; dictation asks the same endpoint only to transcribe, then returns the
// finalized user-owned transcript for editing. No client path can approve or resolve an action.
export async function toggleTalk({
  mode = "conversation", headers = {}, onState, onTranscript, isCurrent = () => true,
} = {}) {
  if (recording()) { _rec.stop(); return; }  // second press: stop + send
  if (_captureActive) return;
  if (!canCapture()) {
    onState("error", "This browser can't record audio (needs a secure, local origin).");
    return;
  }
  const revision = ++_captureRevision;
  const current = () => revision === _captureRevision && isCurrent();
  _captureActive = true;
  try {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true });  // permission prompt
    if (!current()) {
      stream.getTracks().forEach((track) => track.stop());
      return;
    }
    _stream = stream;
  } catch (e) {
    if (!current()) return;
    _captureActive = false;
    const denied = e && (e.name === "NotAllowedError" || e.name === "SecurityError");
    onState("error", denied ? "Microphone permission denied." : "No microphone available.");
    return;
  }
  _discard = false;
  _chunks = [];
  _rec = new MediaRecorder(_stream);
  _rec.addEventListener("dataavailable", (e) => { if (e.data && e.data.size) _chunks.push(e.data); });
  _rec.addEventListener("stop", async () => {
    if (_stream) _stream.getTracks().forEach((t) => t.stop());  // release the mic (indicator off)
    const discard = _discard;
    _discard = false;
    const type = (_rec && _rec.mimeType) || "audio/webm";
    const blob = new Blob(_chunks, { type });
    _rec = null; _stream = null; _chunks = [];
    if (discard || !current()) {
      _captureActive = false;
      if (current()) onState("idle");
      return;
    }
    if (!blob.size) { _captureActive = false; onState("idle"); return; }
    onState("transcribing");
    try {
      // Same-origin POST: the browser attaches the loopback Origin the CSRF check requires.
      const path = mode === "dictation" ? "/api/voice/utterance?mode=dictation" : "/api/voice/utterance";
      const controller = new AbortController();
      _requestController = controller;
      if (!current()) { controller.abort(); return; }
      const res = await fetch(path, {
        method: "POST", headers: { ...headers, "content-type": type }, body: blob,
        signal: controller.signal,
      });
      if (!current()) return;
      if (!res.ok) { onState("error", "Voice capture failed. Try again."); return; }
      if (mode === "dictation") {
        const data = await res.json().catch(() => ({}));
        if (!current()) return;
        if (typeof data.transcript === "string" && data.transcript.trim()) onTranscript?.(data.transcript);
        onState("idle");
      }
    } catch {
      if (current()) onState("error", "Voice capture failed. Try again.");
    } finally {
      if (revision === _captureRevision) {
        _requestController = null;
        _captureActive = false;
      }
    }
  });
  _rec.start();
  onState("listening");
}

// Play the SAFE caption for a completed voice reply, if the user enabled playback. The server
// masks + caps the text before TTS, and returns 204 for local/subtitle mode (⇒ captions stay text).
export function stopCaption() {
  _playbackRevision += 1;
  if (!_audio) return false;
  _audio.pause();
  _audio.currentTime = 0;
  return true;
}

export async function playCaption(
  text, headers = {}, onState = () => {}, isCurrent = () => true,
) {
  if (!playbackOn() || !text) return;
  const revision = ++_playbackRevision;
  const current = () => revision === _playbackRevision && isCurrent();
  try {
    const res = await fetch("/api/voice/tts",
      { method: "POST", headers: { ...headers, "content-type": "application/json" }, body: JSON.stringify({ text }) });
    if (!current()) return;
    if (res.status !== 200) return;  // 204 (no cloud TTS) or an error → nothing to play
    const url = URL.createObjectURL(await res.blob());
    if (!current()) { URL.revokeObjectURL(url); return; }
    if (!_audio) _audio = new Audio();
    _audio.onended = () => { if (current()) onState("idle"); };
    _audio.src = url;
    onState("speaking");
    _audio.play().catch(() => { if (current()) onState("idle"); });
  } catch { /* playback is best-effort; the safe caption is always on the screen */ }
}
