// Kairo Workstation — shell core (Phase 8). WS + router + status + the approval flow.
// Carries NO safety logic: it renders and clicks. All enforcement is server-side (the nonce
// is minted only over the live socket after the modal is shown; the server validates every
// resolve). Per-screen rendering lives in ./screens/*.js.

import { render as renderDaily } from "./screens/daily.js";
import { render as renderChat } from "./screens/chat.js";
import { onConversationEvent } from "./screens/conversation.js";
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
import { render as renderWorkspace } from "./screens/workspace.js";
import { render as renderArtifacts } from "./screens/artifacts.js";
import { get as getTheme, initTheme, setTheme } from "./ui/theme.js";
import { initKeys, clearScope, pushEscape } from "./ui/keys.js";
import { emit as busEmit, on as busOn } from "./ui/bus.js";
import { init as initPalette, openPalette } from "./ui/palette.js";
import { refreshHeader } from "./ui/header.js";
import { canCapture, cancelCapture, playCaption, playbackOn, recording, setPlayback, stopCaption, toggleTalk } from "./ui/voice.js";
import { money } from "./ui/format.js";

const state = {
  chat: [],            // Daily conversation items {role, text} | {tool, resolution}
  chatAttachments: [], // local user-selected sources persisted into this chat/project knowledge scope
  pending: new Map(),  // decision_id -> approval payload (+ nonce once minted)
  runner: {},          // last /api/runner status
  voice: { enabled: false },
  trace: [],           // raw events (Debug/Trace)
  notices: [],         // background job/reminder/digest notices (Phase 9)
  context: null,       // server-owned {session_id, project_id}; never inferred from a hash
  route: "chat",
  routeArgs: [],       // positional hash args after the screen name (#workspace/{id})
};

const WORKSPACE_KEY = "kairo:workspace-id";
let workspaceId = null;
try { workspaceId = sessionStorage.getItem(WORKSPACE_KEY); } catch { /* storage unavailable */ }

function workspaceHeaders(base = {}) {
  return workspaceId ? { ...base, "x-kairo-workspace-id": workspaceId } : base;
}

// --- tiny API helper (same-origin; cookie carried automatically) ---
export const api = {
  state,
  async get(path) {
    const r = await fetch(path, { headers: workspaceHeaders({ "accept": "application/json" }) });
    return r.ok ? r.json() : null;
  },
  async post(path, body) {
    const r = await fetch(path, {
      method: "POST",
      headers: workspaceHeaders({ "content-type": "application/json" }),
      body: JSON.stringify(body || {}),
    });
    return { ok: r.ok, status: r.status, data: await r.json().catch(() => ({})) };
  },
  async upload(path, body) {
    // FormData lets the browser set its own multipart boundary. The server still receives the
    // same authenticated, server-owned workspace handle as every other attended UI action.
    const r = await fetch(path, { method: "POST", headers: workspaceHeaders(), body });
    return { ok: r.ok, status: r.status, data: await r.json().catch(() => ({})) };
  },
  async download(path, filename) {
    // Keep the opaque, server-owned workspace handle on downloads too.  This avoids opening a
    // new unbound tab just to fetch an output from another chat/project.
    const r = await fetch(path, { headers: workspaceHeaders() });
    if (!r.ok) return false;
    const url = URL.createObjectURL(await r.blob());
    const link = document.createElement("a");
    link.href = url;
    link.download = filename || "kairo-output";
    link.hidden = true;
    document.body.appendChild(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 0);
    return true;
  },
  // Re-open the amber approval modal for the oldest pending item (Daily "Review" button).
  reviewPending() {
    const next = [...state.pending.values()][0];
    if (next) { next._shown = false; showTopApproval(); }
  },
  async toggleVoiceCapture(mode, onTranscript) {
    if (!state.voice.enabled) {
      onVoiceState("error", state.voice.reason || "Voice is unavailable.");
      return;
    }
    await toggleTalk({ mode, headers: workspaceHeaders(), onState: onVoiceState, onTranscript });
  },
  cancelVoiceCapture() {
    if (cancelCapture(onVoiceState)) return;
    if (stopCaption()) onVoiceState("idle");
  },
  // Resume a past chat into the live session AND load its transcript into the Daily conversation
  // view (so resuming actually shows the conversation). Returns false if a turn is in flight (409).
  async resumeChat(sessionId) {
    const res = await api.post(`/api/sessions/${sessionId}/resume`, {});
    if (!res.ok) return false;
    const t = await api.get(`/api/sessions/${sessionId}`);
    if (t && Array.isArray(t.messages)) {
      state.chat = t.messages.map((m) => ({ role: m.role, text: m.text }));
    }
    state.chatAttachments = [];
    return true;
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
    wsSend({ type: "hello", surfaces: [...mounted], workspace_id: workspaceId });
    setInterval(() => wsSend({ type: "heartbeat" }), 5000);
  };
  ws.onmessage = (e) => handleMessage(JSON.parse(e.data));
  ws.onclose = () => setTimeout(connect, 1500); // best-effort reconnect
}

function handleMessage(msg) {
  if (msg.type === "workspace") {
    workspaceId = msg.workspace_id || null;
    try {
      if (workspaceId) sessionStorage.setItem(WORKSPACE_KEY, workspaceId);
      else sessionStorage.removeItem(WORKSPACE_KEY);
    } catch { /* storage unavailable — this socket remains usable */ }
    state.context = { session_id: msg.session_id, project_id: msg.project_id };
    pollStatus();
    renderRoute();
    return;
  }
  const lifecycle = ["project_changed", "session_new", "session_resumed"].includes(msg.kind);
  if (msg.workspace_id && msg.workspace_id !== workspaceId) return;
  if (msg.session_id != null && !acceptsContext(msg) && !(lifecycle && msg.workspace_id === workspaceId)) return;
  if (msg.type === "approval") { onApproval(msg); return; }
  if (msg.type === "approval_nonce") { onNonce(msg); return; }
  if (msg.kind && msg.kind.startsWith("orchestration_")) {
    busEmit("orchestration", msg);
    studioOnEvent(state, msg);        // update the in-flight run panel
    refreshIfActive("studio");
    if (msg.kind === "orchestration_completed") pollStatus();  // refresh spend/busy chips
    return;
  }
  if (msg.kind === "mode_changed") {
    if (state.runner) state.runner.mode = msg.mode;
    renderRunnerState(); refreshHeader(); return;
  }
  if (msg.kind === "model_changed") {
    if (state.runner) state.runner.model = msg.model;
    renderRunnerState(); refreshHeader(); return;
  }
  if (msg.kind === "effort_changed") {
    if (state.runner) state.runner.effort = msg.effort;
    renderRunnerState(); refreshHeader(); return;
  }
  if (msg.kind === "project_changed") {
    // A scope switch started a fresh scoped conversation server-side — clear the local view.
    clearPendingApprovals();
    if (state.runner) state.runner.project = { id: msg.project_id, name: msg.name };
    state.context = { session_id: msg.session_id, project_id: msg.project_id };
    state.chat = [];
    state.chatAttachments = [];
    renderRunnerState(); refreshHeader(); refreshConversation(); return;
  }
  if (msg.kind === "session_new" || msg.kind === "session_resumed") {
    clearPendingApprovals();
    state.context = { session_id: msg.session_id, project_id: msg.project_id };
    if (msg.kind === "session_new") state.chat = [];
    state.chatAttachments = [];
    pollStatus(); refreshHeader(); refreshConversation(); return;
  }
  if (msg.kind === "session_persistence") {
    if (state.runner) state.runner.session_save_state = msg.state;
    refreshConversation();
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
    refreshConversation();
    renderRunnerState();
  }
}

// The server already targets exact contexts.  This is a consumer-side backstop: a stale queued
// frame, reconnect, or future emitter cannot mutate this tab from another chat/project.
function acceptsContext(msg) {
  const c = state.context;
  return !!c && msg.session_id === c.session_id && msg.project_id === c.project_id;
}

// Background notices (job/reminder/digest) reach the browser here — the calm, non-modal
// surface (never an attention grab). A digest notice quietly refreshes Daily's Briefing.
function onNotice(notice) {
  if (!notice) return;
  state.notices.unshift(notice);
  if (state.notices.length > 50) state.notices.pop();
  busEmit("notice", notice);
  if (notice.kind === "digest") refreshConversation();
}

// Voice round-trip, made visible in Daily (one heard bubble + one safe caption). The reply
// text is the renderer's post-privacy output — the UI never sees a raw answer or a payload.
function onVoice(msg) {
  state.chat.push(msg.role === "heard" ? { role: "heard", text: msg.text } : { role: "assistant", text: msg.text });
  refreshConversation();
  // Optional playback: speak the SAFE reply caption (the server masks + caps before TTS). The
  // caption is always on screen too, so playback is a best-effort enhancement, never the record.
  if (msg.role !== "heard") playCaption(msg.text, workspaceHeaders(), onVoiceState);
}

// Read-only voice state pill (idle/listening/transcribing/thinking/speaking/error) — content-free.
const VOICE_LABELS = {
  listening: "⏹ Stop", capturing: "🎤 Capturing…", transcribing: "🎤 Transcribing…",
  thinking: "🎤 Thinking…", speaking: "🎤 Speaking…", error: "🎤 Talk",
};
function onVoiceState(s, reason = "") {
  state.voice.listening = s;
  state.voice.reason = reason || (s === "error" ? state.voice.reason : "");
  const voiceEl = document.getElementById("st-voice");
  if (voiceEl) { voiceEl.textContent = s; if (s !== "error") voiceEl.title = ""; }
  const mic = document.getElementById("st-mic");
  if (mic) mic.textContent = VOICE_LABELS[s] || (recording() ? "⏹ Stop" : "🎤 Talk");
  refreshConversation();
}

// The Talk button: browser push-to-talk. First press captures (mic permission prompt); second
// press stops + sends the utterance to the server voice session. A failure (denied mic / no
// audio / turn error) shows a plain reason on the pill — never a raw error body.
async function talk() {
  await api.toggleVoiceCapture("conversation");
}

function onEvent(evt) {
  state.trace.push(evt);
  if (state.trace.length > 500) state.trace.shift();
  busEmit("event", evt);
  // A completed turn settles the runner state immediately — don't wait for the next poll,
  // or the Daily card lingers on "working" after the turn (incl. a denied one) ends.
  if (evt.type === "turn_completed" && state.runner) state.runner.turn_busy = false;
  onConversationEvent(state, evt);
  refreshConversation();
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

function approvalCopy(next) {
  const tool = String(next.tool || "").toLowerCase();
  if (tool === "gmail_create_draft") return ["Gmail", "Kairo wants to create a Gmail draft."];
  if (tool === "gmail_update_draft") return ["Gmail", "Kairo wants to update a Gmail draft."];
  if (tool.includes("gmail")) return ["Gmail", "Kairo wants to read Gmail."];
  if (tool === "calendar_create_event") return ["Calendar", "Kairo wants to create a calendar event."];
  if (tool === "calendar_update_event") return ["Calendar", "Kairo wants to update a calendar event."];
  if (tool === "calendar_cancel_event") return ["Calendar", "Kairo wants to cancel a calendar event."];
  if (tool.includes("calendar")) return ["Calendar", "Kairo wants to read your calendar."];
  if (tool === "drive_create_doc") return ["Google Drive", "Kairo wants to create a Google Doc."];
  if (tool === "drive_update_doc") return ["Google Drive", "Kairo wants to update a Google Doc."];
  if (tool.includes("drive") || tool.includes("doc")) return ["Google Drive", "Kairo wants to read Google Drive."];
  if (tool === "write_file") return ["Files", "Kairo wants to write a local file."];
  if (tool === "send_notification") return ["Notifications", "Kairo wants to send a notification."];
  if (tool.includes("shell") || tool.includes("terminal") || tool.includes("command")) {
    return ["Terminal", "Kairo wants to use your terminal."];
  }
  if (tool === "web_search") return ["Web", "Kairo wants to search the web."];
  if (tool.includes("web")) return ["Web", "Kairo wants to access a website."];
  if (tool.includes("file") || tool.includes("directory")) return ["Files", "Kairo wants to access local files."];
  const label = String(next.tool || "an action").replace(/[_-]+/g, " ");
  return [label, `Kairo wants to use ${label}.`];
}

function showTopApproval() {
  const next = [...state.pending.values()].find((p) => !p._shown);
  if (!next) return;
  const overlay = document.getElementById("overlay");
  if (overlay.classList.contains("show")) return; // one attention surface at a time
  next._shown = true;
  const [label, request] = approvalCopy(next);
  document.getElementById("ap-kind").textContent =
    next.kind === "voice" ? "Confirm on screen (voice)" : "Kairo needs your approval";
  document.getElementById("ap-tool").textContent = label;
  document.getElementById("ap-request").textContent = request;
  document.getElementById("ap-details").open = false;
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

function clearPendingApprovals() {
  state.pending.clear();
  hideApproval();
  updateGateBadge();
  refreshIfActive("gate");
}

function updateGateBadge() {
  const badge = document.getElementById("gate-badge");
  const n = state.pending.size;
  badge.textContent = String(n);
  badge.classList.toggle("show", n > 0);
  const attention = document.getElementById("st-attention");
  if (attention) {
    attention.textContent = `${n} approval${n === 1 ? "" : "s"}`;
    attention.classList.toggle("is-hidden", n === 0);
  }
  const runner = state.runner || {};
  syncStatusChrome(!!runner.turn_busy, runner.runner_running === false);
}

function syncStatusChrome(busy, paused = false) {
  const status = document.querySelector(".status");
  if (!status) return;
  // Idle is not a user task. Keep the global chrome out of the way unless work is active or a
  // non-Chat surface needs an approval shortcut; Chat already owns its own approval pill.
  const attentionElsewhere = state.pending.size > 0 && state.route !== "chat";
  status.classList.toggle("status-active", !!busy || !!paused || attentionElsewhere);
  status.classList.toggle("is-working", !!busy);
  status.classList.toggle("is-paused", !!paused);
}

// --- router ---
const screens = {
  chat: renderChat, daily: renderDaily, projects: renderProjects, studio: renderStudio, gate: renderGate,
  vault: renderVault, tasks: renderTasks, memory: renderMemory, hub: renderHub,
  costs: renderCosts, lab: renderLab, meetings: renderMeetings, trace: renderTrace,
  settings: renderSettings, workspace: renderWorkspace, artifacts: renderArtifacts,
};
const DEBUG_ROUTES = new Set(["trace", "lab"]);
const ROUTE_LABELS = {
  chat: "Chat", daily: "Daily", projects: "Projects", workspace: "Workspace",
  studio: "Studio", artifacts: "Artifacts", vault: "Knowledge", costs: "Costs",
  gate: "Notifications", hub: "Connectors", settings: "Settings", meetings: "Meetings",
  trace: "Trace", lab: "Lab", tasks: "Tasks", memory: "Memory",
};
const WORKSPACE_LABELS = {
  overview: "Overview", chats: "Chats", artifacts: "Artifacts", memory: "Memory",
  tasks: "Tasks", vault: "Vault", studio: "Studio", office: "Office", graph: "Graph",
  costs: "Costs", activity: "Activity",
};

function refreshIfActive(name) { if (state.route === name) renderRoute(); }
function refreshConversation() { refreshIfActive("chat"); refreshIfActive("daily"); }

function renderRoute() {
  const container = document.getElementById("screen");
  document.body.dataset.route = state.route;
  container.className = "screen";
  if (DEBUG_ROUTES.has(state.route) && !document.body.classList.contains("debug")) {
    container.textContent = "";
    const h = document.createElement("h1");
    h.textContent = "Debug is off";
    const sub = document.createElement("div");
    sub.className = "sub";
    sub.textContent = "Trace and Lab stay hidden until Debug is enabled in Settings.";
    const link = document.createElement("a");
    link.className = "plain-button";
    link.href = "#settings";
    link.textContent = "Open Settings";
    container.append(h, sub, link);
    return;
  }
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
  return { name: parts[0] || "chat", args: parts.slice(1) };
}

function navigate() {
  const { name, args } = parseHash();
  if (state.route && state.route !== name) setSurface(state.route, false);
  state.route = name;
  state.routeArgs = args;
  clearScope();                                      // a screen's local keys never leak forward
  setSurface(name, true);
  if (name === "gate") setSurface("gate", true);
  document.querySelector(".mobile-more")?.removeAttribute("open");
  for (const a of document.querySelectorAll(".rail a")) a.classList.toggle("active", a.dataset.screen === name);
  renderLocation();
  renderRoute();
}

function cap(s) { return s.charAt(0).toUpperCase() + s.slice(1); }

function renderLocation() {
  const location = document.getElementById("st-location");
  if (!location) return;
  const tab = state.route === "workspace" && state.routeArgs[1]
    ? WORKSPACE_LABELS[state.routeArgs[1]] : "";
  const base = tab ? `Workspace · ${tab}` : (ROUTE_LABELS[state.route] || cap(state.route));
  const project = state.runner && state.runner.project && state.runner.project.name;
  location.textContent = project && ["chat", "workspace", "studio", "artifacts", "costs"].includes(state.route)
    ? `${base} · ${project}` : base;
}

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
  const stop = document.getElementById("st-stop"); if (stop) stop.classList.toggle("is-hidden", !busy);
  const resume = document.getElementById("st-resume"); if (resume) resume.classList.toggle("is-hidden", s.runner_running !== false);
  // Phase 10 status strip: active project, run mode, today's spend, cost-ledger health.
  setText("st-project", s.project && s.project.name ? s.project.name : "global");
  setText("st-mode", s.mode || "approval");
  // Phase 15.5: the composer's always-visible live model + mode readout (real server state, not
  // the old debug-only fake chips). The full selectors live in the conversation header.
  for (const id of ["composer-model", "chat-model"]) setText(id, s.model || "");
  for (const id of ["composer-mode", "chat-mode"]) setText(id, s.mode ? `mode ${s.mode}` : "");
  // The rail's Workspace entry deep-links to the ACTIVE project (hidden in global scope).
  const ws = document.getElementById("rail-workspace");
  if (ws) {
    const pid = s.project && s.project.id;
    ws.classList.toggle("is-hidden", !pid);
    if (pid) ws.setAttribute("href", `#workspace/${pid}`);
  }
  if (typeof s.today_spend_usd === "number") setText("st-spend", `$${s.today_spend_usd.toFixed(4)}`);
  const led = document.getElementById("st-ledger"); if (led) led.classList.toggle("is-hidden", !s.ledger_degraded);
  renderLocation();
  // Daily's quiet briefing line (if mounted) shares the settled runner state, but Chat remains
  // the only place to send a message. Keep this copy/format aligned with Daily's initial render.
  if (document.getElementById("daily-now-lead")) {
    setClass("daily-now-dot", dotClass);
    setText("daily-now-lead", busy ? "Kairo is working" : "Kairo is idle");
    setText("daily-now-desc", busy ? "Progress is available in Chat." : "Your briefing is up to date.");
    setText("daily-cost-today", typeof s.today_spend_usd === "number"
      ? `${money(s.today_spend_usd)} today` : "Cost unavailable");
  }
  syncStatusChrome(busy, s.runner_running === false);
}

let _rehydrated = false;
// Fix the "No messages yet" reload: the server keeps the active conversation alive across a
// browser reload, so on first load pull the transcript we are IN back into the view. Runs once,
// only when there's an active session and no local chat yet (never clobbers an in-progress chat).
async function rehydrateConversation() {
  const sid = state.runner && state.runner.session_id;
  if (_rehydrated || sid == null) return;
  _rehydrated = true;
  if (state.chat.length) return;
  const t = await api.get(`/api/sessions/${sid}`);
  if (t && Array.isArray(t.messages)) {
    state.chat = t.messages.map((m) => ({ role: m.role, text: m.text }));
    refreshConversation();
  }
}

async function pollStatus() {
  const s = await api.get("/api/runner");
  if (s) {
    state.runner = s;
    if (s.session_id != null) {
      state.context = { session_id: s.session_id, project_id: s.project ? s.project.id : null };
    }
    renderRunnerState(); rehydrateConversation();
  }
  // Default to OFF when the status can't be read (v null), so the mic is ALWAYS gated — never left
  // at the CSP-blocked HTML default (which would leave Talk visible while voice is off, blocker 4).
  const v = (await api.get("/api/voice/status")) || { enabled: false, reason: "" };
  const previousVoice = state.voice;
  const currentVoiceState = state.voice.listening || "idle";
  const unavailableReason = !v.enabled ? (v.reason || "Voice is unavailable.")
    : (!canCapture() ? "This browser can't record audio." : "");
  state.voice = {
    ...state.voice, ...v,
    listening: (!v.enabled || currentVoiceState === "idle" || currentVoiceState === "error")
      ? (v.listening || "idle") : currentVoiceState,
    reason: unavailableReason || (currentVoiceState === "error" ? state.voice.reason : ""),
    browserCapture: canCapture(),
  };
  const voiceEl = document.getElementById("st-voice");
  if (voiceEl) {
    voiceEl.textContent = v.enabled ? (v.listening || "ready") : "off";
    voiceEl.title = v.enabled ? "" : (v.reason || "");
    if (v.enabled && !canCapture()) voiceEl.title = "This browser can't record audio.";
  }
  const mic = document.getElementById("st-mic");
  if (mic) {
    // Talk shows ONLY when voice is enabled AND the browser can capture audio (class toggle, so
    // the strict CSP can't leave it stuck visible).
    mic.classList.toggle("is-hidden", !(v.enabled && canCapture()));
    if (mic.dataset.busy !== "1" && !recording()) mic.textContent = "🎤 Talk";
  }
  const play = document.getElementById("st-play");
  if (play) {  // playback toggle appears only when a cloud TTS can actually produce audio
    play.classList.toggle("is-hidden", !(v.enabled && v.playback));
    play.classList.toggle("active", playbackOn());
    play.title = playbackOn() ? "Spoken replies: on" : "Spoken replies: off";
  }
  if (previousVoice.enabled !== state.voice.enabled
      || previousVoice.reason !== state.voice.reason
      || previousVoice.browserCapture !== state.voice.browserCapture) refreshConversation();
}

// --- wire up ---
function init() {
  document.getElementById("ap-approve").addEventListener("click", () => resolveApproval("approve"));
  document.getElementById("ap-always").addEventListener("click", () => resolveApproval("always"));
  document.getElementById("ap-deny").addEventListener("click", () => resolveApproval("deny"));
  document.getElementById("st-stop").addEventListener("click", async () => { await api.post("/api/runner/pause"); pollStatus(); });
  document.getElementById("st-resume").addEventListener("click", async () => { await api.post("/api/runner/resume"); pollStatus(); });
  document.getElementById("st-mic").addEventListener("click", talk);  // browser push-to-talk
  document.getElementById("st-play").addEventListener("click", () => {  // toggle spoken replies
    setPlayback(!playbackOn());
    pollStatus();
  });
  // Daily/Debug segmented toggle — the clear mode split. Debug reveals telemetry only
  // (a body class); it never changes any route or capability.
  const setMode = (debug) => {
    document.body.classList.toggle("debug", debug);
    document.getElementById("mode-debug").classList.toggle("active", debug);
    document.getElementById("mode-daily").classList.toggle("active", !debug);
    if (!debug && DEBUG_ROUTES.has(state.route)) renderRoute();
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
  initPalette(api);                                  // Ctrl/Cmd-K command palette (search + actions)
  document.getElementById("rail-search").addEventListener("click", () => openPalette());
  window.addEventListener("hashchange", navigate);
  connect();
  navigate();
  pollStatus();
  setInterval(pollStatus, 4000);
}

init();
