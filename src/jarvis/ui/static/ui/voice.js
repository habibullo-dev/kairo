// Browser voice controller (Phase 15.5). Push-to-talk from the BROWSER mic (getUserMedia +
// MediaRecorder) → POST /api/voice/utterance → the SAME server voice session (STT → framed
// untrusted turn → safe caption) through the unchanged VoiceApprover. Optional playback fetches
// the SAFE caption's audio from /api/voice/tts and plays it. This module NEVER approves or commits
// a risky action — the screen (the Gate modal) remains the only approval surface; voice only
// PREPARES a turn. It also never renders raw content: it moves audio + fixed state strings only.

let _rec = null;
let _stream = null;
let _chunks = [];
let _audio = null;
let _discard = false;

// Is the browser capable of capturing audio at all? (A remote/insecure origin or an old browser
// can't — we say so plainly rather than failing silently.)
export function canCapture() {
  return !!(navigator.mediaDevices && navigator.mediaDevices.getUserMedia && window.MediaRecorder);
}

export function playbackOn() {
  try { return localStorage.getItem("kairo:voice:playback") === "1"; } catch { return false; }
}

export function setPlayback(on) {
  try { localStorage.setItem("kairo:voice:playback", on ? "1" : "0"); } catch { /* disabled */ }
}

export function recording() {
  return !!(_rec && _rec.state === "recording");
}

// Stop capture without sending it anywhere. Dictation is review-first, and a voice turn remains
// cancellable before any transcript reaches the server. The recorder's stop handler releases the
// mic and moves to idle, but skips the audio POST entirely.
export function cancelCapture(onState) {
  if (!recording()) return false;
  _discard = true;
  _rec.stop();
  onState("idle");
  return true;
}

// Start browser capture or stop+submit an utterance. Conversation retains the existing safe
// VoiceSession path; dictation asks the same endpoint only to transcribe, then returns the
// finalized user-owned transcript for editing. No client path can approve or resolve an action.
export async function toggleTalk({ mode = "conversation", headers = {}, onState, onTranscript } = {}) {
  if (recording()) { _rec.stop(); return; }  // second press: stop + send
  if (!canCapture()) {
    onState("error", "This browser can't record audio (needs a secure, local origin).");
    return;
  }
  try {
    _stream = await navigator.mediaDevices.getUserMedia({ audio: true });  // permission prompt
  } catch (e) {
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
    if (discard) { onState("idle"); return; }
    if (!blob.size) { onState("idle"); return; }
    onState("transcribing");
    try {
      // Same-origin POST: the browser attaches the loopback Origin the CSRF check requires.
      const path = mode === "dictation" ? "/api/voice/utterance?mode=dictation" : "/api/voice/utterance";
      const res = await fetch(path, { method: "POST", headers: { ...headers, "content-type": type }, body: blob });
      if (!res.ok) { onState("error", "Voice capture failed. Try again."); return; }
      if (mode === "dictation") {
        const data = await res.json().catch(() => ({}));
        if (typeof data.transcript === "string" && data.transcript.trim()) onTranscript?.(data.transcript);
        onState("idle");
      }
    } catch {
      onState("error", "Voice capture failed. Try again.");
    }
  });
  _rec.start();
  onState("listening");
}

// Play the SAFE caption for a completed voice reply, if the user enabled playback. The server
// masks + caps the text before TTS, and returns 204 for local/subtitle mode (⇒ captions stay text).
export function stopCaption() {
  if (!_audio) return false;
  _audio.pause();
  _audio.currentTime = 0;
  return true;
}

export async function playCaption(text, headers = {}, onState = () => {}) {
  if (!playbackOn() || !text) return;
  try {
    const res = await fetch("/api/voice/tts",
      { method: "POST", headers: { ...headers, "content-type": "application/json" }, body: JSON.stringify({ text }) });
    if (res.status !== 200) return;  // 204 (no cloud TTS) or an error → nothing to play
    const url = URL.createObjectURL(await res.blob());
    if (!_audio) _audio = new Audio();
    _audio.onended = () => onState("idle");
    _audio.src = url;
    onState("speaking");
    _audio.play().catch(() => onState("idle"));  // captions stay on screen if autoplay is blocked
  } catch { /* playback is best-effort; the safe caption is always on the screen */ }
}
