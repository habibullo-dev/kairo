// Kairo Workstation — shell core (Phase 8). WS + router + status + the approval flow.
// Carries NO safety logic: it renders and clicks. All enforcement is server-side (the nonce
// is minted only over the live socket after the modal is shown; the server validates every
// resolve). Per-screen rendering lives in ./screens/*.js.

import { render as renderDaily, onEvent as dailyOnEvent } from "./screens/daily.js";
import { render as renderProjects } from "./screens/projects.js";
import { render as renderStudio, onEvent as studioOnEvent } from "./screens/studio.js";
import { render as renderGate } from "./screens/gate.js";
import { render as renderVault } from "./screens/vault.js";
import { render as renderTasks } from "./screens/tasks.js";
import { render as renderMemory } from "./screens/memory.js";
import { render as renderHub } from "./screens/hub.js";
import { render as renderCosts } from "./screens/costs.js";
import { render as renderLab } from "./screens/lab.js";
import { render as renderMeetings } from "./screens/meetings.js";
import { render as renderTrace } from "./screens/trace.js";
import { render as renderSettings } from "./screens/settings.js";
import { get as getTheme, initTheme, setTheme } from "./ui/theme.js";
import { initKeys, clearScope, pushEscape } from "./ui/keys.js";
import { emit as busEmit, on as busOn } from "./ui/bus.js";
import { init as initPalette } from "./ui/palette.js";

const state = {
  chat: [],            // Daily conversation items {role, text} | {tool, resolution}
  pending: new Map(),  // decision_id -> approval payload (+ nonce once minted)
  runner: {},          // last /api/runner status
  voice: { enabled: false },
  trace: [],           // raw events (Debug/Trace)
  notices: [],         // background job/reminder/digest notices (Phase 9)
  route: "daily",
  routeArgs: [],       // positional hash args after the screen name (#workspace/{id})
};

// --- tiny API helper (same-origin; cookie carried automatically) ---
export const api = {
  state,
  async get(path) {
    const r = await fetch(path, { headers: { "accept": "application/json" } });
    return r.ok ? r.json() : null;
  },
  async post(path, body) {
    const r = await fetch(path, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(body || {}),
    });
    return { ok: r.ok, status: r.status, data: await r.json().catch(() => ({})) };
  },
  // Re-open the amber approval modal for the oldest pending item (Daily "Review" button).
  reviewPending() {
    const next = [...state.pending.values()][0];
    if (next) { next._shown = false; showTopApproval(); }
  },
};

// --- WebSocket: heartbeat + surface state + event stream ---
let ws = null;
let mounted = new Set();

function wsSend(obj) { if (ws && ws.readyState === 1) ws.send(JSON.stringify(obj)); }

function setSurface(name, on) {
  if (on && !mounted.has(name)) { mounted.add(name); wsSend({ type: "surface", surface: name, mounted: true }); }
  if (!on && mounted.has(name)) { mounted.delete(name); wsSend({ type: "surface", surface: name, mounted: false }); }
}

function connect() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  ws = new WebSocket(`${proto}://${location.host}/ws`);
  ws.onopen = () => {
    wsSend({ type: "hello", surfaces: [...mounted] });
    setInterval(() => wsSend({ type: "heartbeat" }), 5000);
  };
  ws.onmessage = (e) => handleMessage(JSON.parse(e.data));
  ws.onclose = () => setTimeout(connect, 1500); // best-effort reconnect
}

function handleMessage(msg) {
  if (msg.type === "approval") { onApproval(msg); return; }
  if (msg.type === "approval_nonce") { onNonce(msg); return; }
  if (msg.kind && msg.kind.startsWith("orchestration_")) {
    busEmit("orchestration", msg);
    studioOnEvent(state, msg);        // update the in-flight run panel
    refreshIfActive("studio");
    if (msg.kind === "orchestration_completed") pollStatus();  // refresh spend/busy chips
    return;
  }
  if (msg.kind === "event") { onEvent(msg); return; }
  if (msg.kind === "notice") { onNotice(msg.notice); return; }
  if (msg.kind === "voice") { onVoice(msg); return; }
  if (msg.kind === "voice_state") { onVoiceState(msg.state); return; }
  if (msg.kind === "turn_cancelled" || msg.kind === "turn_error") {
    if (state.runner) state.runner.turn_busy = false;  // settle: the turn ended
    const text = msg.kind === "turn_cancelled" ? "— turn cancelled —" : `— error: ${msg.error} —`;
    state.chat.push({ role: "assistant", text });
    refreshIfActive("daily");
    renderRunnerState();
  }
}

// Background notices (job/reminder/digest) reach the browser here — the calm, non-modal
// surface (never an attention grab). A digest notice quietly refreshes Daily's Briefing.
function onNotice(notice) {
  if (!notice) return;
  state.notices.unshift(notice);
  if (state.notices.length > 50) state.notices.pop();
  busEmit("notice", notice);
  if (notice.kind === "digest") refreshIfActive("daily");
}

// Voice round-trip, made visible in Daily (one heard bubble + one safe caption). The reply
// text is the renderer's post-privacy output — the UI never sees a raw answer or a payload.
function onVoice(msg) {
  state.chat.push(msg.role === "heard" ? { role: "heard", text: msg.text } : { role: "assistant", text: msg.text });
  refreshIfActive("daily");
}

// Read-only voice state pill (idle/listening/transcribing/thinking/speaking) — content-free.
const VOICE_LABELS = {
  listening: "🎤 Listening…", capturing: "🎤 Capturing…", transcribing: "🎤 Transcribing…",
  thinking: "🎤 Thinking…", speaking: "🎤 Speaking…",
};
function onVoiceState(s) {
  state.voice.listening = s;
  const voiceEl = document.getElementById("st-voice");
  if (voiceEl) voiceEl.textContent = s;
  const mic = document.getElementById("st-mic");
  if (mic) mic.textContent = VOICE_LABELS[s] || "🎤 Talk";
}

function onEvent(evt) {
  state.trace.push(evt);
  if (state.trace.length > 500) state.trace.shift();
  busEmit("event", evt);
  // A completed turn settles the runner state immediately — don't wait for the next poll,
  // or the Daily card lingers on "working" after the turn (incl. a denied one) ends.
  if (evt.type === "turn_completed" && state.runner) state.runner.turn_busy = false;
  dailyOnEvent(state, evt);
  refreshIfActive("daily");
  refreshIfActive("trace");
  if (evt.type === "turn_completed") { renderRunnerState(); pollStatus(); }
}

// --- approvals: the priority attention surface ---
let _approvalEsc = null; // unregister fn for the Escape-closes-modal binding (keys.js)
function onApproval(msg) {
  state.pending.set(msg.decision_id, { ...msg, nonce: null });
  updateGateBadge();
  showTopApproval();
  refreshIfActive("gate");
}

function showTopApproval() {
  const next = [...state.pending.values()].find((p) => !p._shown);
  if (!next) return;
  const overlay = document.getElementById("overlay");
  if (overlay.classList.contains("show")) return; // one attention surface at a time
  next._shown = true;
  document.getElementById("ap-kind").textContent =
    next.kind === "voice" ? "Confirm on screen (voice)" : "Approval required";
  document.getElementById("ap-tool").textContent = next.title ? `${next.tool} — ${next.title}` : next.tool;
  document.getElementById("ap-payload").textContent = JSON.stringify(next.input, null, 2);
  document.getElementById("ap-reason").textContent = next.reason || "";
  document.getElementById("ap-waiting").textContent = "Preparing secure confirmation…";
  document.getElementById("ap-spin").classList.add("show");
  for (const id of ["ap-approve", "ap-always"]) document.getElementById(id).disabled = true;
  // No "always" for voice (per-instance only) OR a non-persistable decision (tainted egress:
  // a private read happened this turn, so sending off-box must not become a standing grant).
  const noAlways = next.kind === "voice" || next.persistable === false;
  document.getElementById("ap-always").style.display = noAlways ? "none" : "";
  overlay.dataset.decision = next.decision_id;
  overlay.classList.add("show");
  if (_approvalEsc) _approvalEsc();
  _approvalEsc = pushEscape(hideApproval);           // Escape dismisses (leaves the item pending)
  setSurface("gate", true);                          // the screen is now watching
  wsSend({ type: "approval_shown", decision_id: next.decision_id }); // prove visibility ⇒ mint nonce
}

function onNonce(msg) {
  const p = state.pending.get(msg.decision_id);
  if (!p) return;
  p.nonce = msg.nonce;
  const overlay = document.getElementById("overlay");
  if (overlay.dataset.decision === msg.decision_id) {
    document.getElementById("ap-spin").classList.remove("show");
    document.getElementById("ap-waiting").textContent = "Confirm below to commit this action.";
    document.getElementById("ap-approve").disabled = false;
    document.getElementById("ap-always").disabled = false;
  }
}

async function resolveApproval(action) {
  const overlay = document.getElementById("overlay");
  const did = overlay.dataset.decision;
  const p = state.pending.get(did);
  if (!p) { hideApproval(); return; }
  if (action !== "deny" && !p.nonce) return; // can't approve without the minted nonce
  await api.post(`/api/approvals/${did}/resolve`, { nonce: p.nonce || "", action });
  state.pending.delete(did);
  hideApproval();
  updateGateBadge();
  refreshIfActive("gate");
  showTopApproval(); // surface the next pending approval, if any
}

function hideApproval() {
  if (_approvalEsc) { _approvalEsc(); _approvalEsc = null; }
  const overlay = document.getElementById("overlay");
  overlay.classList.remove("show");
  overlay.dataset.decision = "";
  if (state.route !== "gate") setSurface("gate", false); // stop advertising the screen
}

function updateGateBadge() {
  const badge = document.getElementById("gate-badge");
  const n = state.pending.size;
  badge.textContent = String(n);
  badge.classList.toggle("show", n > 0);
}

// --- router ---
const screens = {
  daily: renderDaily, projects: renderProjects, studio: renderStudio, gate: renderGate,
  vault: renderVault, tasks: renderTasks, memory: renderMemory, hub: renderHub,
  costs: renderCosts, lab: renderLab, meetings: renderMeetings, trace: renderTrace,
  settings: renderSettings,
};

function refreshIfActive(name) { if (state.route === name) renderRoute(); }

function renderRoute() {
  const container = document.getElementById("screen");
  // Own-property lookup only: state.route is hash-derived, so "#__proto__" etc. must fall
  // through to the safe unknown-route branch, never resolve an inherited Object member.
  const fn = Object.hasOwn(screens, state.route) ? screens[state.route] : null;
  if (fn) {
    fn(container, api, state.routeArgs || []);
    return;
  }
  // Unknown route — build with textContent, never interpolate the (hash-derived, so
  // attacker-influenceable) route name into innerHTML. CSP already blocks inline handlers;
  // this closes the injection smell outright.
  container.textContent = "";
  const h = document.createElement("h1");
  h.textContent = cap(state.route);
  const sub = document.createElement("div");
  sub.className = "sub";
  sub.textContent = "Unknown screen.";
  container.append(h, sub);
}

// Parse the hash into a screen name + positional args: "#workspace/12" -> {name:"workspace",
// args:["12"]}. Args let a screen deep-link (the project Workspace, T10) with no new route.
function parseHash() {
  const parts = location.hash.replace(/^#/, "").split("/").filter((s) => s !== "");
  return { name: parts[0] || "daily", args: parts.slice(1) };
}

function navigate() {
  const { name, args } = parseHash();
  if (state.route && state.route !== name) setSurface(state.route, false);
  state.route = name;
  state.routeArgs = args;
  clearScope();                                      // a screen's local keys never leak forward
  setSurface(name, true);
  if (name === "gate") setSurface("gate", true);
  for (const a of document.querySelectorAll(".rail a")) a.classList.toggle("active", a.dataset.screen === name);
  renderRoute();
}

function cap(s) { return s.charAt(0).toUpperCase() + s.slice(1); }

// --- status bar ---
// Write BOTH the status bar and the Daily current-activity card from the same settled
// state.runner — so they can never diverge (the "still working after deny" bug). Idempotent;
// safe to call whether or not the Daily card is mounted.
function renderRunnerState() {
  const s = state.runner || {};
  const busy = !!s.turn_busy;
  const setText = (id, t) => { const el = document.getElementById(id); if (el) el.textContent = t; };
  const setClass = (id, c) => { const el = document.getElementById(id); if (el) el.className = c; };
  const dotClass = "runner-dot" + (busy ? " busy" : "");
  // status bar
  setText("st-runner", busy ? "Kairo is working" : (s.runner_running ? "Kairo is idle" : "Kairo is paused"));
  setClass("runner-dot", dotClass);
  setText("st-turn", busy ? "working" : "ready");
  const stop = document.getElementById("st-stop"); if (stop) stop.style.display = s.runner_running ? "" : "none";
  const resume = document.getElementById("st-resume"); if (resume) resume.style.display = s.runner_running ? "none" : "";
  // Phase 10 status strip: active project, run mode, today's spend, cost-ledger health.
  setText("st-project", s.project && s.project.name ? s.project.name : "global");
  setText("st-mode", s.mode || "approval");
  if (typeof s.today_spend_usd === "number") setText("st-spend", `$${s.today_spend_usd.toFixed(4)}`);
  const led = document.getElementById("st-ledger"); if (led) led.style.display = s.ledger_degraded ? "" : "none";
  // Daily current-activity card (if mounted) — same source, same result
  if (document.getElementById("daily-now-lead")) {
    setClass("daily-now-dot", dotClass);
    setText("daily-now-lead", busy ? "Kairo is working" : "Kairo is idle");
    setClass("daily-now-lead", "lead" + (busy ? "" : " idle"));
    setText("daily-now-desc", busy ? "Working on your request." : "Nothing running. Send a message to begin.");
  }
}

async function pollStatus() {
  const s = await api.get("/api/runner");
  if (s) { state.runner = s; renderRunnerState(); }
  const v = await api.get("/api/voice/status");
  if (v) {
    document.getElementById("st-voice").textContent = v.enabled ? (v.listening || "ready") : "off";
    const mic = document.getElementById("st-mic");
    mic.style.display = v.enabled ? "" : "none";  // only show when voice is wired
    if (mic.dataset.busy !== "1") {
      mic.textContent = v.listening === "listening" ? "🎤 Listening…" : "🎤 Talk";
    }
  }
}

// Push-to-talk: one activation opens the SERVER's mic for one utterance → one turn. Risky
// actions in that turn still escalate to the on-screen Gate (voice prepares, screen commits).
async function listenOnce() {
  const mic = document.getElementById("st-mic");
  if (mic.dataset.busy === "1") return;
  mic.dataset.busy = "1"; mic.disabled = true; mic.textContent = "🎤 Listening…";
  try {
    const res = await api.post("/api/voice/listen");
    if (!res.ok) mic.textContent = "🎤 (unavailable)";
  } finally {
    mic.dataset.busy = ""; mic.disabled = false;
    setTimeout(() => { mic.textContent = "🎤 Talk"; }, 1500);
    pollStatus();
  }
}

// --- wire up ---
function init() {
  document.getElementById("ap-approve").addEventListener("click", () => resolveApproval("approve"));
  document.getElementById("ap-always").addEventListener("click", () => resolveApproval("always"));
  document.getElementById("ap-deny").addEventListener("click", () => resolveApproval("deny"));
  document.getElementById("st-stop").addEventListener("click", async () => { await api.post("/api/runner/pause"); pollStatus(); });
  document.getElementById("st-resume").addEventListener("click", async () => { await api.post("/api/runner/resume"); pollStatus(); });
  document.getElementById("st-mic").addEventListener("click", listenOnce);
  // Daily/Debug segmented toggle — the clear mode split. Debug reveals telemetry only
  // (a body class); it never changes any route or capability.
  const setMode = (debug) => {
    document.body.classList.toggle("debug", debug);
    document.getElementById("mode-debug").classList.toggle("active", debug);
    document.getElementById("mode-daily").classList.toggle("active", !debug);
  };
  document.getElementById("mode-daily").addEventListener("click", () => setMode(false));
  document.getElementById("mode-debug").addEventListener("click", () => setMode(true));
  // Appearance theme toggle — client-side only (theme.js persists to localStorage, applies to
  // <html>). No server route: appearance adds no authority.
  const syncTheme = () => {
    const cur = getTheme().theme;
    document
      .querySelectorAll("#theme-toggle button")
      .forEach((b) => b.classList.toggle("active", b.dataset.themeChoice === cur));
  };
  document
    .querySelectorAll("#theme-toggle button")
    .forEach((b) => b.addEventListener("click", () => setTheme(b.dataset.themeChoice)));
  // One sync path: any appearance change (this toggle OR the Settings screen) re-syncs the
  // toggle's active pill and re-renders Settings if it's open, so the two controls never disagree.
  busOn("appearance", () => { syncTheme(); refreshIfActive("settings"); });
  initTheme();
  syncTheme();
  initKeys();                                        // single keydown dispatcher
  initPalette(api);                                  // Ctrl/Cmd-K command palette (search + navigate, GET-only)
  window.addEventListener("hashchange", navigate);
  connect();
  navigate();
  pollStatus();
  setInterval(pollStatus, 4000);
}

init();
