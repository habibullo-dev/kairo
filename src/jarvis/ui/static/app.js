// Kairo Workstation — shell core (Phase 8). WS + router + status + the approval flow.
// Carries NO safety logic: it renders and clicks. All enforcement is server-side (the nonce
// is minted only over the live socket after the modal is shown; the server validates every
// resolve). Per-screen rendering lives in ./screens/*.js.

import { render as renderDaily, onEvent as dailyOnEvent } from "./screens/daily.js";
import { render as renderGate } from "./screens/gate.js";
import { render as renderVault } from "./screens/vault.js";
import { render as renderTasks } from "./screens/tasks.js";
import { render as renderMemory } from "./screens/memory.js";
import { render as renderHub } from "./screens/hub.js";
import { render as renderLab } from "./screens/lab.js";
import { render as renderMeetings } from "./screens/meetings.js";
import { render as renderTrace } from "./screens/trace.js";

const state = {
  chat: [],            // Daily conversation items {role, text} | {tool, resolution}
  pending: new Map(),  // decision_id -> approval payload (+ nonce once minted)
  runner: {},          // last /api/runner status
  voice: { enabled: false },
  trace: [],           // raw events (Debug/Trace)
  route: "daily",
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
  if (msg.kind === "event") { onEvent(msg); return; }
  if (msg.kind === "turn_cancelled") { state.chat.push({ role: "assistant", text: "— turn cancelled —" }); refreshIfActive("daily"); }
  if (msg.kind === "turn_error") { state.chat.push({ role: "assistant", text: `— error: ${msg.error} —` }); refreshIfActive("daily"); }
}

function onEvent(evt) {
  state.trace.push(evt);
  if (state.trace.length > 500) state.trace.shift();
  dailyOnEvent(state, evt);
  refreshIfActive("daily");
  refreshIfActive("trace");
}

// --- approvals: the priority attention surface ---
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
  for (const id of ["ap-approve", "ap-always"]) document.getElementById(id).disabled = true;
  document.getElementById("ap-always").style.display = next.kind === "voice" ? "none" : ""; // voice: no "always"
  overlay.dataset.decision = next.decision_id;
  overlay.classList.add("show");
  setSurface("gate", true);                          // the screen is now watching
  wsSend({ type: "approval_shown", decision_id: next.decision_id }); // prove visibility ⇒ mint nonce
}

function onNonce(msg) {
  const p = state.pending.get(msg.decision_id);
  if (!p) return;
  p.nonce = msg.nonce;
  const overlay = document.getElementById("overlay");
  if (overlay.dataset.decision === msg.decision_id) {
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
  daily: renderDaily, gate: renderGate, vault: renderVault, tasks: renderTasks,
  memory: renderMemory, hub: renderHub, lab: renderLab, meetings: renderMeetings,
  trace: renderTrace,
};

function refreshIfActive(name) { if (state.route === name) renderRoute(); }

function renderRoute() {
  const container = document.getElementById("screen");
  const fn = screens[state.route];
  if (fn) { fn(container, api); }
  else { container.innerHTML = `<h1>${cap(state.route)}</h1><div class="sub">This screen lands in Task 8.</div>`; }
}

function navigate() {
  const next = (location.hash.replace("#", "") || "daily");
  if (state.route && state.route !== next) setSurface(state.route, false);
  state.route = next;
  setSurface(next, true);
  if (next === "gate") setSurface("gate", true);
  for (const a of document.querySelectorAll(".rail a")) a.classList.toggle("active", a.dataset.screen === next);
  renderRoute();
}

function cap(s) { return s.charAt(0).toUpperCase() + s.slice(1); }

// --- status bar ---
async function pollStatus() {
  const s = await api.get("/api/runner");
  if (s) {
    state.runner = s;
    const pill = document.getElementById("st-runner");
    pill.textContent = s.turn_busy ? "thinking" : (s.runner_running ? "idle" : "paused");
    pill.className = "pill " + (s.runner_running ? "running" : "paused");
    document.getElementById("st-turn").textContent = s.turn_busy ? "busy" : "—";
    document.getElementById("st-stop").style.display = s.runner_running ? "" : "none";
    document.getElementById("st-resume").style.display = s.runner_running ? "none" : "";
  }
  const v = await api.get("/api/voice/status");
  if (v) document.getElementById("st-voice").textContent = v.enabled ? (v.listening || "ready") : "off";
}

// --- wire up ---
function init() {
  document.getElementById("ap-approve").addEventListener("click", () => resolveApproval("approve"));
  document.getElementById("ap-always").addEventListener("click", () => resolveApproval("always"));
  document.getElementById("ap-deny").addEventListener("click", () => resolveApproval("deny"));
  document.getElementById("st-stop").addEventListener("click", async () => { await api.post("/api/runner/pause"); pollStatus(); });
  document.getElementById("st-resume").addEventListener("click", async () => { await api.post("/api/runner/resume"); pollStatus(); });
  document.getElementById("st-debug").addEventListener("change", (e) => document.body.classList.toggle("debug", e.target.checked));
  window.addEventListener("hashchange", navigate);
  connect();
  navigate();
  pollStatus();
  setInterval(pollStatus, 4000);
}

init();
