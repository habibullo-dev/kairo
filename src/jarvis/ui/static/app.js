// Kairo Workstation — shell core (Phase 8). WS + router + status + the approval flow.
// Carries NO safety logic: it renders and clicks. All enforcement is server-side (the nonce
// is minted only over the live socket after the modal is shown; the server validates every
// resolve). Per-screen rendering lives in ./screens/*.js.

import { render as renderDaily } from "./screens/daily.js";
import { render as renderChat } from "./screens/chat.js";
import { onConversationEvent } from "./screens/conversation.js";
import {
  dismissProjectDialogs,
  dismissProjectServiceAccess,
  render as renderProjects,
} from "./screens/projects.js";
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
import { closePalette, init as initPalette, openPalette } from "./ui/palette.js";
import { refreshHeader } from "./ui/header.js";
import { canCapture, cancelCapture, playCaption, playbackOn, recording, setPlayback, stopCaption, toggleTalk } from "./ui/voice.js";
import { money } from "./ui/format.js";
import { dismissTaskDialogs, openParkedTaskApproval } from "./ui/task-draft.js";
import { dismissMemoryDraft } from "./ui/memory-draft.js";
import { dismissFeedbackDialogs, showToast } from "./ui/feedback.js";
import { dismissProjectReport } from "./ui/project-report.js";
import { readMigrated, removeStored, writeStored } from "./ui/storage.js";

const state = {
  chat: [],            // Daily conversation items {role, text} | {tool, resolution}
  chatAttachments: [], // local user-selected sources persisted into this chat/project knowledge scope
  pending: new Map(),  // decision_id -> approval payload (+ nonce once minted)
  parkedTaskApprovals: new Map(), // run_id -> exact durable unattended ASK (+ live nonce)
  runner: null,        // last /api/runner status; null until the first shared read succeeds
  runnerStatusError: false, // latest shared runner read failed; cached state is not current
  voice: { enabled: false, meeting: "idle", meeting_recording: false },
  trace: [],           // raw events (Debug/Trace)
  notices: [],         // background job/reminder/digest notices (Phase 9)
  turnAdmission: null, // exact optimistic /api/turn admission currently awaiting reconciliation
  turnDraft: null,     // failed admission restored across same-authority route remounts
  context: null,       // server-owned {session_id, project_id}; never inferred from a hash
  route: "chat",
  routeArgs: [],       // positional hash args after the screen name (#workspace/{id})
};
let meetingEventGeneration = 0;
let meetingRecordingEventGeneration = 0;
let meetingStateRevision = -1;
let meetingRecordingRevision = -1;
let meetingRecordingEpoch = null;
let voiceStatusSequence = 0;
let lastPublishedVoiceStatus = null;

const RECORDED_DELEGATION_STATUSES = new Set(["running", "ok", "error", "timeout", "cancelled", "aborted"]);

// The transcript API intentionally returns only terminal/lifecycle delegation metadata, not a
// child event timeline. Keep parent messages first and append these honest recorded summaries;
// their original position in the turn was never persisted and must not be invented on reload.
function hydrateTranscript(transcript) {
  const messages = Array.isArray(transcript && transcript.messages) ? transcript.messages : [];
  const delegations = Array.isArray(transcript && transcript.delegations) ? transcript.delegations : [];
  return [
    ...messages.map((m) => ({ role: m.role, text: m.text })),
    ...delegations.slice(0, 50).map((d) => ({
      subagent: true,
      agentId: String(d && d.agent_id || "unknown").slice(0, 80),
      title: String(d && d.title || "sub-agent").slice(0, 120),
      status: RECORDED_DELEGATION_STATUSES.has(d && d.status) ? d.status : "aborted",
      detail: "recorded delegated work",
    })),
  ];
}

const WORKSPACE_KEY = "kira:workspace-id";
const LEGACY_WORKSPACE_KEY = "kairo:workspace-id";
let workspaceId = readMigrated("session", WORKSPACE_KEY, [LEGACY_WORKSPACE_KEY]);

function workspaceHeaders(base = {}) {
  return workspaceId ? { ...base, "x-kira-workspace-id": workspaceId } : base;
}

function expectedContextHeaders(context = state.context) {
  if (!Number.isInteger(context?.session_id) || !Number.isInteger(context?.context_revision)) return {};
  return {
    "x-kira-expected-session-id": String(context.session_id),
    "x-kira-expected-project-id": context.project_id == null ? "global" : String(context.project_id),
    "x-kira-expected-context-revision": String(context.context_revision),
  };
}

function sessionAlive(response) {
  if (response.status !== 401) return true;
  location.replace("/login");
  return false;
}

// --- tiny API helper (same-origin; cookie carried automatically) ---
let runnerStatusRequest = null;
let runnerStatusRequestGeneration = -1;
let runnerStatusAbort = null;
let runnerStatusGeneration = -1;
let runnerStatusRequestRevision = 0;
let turnSettlementRevision = 0;
let runnerControlOperation = null;
let runnerControlSequence = 0;
const API_READ_OUTCOME = Symbol("api-read-outcome");
const EXPECTED_UNAVAILABLE_READ_STATUSES = new Set([404, 503]);
const INITIAL_ROUTE_READ_TIMEOUT_MS = 15000;
const RUNNER_CONTROL_RECONCILE_TIMEOUT_MS = 7000;
export const api = {
  state,
  authorityToken() { return authorityGeneration; },
  authorityIsCurrent(token) { return token === authorityGeneration; },
  navigationToken() { return navigationGeneration; },
  navigationIsCurrent(token) { return token === navigationGeneration; },
  restoreTurnDraft(text) {
    const value = String(text || "").trim();
    if (!value) return;
    const liveInput = state.route === "chat" ? document.getElementById("chat-input") : null;
    if (liveInput) {
      liveInput.value = liveInput.value.trim() ? `${value}\n${liveInput.value}` : value;
      liveInput.dispatchEvent(new Event("input"));
      liveInput.focus();
    } else {
      state.turnDraft = state.turnDraft ? `${value}\n${state.turnDraft}` : value;
    }
    refreshConversation();
  },
  refreshConversationView() { refreshConversation(); },
  workspaceToken() { return workspaceId; },
  async get(path, { signal, diagnostic = false } = {}) {
    try {
      const r = await fetch(path, {
        headers: workspaceHeaders({ "accept": "application/json" }),
        signal,
      });
      if (!sessionAlive(r)) {
        return diagnostic ? { [API_READ_OUTCOME]: true, data: null, failure: null } : null;
      }
      if (!r.ok) {
        const failure = diagnostic && !EXPECTED_UNAVAILABLE_READ_STATUSES.has(r.status)
          ? new Error(`read failed with status ${r.status}`)
          : null;
        return diagnostic ? { [API_READ_OUTCOME]: true, data: null, failure } : null;
      }
      const data = await r.json();
      return diagnostic ? { [API_READ_OUTCOME]: true, data, failure: null } : data;
    } catch (failure) {
      // Ordinary read surfaces deliberately use null for feature-off/partial-unavailable states.
      // The router asks for a diagnostic outcome only while an async screen owns its initial
      // render, allowing transport/JSON failures to reach that screen's recovery boundary.
      return diagnostic ? { [API_READ_OUTCOME]: true, data: null, failure } : null;
    }
  },
  async voiceStatus() {
    return refreshVoiceStatus();
  },
  async runnerStatus({ refresh = false, timeoutMs = null, adoptSuperseding = true } = {}) {
    const generation = contextGeneration;
    if (!refresh && runnerStatusRequest && runnerStatusRequestGeneration === generation) {
      return runnerStatusRequest;
    }
    if (runnerStatusRequest) runnerStatusAbort?.abort();
    // Retain last-known state for passive chrome, but never hand it to an interactive surface
    // after a failed read or a workspace epoch change. The scheduled refresh is the sole recovery
    // path in that interval, and an old workspace's request can never block or populate the new one.
    if (!refresh && runnerStatusGeneration === generation && state.runnerStatusError) return null;
    if (!refresh && runnerStatusGeneration === generation && state.runner) return state.runner;
    const controller = new AbortController();
    const timeout = Number.isFinite(timeoutMs) && timeoutMs > 0
      ? setTimeout(() => controller.abort(), timeoutMs) : null;
    const revision = ++runnerStatusRequestRevision;
    const settlementRevision = turnSettlementRevision;
    const request = api.get("/api/runner", { signal: controller.signal }).then((runner) => {
      if (generation !== contextGeneration) return null;
      if (revision !== runnerStatusRequestRevision) {
        // A forced refresh superseded this read in the same authority. Its original callers
        // adopt the newest request instead of observing an AbortError-shaped null and falsely
        // reporting that runner state is unavailable.
        if (!adoptSuperseding) return null;
        return runnerStatusRequestGeneration === generation
          && runnerStatusRequest !== request ? runnerStatusRequest : null;
      }
      // A terminal frame is newer than a runner snapshot that was already in flight. Never let
      // that older snapshot resurrect the completed turn as busy; the terminal frame has already
      // settled the visible state and the next poll will obtain a post-terminal snapshot.
      if (settlementRevision !== turnSettlementRevision && runner?.turn_busy) {
        const settledRunner = state.runner || { ...runner, turn_busy: false, turn_id: null };
        state.runnerStatusError = false;
        runnerStatusGeneration = generation;
        if (!state.runner) reconcileRunnerContext(settledRunner);
        return settledRunner;
      }
      state.runnerStatusError = runner == null;
      runnerStatusGeneration = generation;
      if (runner) reconcileRunnerContext(runner);
      return runner;
    }).finally(() => {
      if (timeout !== null) clearTimeout(timeout);
      if (runnerStatusRequest === request) {
        runnerStatusRequest = null;
        runnerStatusAbort = null;
      }
    });
    runnerStatusRequest = request;
    runnerStatusRequestGeneration = generation;
    runnerStatusAbort = controller;
    return request;
  },
  async post(path, body, { signal } = {}) {
    const r = await fetch(path, {
      method: "POST",
      headers: workspaceHeaders({ ...expectedContextHeaders(), "content-type": "application/json" }),
      body: JSON.stringify(body || {}),
      signal,
    });
    if (!sessionAlive(r)) return { ok: false, status: r.status, data: {} };
    return { ok: r.ok, status: r.status, data: await r.json().catch(() => ({})) };
  },
  async stepUp(password) {
    const generation = workspaceGeneration;
    const startingWorkspace = workspaceId;
    const result = await api.post("/auth/step-up", { password });
    if (!result.ok) return result;
    if (!await waitForWorkspaceAfter(generation)) {
      return { ok: false, status: 409, data: { message: "secure workspace reconnect timed out" } };
    }
    if (!workspaceId || workspaceId === startingWorkspace) {
      return { ok: false, status: 409, data: { message: "secure workspace replacement was not observed" } };
    }
    return result;
  },
  async upload(path, body) {
    // FormData lets the browser set its own multipart boundary. The server still receives the
    // same authenticated, server-owned workspace handle as every other attended UI action.
    const r = await fetch(path, {
      method: "POST", headers: workspaceHeaders(expectedContextHeaders()), body,
    });
    if (!sessionAlive(r)) return { ok: false, status: r.status, data: {} };
    return { ok: r.ok, status: r.status, data: await r.json().catch(() => ({})) };
  },
  async download(path, filename) {
    // Keep the opaque, server-owned workspace handle on downloads too.  This avoids opening a
    // new unbound tab just to fetch an output from another chat/project.
    const r = await fetch(path, { headers: workspaceHeaders() });
    if (!sessionAlive(r)) return false;
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
  // A parked job is visible only after Task History returned its exact durable continuation.
  // This screen helper has no scheduler authority: the server revalidates the run, nonce, and
  // workspace before it delegates the decision to its host-composed resume callback.
  reviewParkedTask(approval) {
    const runId = Number(approval && approval.run_id);
    if (!Number.isInteger(runId) || runId < 1 || !state.context) return false;
    const projectId = approval.project_id == null ? null : Number(approval.project_id);
    if (projectId !== null && projectId !== state.context.project_id) return false;
    const pending = { ...approval, run_id: runId, nonce: null };
    state.parkedTaskApprovals.set(runId, pending);
    const controller = openParkedTaskApproval(pending, api, {
      onShown() { wsSend({ type: "parked_task_approval_shown", run_id: runId }); },
      onResolved() {
        state.parkedTaskApprovals.delete(runId);
        if (_parkedTaskDialog === controller) _parkedTaskDialog = null;
        refreshIfActive("tasks");
      },
      onRetry() {
        pending.nonce = null;
        wsSend({ type: "parked_task_approval_shown", run_id: runId });
      },
      onDismissed() {
        if (_parkedTaskDialog === controller) _parkedTaskDialog = null;
      },
    });
    if (!controller) {
      state.parkedTaskApprovals.delete(runId);
      return false;
    }
    _parkedTaskDialog = controller;
    return true;
  },
  async toggleVoiceCapture(mode, onTranscript) {
    if (!state.voice.enabled) {
      onVoiceState("error", state.voice.reason || "Voice is unavailable.");
      return;
    }
    const authorityToken = authorityGeneration;
    const navigationToken = navigationGeneration;
    await toggleTalk({
      mode,
      headers: workspaceHeaders(expectedContextHeaders()),
      onState: onVoiceState,
      onTranscript: (transcript) => {
        if (authorityToken !== authorityGeneration) return;
        // Same-authority navigation keeps the capture valid but retires the textarea callback
        // that started it. Preserve finalized dictation as a draft for the active/new Chat DOM.
        if (navigationToken !== navigationGeneration) api.restoreTurnDraft(transcript);
        else onTranscript?.(transcript);
      },
      isCurrent: () => authorityToken === authorityGeneration,
    });
  },
  cancelVoiceCapture() {
    const captureStopped = cancelCapture(onVoiceState);
    const captionStopped = stopCaption();
    if (!captureStopped && captionStopped) onVoiceState("idle");
  },
  // Resume a past chat into the live session AND load its transcript into the Daily conversation
  // view (so resuming actually shows the conversation). Returns false if a turn is in flight (409).
  async resumeChat(sessionId) {
    const startingAuthority = authorityGeneration;
    const startingWorkspace = workspaceId;
    const startingContext = state.context && { ...state.context };
    const ownsExactSuccessor = () => Boolean(
      startingContext
      && workspaceId === startingWorkspace
      && authorityGeneration === startingAuthority + 1
      && state.context?.session_id === sessionId
      && state.context?.context_revision === startingContext.context_revision + 1
    );
    const res = await api.post(`/api/sessions/${sessionId}/resume`, {});
    if (!res.ok) return false;
    // If the lifecycle echo won the race, it already cleared/hydrated the exact target. A
    // replacement workspace or a later context transition must never be navigated by this old
    // callback, even though its server response was successful.
    if (workspaceId !== startingWorkspace) return false;
    if (authorityGeneration !== startingAuthority) {
      return ownsExactSuccessor();
    }
    // The HTTP transition may beat or replace its WebSocket echo. Retire the old transcript
    // immediately, then force an authoritative runner snapshot to reconcile the target context.
    state.chat = [];
    state.chatAttachments = [];
    invalidateConversationHydration();
    refreshConversation();
    await api.runnerStatus({ refresh: true });
    if (!ownsExactSuccessor()) return false;
    const expectedContext = state.context;
    const expectedAuthority = authorityGeneration;
    const emptyChat = state.chat;
    const t = await api.get(`/api/sessions/${sessionId}`);
    if (workspaceId !== startingWorkspace || authorityGeneration !== expectedAuthority
        || state.context?.session_id !== expectedContext.session_id
        || state.context?.project_id !== expectedContext.project_id
        || state.context?.context_revision !== expectedContext.context_revision) return false;
    if (state.chat !== emptyChat || state.chat.length) return true;
    if (t && Array.isArray(t.messages)) {
      state.chat = hydrateTranscript(t);
      refreshConversation();
    }
    state.chatAttachments = [];
    return true;
  },
};

// --- WebSocket: heartbeat + surface state + event stream ---
let ws = null;
let heartbeatTimer = null;
let mounted = new Set();
let workspaceGeneration = 0;
let contextGeneration = 0;
let authorityGeneration = 0;
const workspaceWaiters = new Set();

function advanceContextGeneration() {
  contextGeneration += 1;
  if (runnerStatusRequest && runnerStatusRequestGeneration !== contextGeneration) {
    runnerStatusAbort?.abort();
  }
}

function reconcileRunnerContext(runner) {
  const priorContext = state.context;
  state.runner = runner;
  if (runner.session_id == null) return false;
  const idsChanged = Boolean(priorContext
    && (priorContext.session_id !== runner.session_id
      || priorContext.project_id !== (runner.project ? runner.project.id : null)));
  const contextRevision = validContextRevision(runner.context_revision)
    ?? (priorContext ? priorContext.context_revision + (idsChanged ? 1 : 0) : 1);
  const nextContext = {
    session_id: runner.session_id,
    project_id: runner.project ? runner.project.id : null,
    context_revision: contextRevision,
  };
  const contextChanged = Boolean(priorContext
    && (priorContext.session_id !== nextContext.session_id
      || priorContext.project_id !== nextContext.project_id
      || priorContext.context_revision !== nextContext.context_revision));
  const projectChanged = priorContext?.project_id !== nextContext.project_id;
  state.context = nextContext;
  if (contextChanged) {
    // Any consumer of runnerStatus may be the first observer after a missed lifecycle frame.
    // Reconcile before exposing the snapshot so chrome and writable screens cannot straddle two
    // server-owned contexts until the periodic poll happens to run.
    advanceContextGeneration();
    authorityGeneration += 1;
    runnerStatusGeneration = contextGeneration;
    clearAuthorityLocalState();
  }
  if (contextChanged || !priorContext) {
    renderRunnerState();
    rehydrateConversation();
    rerenderAfterContextChange({ projectChanged });
  }
  return contextChanged;
}

function waitForWorkspaceAfter(generation, timeoutMs = 7000) {
  if (workspaceGeneration > generation) return Promise.resolve(true);
  return new Promise((resolve) => {
    const waiter = () => { clearTimeout(timer); workspaceWaiters.delete(waiter); resolve(true); };
    const timer = setTimeout(() => { workspaceWaiters.delete(waiter); resolve(false); }, timeoutMs);
    workspaceWaiters.add(waiter);
  });
}

function wsSend(obj) { if (ws && ws.readyState === 1) ws.send(JSON.stringify(obj)); }

// A reconnect creates a new socket, so its heartbeat must replace (never join) the old one.
// Keep the timer handle outside the socket so both reconnect and close can cancel it.
function clearHeartbeat() {
  if (heartbeatTimer !== null) {
    clearInterval(heartbeatTimer);
    heartbeatTimer = null;
  }
}

function startHeartbeat(socket) {
  clearHeartbeat();
  heartbeatTimer = setInterval(() => {
    // A late timer from a replaced socket must never beat the current connection.
    if (ws === socket && socket.readyState === 1) socket.send(JSON.stringify({ type: "heartbeat" }));
  }, 5000);
}

function setSurface(name, on) {
  if (on && !mounted.has(name)) { mounted.add(name); wsSend({ type: "surface", surface: name, mounted: true }); }
  if (!on && mounted.has(name)) { mounted.delete(name); wsSend({ type: "surface", surface: name, mounted: false }); }
}

function connect() {
  clearHeartbeat();
  // Only the newly assigned socket can reach handleMessage. Open a fresh process-epoch window
  // before any global frame can arrive; the workspace handshake will then pin its exact epoch.
  meetingRecordingEpoch = null;
  meetingRecordingRevision = -1;
  lastPublishedVoiceStatus = null;
  voiceStatusSequence += 1;
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const socket = new WebSocket(`${proto}://${location.host}/ws`);
  ws = socket;
  socket.onopen = () => {
    if (ws !== socket) return;
    wsSend({ type: "hello", surfaces: [...mounted], workspace_id: workspaceId });
    startHeartbeat(socket);
    resetApprovalNoncesForSocket();
  };
  socket.onmessage = (e) => {
    if (ws === socket) handleMessage(JSON.parse(e.data));
  };
  socket.onclose = (event) => {
    // A late close from a superseded socket cannot tear down the new connection's heartbeat.
    if (ws !== socket) return;
    clearHeartbeat();
    ws = null;
    if (event.code === 1008) {
      // Logout/recovery/session expiry close live authority with policy code 1008. Allow a
      // concurrent step-up response to install its replacement cookie before deciding whether
      // this browser must return to login.
      setTimeout(async () => {
        try {
          const response = await fetch("/auth/session", { headers: { "accept": "application/json" } });
          if (response.status === 401) location.replace("/login");
          else if (response.ok) connect();
          else setTimeout(connect, 1500);
        } catch {
          setTimeout(connect, 1500);
        }
      }, 200);
      return;
    }
    setTimeout(connect, 1500); // best-effort reconnect
  };
}

function handleMessage(msg) {
  if (msg.type === "workspace") {
    const nextWorkspaceId = msg.workspace_id || null;
    const priorContext = state.context;
    const idsChanged = Boolean(priorContext
      && (priorContext.session_id !== msg.session_id
        || priorContext.project_id !== msg.project_id));
    const contextRevision = validContextRevision(msg.context_revision);
    if (contextRevision === null) return;
    if (priorContext && workspaceId === nextWorkspaceId
        && (contextRevision < priorContext.context_revision
          || (contextRevision === priorContext.context_revision && idsChanged))) return;
    const sameWorkspaceContext = workspaceId === nextWorkspaceId
      && priorContext?.session_id === msg.session_id
      && priorContext?.project_id === msg.project_id
      && priorContext?.context_revision === contextRevision;
    const workspaceChanged = workspaceId !== nextWorkspaceId;
    const contextChanged = !priorContext || idsChanged
      || priorContext.context_revision !== contextRevision;
    const authorityChanged = workspaceChanged || contextChanged;
    workspaceId = nextWorkspaceId;
    if (workspaceId) {
      writeStored("session", WORKSPACE_KEY, workspaceId);
      // Unlike appearance preferences, workspace routing must not become stale on a same-tab
      // rollback to a cached Kairo shell. Keep the exact compatibility alias synchronized.
      writeStored("session", LEGACY_WORKSPACE_KEY, workspaceId);
    } else {
      removeStored("session", [WORKSPACE_KEY, LEGACY_WORKSPACE_KEY]);
    }
    advanceContextGeneration();
    if (authorityChanged) authorityGeneration += 1;
    state.context = {
      session_id: msg.session_id, project_id: msg.project_id, context_revision: contextRevision,
    };
    // Local phase revisions are owned by this workspace. Global mic revisions are process-wide:
    // a frame can arrive before this handshake, so reset them only when the server epoch changes.
    const handshakeRecordingEpoch = wireEpoch(msg.meeting_recording_epoch);
    if (handshakeRecordingEpoch && handshakeRecordingEpoch !== meetingRecordingEpoch) {
      meetingRecordingEpoch = handshakeRecordingEpoch;
      meetingRecordingRevision = -1;
    }
    lastPublishedVoiceStatus = null;
    voiceStatusSequence += 1;
    if (authorityChanged) {
      clearWorkspaceLocalState();
      // Meeting workflow is workspace-local. Never carry an old tab/session's recording phase
      // into a replacement workspace while its own status is still loading or unavailable.
      state.voice.meeting = "idle";
      state.voice.meeting_revision = -1;
      meetingStateRevision = -1;
      meetingEventGeneration += 1;
      busEmit("meeting_state", { state: "idle", revision: null, workspace_reset: true });
    }
    workspaceGeneration += 1;
    for (const waiter of [...workspaceWaiters]) waiter();
    pollStatus({ refreshChatHeader: sameWorkspaceContext });
    rerenderAfterContextChange({ projectChanged: true });
    return;
  }
  const lifecycle = ["project_changed", "session_new", "session_resumed"].includes(msg.kind);
  if (msg.workspace_id && msg.workspace_id !== workspaceId) return;
  if (msg.kind === "runner_state") {
    // Global runner broadcasts intentionally carry no workspace authority. Each tab refreshes
    // only its own scoped runner snapshot instead of trusting process-wide state on the wire.
    void refreshRunnerStatus({ refreshChatHeader: true });
    return;
  }
  if (lifecycle && msg.workspace_id === workspaceId) {
    const revision = validContextRevision(msg.context_revision);
    const current = state.context;
    if (revision === null || (current && (
      revision < current.context_revision
      || (revision === current.context_revision
        && (msg.session_id !== current.session_id || msg.project_id !== current.project_id))
    ))) return;
  }
  if (msg.session_id != null && !acceptsContext(msg) && !(lifecycle && msg.workspace_id === workspaceId)) return;
  if (msg.type === "approval") { onApproval(msg); return; }
  if (msg.type === "approval_nonce") { onNonce(msg); return; }
  if (msg.type === "parked_task_approval_nonce") { onParkedTaskNonce(msg); return; }
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
  if (msg.kind === "project_services_changed") {
    // The server has already refreshed this exact workspace's immutable project context. Re-read
    // every mounted surface that reports service/capability truth; the frame carries no settings
    // payload, so a socket can never become an optimistic authority for the selection itself.
    busEmit("project_services_changed", msg);
    const invalidatedProjectId = dismissProjectServiceAccess();
    refreshHeader();
    let rendered = Promise.resolve();
    if (["daily", "hub", "projects", "settings", "studio", "workspace"].includes(state.route)) {
      rendered = renderRoute();
    }
    if (invalidatedProjectId !== null) {
      showToast("Service access changed in another workspace. Review the latest setting.");
      rendered.then(() => {
        document.querySelector(
          `[data-service-access-project="${invalidatedProjectId}"]`,
        )?.focus?.();
      }).catch(() => {});
    }
    return;
  }
  if (msg.kind === "project_changed") {
    // A scope switch started a fresh scoped conversation server-side — clear the local view.
    if (state.runner) state.runner.project = { id: msg.project_id, name: msg.name };
    const priorContext = state.context;
    const contextRevision = validContextRevision(msg.context_revision);
    if (contextRevision === null) return;
    if (priorContext?.session_id === msg.session_id
        && priorContext?.project_id === msg.project_id
        && priorContext?.context_revision === contextRevision) {
      renderRunnerState(); refreshHeader(); return;
    }
    advanceContextGeneration();
    authorityGeneration += 1;
    state.context = {
      session_id: msg.session_id, project_id: msg.project_id, context_revision: contextRevision,
    };
    clearAuthorityLocalState({ clearRunner: true });
    renderRunnerState(); rerenderAfterContextChange({ projectChanged: true }); return;
  }
  if (msg.kind === "session_new" || msg.kind === "session_resumed") {
    const priorContext = state.context;
    const contextRevision = validContextRevision(msg.context_revision);
    if (contextRevision === null) return;
    const projectChanged = priorContext?.project_id !== msg.project_id;
    if (priorContext?.session_id === msg.session_id
        && priorContext?.project_id === msg.project_id
        && priorContext?.context_revision === contextRevision) {
      pollStatus(); return;
    }
    advanceContextGeneration();
    authorityGeneration += 1;
    state.context = {
      session_id: msg.session_id, project_id: msg.project_id, context_revision: contextRevision,
    };
    clearAuthorityLocalState({ clearRunner: true });
    pollStatus(); rerenderAfterContextChange({ projectChanged }); return;
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
  if (msg.kind === "meeting_state") {
    onMeetingState(msg.state, msg.revision);
    return;
  }
  if (msg.kind === "meeting_recording") {
    onMeetingRecording(msg.active, msg.revision, msg.epoch);
    return;
  }
  if (msg.kind === "turn_cancelled" || msg.kind === "turn_error") {
    turnSettlementRevision += 1;
    if (state.runner) {
      state.runner.turn_busy = false;  // settle: the turn ended
      state.runner.turn_id = null;
    }
    // A top-level turn approval is owned by this Chat frame and can retire immediately. A
    // subagent approval is provenance-ambiguous (Studio uses the same subagent manager), so
    // remove it only after the exact workspace's approval read model says it is gone.
    const ambiguousSubagents = subagentPendingDecisionIds();
    clearTurnPendingApprovals();
    void reconcilePendingApprovalIds(ambiguousSubagents, authorityGeneration);
    // Live drafts from an interrupted or failed provider request are not durable protocol
    // messages. Drop only those drafts; completed tool-round text was settled earlier.
    state.chat = state.chat.filter((item) => item.role !== "assistant" || !item.live);
    const text = msg.kind === "turn_cancelled" ? "(stopped)" : "(unable to complete this turn)";
    state.chat.push({ role: "assistant", text });
    refreshConversation();
    renderRunnerState();
  }
}

// The server already targets exact contexts.  This is a consumer-side backstop: a stale queued
// frame, reconnect, or future emitter cannot mutate this tab from another chat/project.
function acceptsContext(msg) {
  const c = state.context;
  const revision = validContextRevision(msg.context_revision);
  return !!c && msg.session_id === c.session_id && msg.project_id === c.project_id
    && revision === c.context_revision;
}

function validContextRevision(value) {
  const revision = Number(value);
  return Number.isInteger(revision) && revision > 0 ? revision : null;
}

// Background notices (job/reminder/digest) reach the browser here — the calm, non-modal
// surface (never an attention grab). A digest notice quietly refreshes Daily's Briefing.
function onNotice(notice) {
  // Project scope is server-owned, but reject a stale queued frame client-side as well. A notice
  // without exact provenance is never safe to show in the currently selected workspace.
  if (!notice || !state.context || notice.project_id !== state.context.project_id) return;
  state.notices.unshift(notice);
  if (state.notices.length > 50) state.notices.pop();
  busEmit("notice", notice);
  if (notice.kind === "digest") {
    refreshConversation();
  } else {
    refreshIfActive("daily");  // the latest-notification card is current without navigation
  }
  refreshIfActive("gate");        // durable notice history + attention queue share this surface
  if (notice.kind === "task") {
    refreshIfActive("tasks");
    refreshWorkspaceTabs("tasks");
  }
}

// Voice round-trip, made visible in Daily (one heard bubble + one safe caption). The reply
// text is the renderer's post-privacy output — the UI never sees a raw answer or a payload.
function onVoice(msg) {
  state.chat.push(msg.role === "heard" ? { role: "heard", text: msg.text } : { role: "assistant", text: msg.text });
  refreshConversation();
  // Optional playback: speak the SAFE reply caption (the server masks + caps before TTS). The
  // caption is always on screen too, so playback is a best-effort enhancement, never the record.
  if (msg.role !== "heard") {
    const authorityToken = authorityGeneration;
    playCaption(
      msg.text,
      workspaceHeaders(),
      onVoiceState,
      () => authorityToken === authorityGeneration,
    );
  }
}

// Read-only conversation/dictation voice-state pill — content-free. Meeting workflow and the
// process-wide physical-microphone signal use their own channels below.
const VOICE_LABELS = {
  listening: "⏹ Stop", capturing: "🎤 Capturing…", recording: "🎤 Meeting note…",
  transcribing: "🎤 Transcribing…", saving: "🎤 Saving…",
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

function wireRevision(value) {
  const revision = Number(value);
  return Number.isInteger(revision) && revision >= 0 ? revision : null;
}

function wireEpoch(value) {
  return typeof value === "string" && value.length > 0 ? value : null;
}

function onMeetingState(s, revision) {
  const nextRevision = wireRevision(revision);
  if (nextRevision !== null && nextRevision < meetingStateRevision) return;
  if (nextRevision !== null) meetingStateRevision = nextRevision;
  meetingEventGeneration += 1;
  state.voice.meeting = s;
  busEmit("meeting_state", { state: s, revision: nextRevision });
}

function onMeetingRecording(active, revision, epoch) {
  const nextEpoch = wireEpoch(epoch);
  if (nextEpoch && meetingRecordingEpoch && nextEpoch !== meetingRecordingEpoch) return;
  if (nextEpoch && !meetingRecordingEpoch) meetingRecordingEpoch = nextEpoch;
  const nextRevision = wireRevision(revision);
  if (nextRevision !== null && nextRevision < meetingRecordingRevision) return;
  if (nextRevision !== null) meetingRecordingRevision = nextRevision;
  meetingRecordingEventGeneration += 1;
  state.voice.meeting_recording = Boolean(active);
  showMeetingRecording(state.voice.meeting_recording);
}

function showMeetingRecording(active) {
  const recordingNow = Boolean(active);
  document.querySelectorAll("[data-meeting-rec-dot]").forEach((dot) => {
    dot.classList.toggle("show", recordingNow);
  });
  const accessibleStatus = document.getElementById("meeting-recording-status");
  const next = recordingNow ? "true" : "false";
  if (accessibleStatus && accessibleStatus.dataset.active !== next) {
    const wasRecording = accessibleStatus.dataset.active === "true";
    accessibleStatus.dataset.active = next;
    accessibleStatus.textContent = recordingNow
      ? "Workstation microphone is recording a meeting note."
      : (wasRecording ? "Workstation microphone closed." : "");
  }
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
  if (evt.type === "turn_completed" && state.runner) {
    turnSettlementRevision += 1;
    state.runner.turn_busy = false;
    state.runner.turn_id = null;
  } else if (evt.type === "turn_completed") {
    turnSettlementRevision += 1;
  }
  onConversationEvent(state, evt);
  refreshConversation();
  refreshIfActive("trace");
  if (evt.type === "turn_completed") {
    // A completed turn can change the current scope's derived memory/knowledge. Re-fetch only
    // if one of those read-only panels is visible; a stale event cannot change scope server-side.
    refreshIfActive("memory");
    if (!vaultHasDraft()) {
      refreshIfActive("vault");
      refreshWorkspaceTabs("vault");
    }
    refreshWorkspaceTabs("memory");
    renderRunnerState();
    pollStatus();
  }
}

// --- approvals: the priority attention surface ---
let _approvalEsc = null; // unregister fn for the Escape-closes-modal binding (keys.js)
let _approvalRestoreFocus = null;
let _approvalNonceTimer = null;
let _approvalInerted = [];
const _approvalResolving = new Set();
const _approvalRecovering = new Set();
const TURN_APPROVAL_KIND = "turn";
const SUBAGENT_APPROVAL_KIND = "subagent";
let _parkedTaskDialog = null;
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

function setApprovalControlsEnabled(enabled) {
  const always = document.getElementById("ap-always");
  for (const id of ["ap-approve", "ap-always", "ap-deny"]) {
    document.getElementById(id).disabled = !enabled || (id === "ap-always" && always.hidden);
  }
}

function setApprovalBackgroundInert(inert) {
  const overlay = document.getElementById("overlay");
  if (inert) {
    if (_approvalInerted.length) return;
    for (const child of document.body.children) {
      if (child === overlay || child.id === "runner-control-feedback"
          || child.tagName === "SCRIPT" || child.inert) continue;
      child.inert = true;
      _approvalInerted.push(child);
    }
    return;
  }
  for (const child of _approvalInerted) child.inert = false;
  _approvalInerted = [];
}

function clearApprovalNonceTimer() {
  if (_approvalNonceTimer !== null) {
    clearTimeout(_approvalNonceTimer);
    _approvalNonceTimer = null;
  }
}

function setApprovalStatus(message, { busy = false, error = false, retry = false } = {}) {
  document.getElementById("ap-waiting").textContent = message;
  document.getElementById("ap-spin").classList.toggle("show", busy);
  document.getElementById("ap-status").classList.toggle("error", error);
  document.getElementById("ap-retry").hidden = !retry;
  document.getElementById("approval-dialog").setAttribute("aria-busy", String(busy));
}

function requestApprovalNonce(p, waitingMessage = "") {
  const overlay = document.getElementById("overlay");
  if (!overlay.classList.contains("show") || overlay.dataset.decision !== p.decision_id) {
    return false;
  }
  clearApprovalNonceTimer();
  p.nonce = null;
  setApprovalControlsEnabled(false);
  const prefix = p.error ? `${p.error} ` : "";
  const progress = waitingMessage
    || (p.error ? "Preparing a fresh secure confirmation…" : "Preparing secure confirmation…");
  setApprovalStatus(`${prefix}${progress}`, {
    busy: true,
    error: !!p.error,
  });
  wsSend({ type: "approval_shown", decision_id: p.decision_id });
  _approvalNonceTimer = setTimeout(() => {
    _approvalNonceTimer = null;
    if (state.pending.get(p.decision_id) !== p || p.nonce
        || _approvalResolving.has(p.decision_id)) return;
    const delayed = p.error
      ? `${p.error} A fresh confirmation could not be prepared yet.`
      : "Secure confirmation is taking longer than expected.";
    setApprovalStatus(`${delayed} Retry when the connection is ready.`, {
      error: true,
      retry: true,
    });
  }, 7000);
  return true;
}

function resetApprovalNoncesForSocket() {
  for (const pending of state.pending.values()) pending.nonce = null;
  const overlay = document.getElementById("overlay");
  if (!overlay?.classList.contains("show")) return;
  const pending = state.pending.get(overlay.dataset.decision);
  if (pending && !_approvalResolving.has(pending.decision_id)
      && !_approvalRecovering.has(pending.decision_id)) {
    requestApprovalNonce(pending, "Connection changed. Preparing a fresh confirmation…");
  }
}

async function recoverApproval(p, message) {
  if (_approvalRecovering.has(p.decision_id)) return;
  _approvalRecovering.add(p.decision_id);
  clearApprovalNonceTimer();
  try {
    p.error = message;
    const overlay = document.getElementById("overlay");
    if (overlay.dataset.decision === p.decision_id) {
      setApprovalStatus(`${message} Checking the current approval state…`, {
        busy: true,
        error: true,
      });
    }
    const snapshot = await api.get("/api/approvals");
    if (state.pending.get(p.decision_id) !== p) return;
    if (Array.isArray(snapshot?.pending)
        && !snapshot.pending.some((item) => item.decision_id === p.decision_id)) {
      state.pending.delete(p.decision_id);
      if (overlay.dataset.decision === p.decision_id) hideApproval();
      updateGateBadge();
      refreshIfActive("gate");
      showTopApproval();
      return;
    }
    if (!requestApprovalNonce(p)) {
      p._shown = false;
      showTopApproval();
    }
  } finally {
    _approvalRecovering.delete(p.decision_id);
  }
}

function showTopApproval() {
  const next = [...state.pending.values()].find((p) => !p._shown);
  if (!next) return;
  const overlay = document.getElementById("overlay");
  if (overlay.classList.contains("show")) return; // one attention surface at a time
  next._shown = true;
  next.nonce = null;
  const [label, request] = approvalCopy(next);
  document.getElementById("ap-kind").textContent =
    next.kind === "voice" ? "Confirm on screen (voice)" : "Kairo needs your approval";
  document.getElementById("ap-tool").textContent = label;
  document.getElementById("ap-request").textContent = request;
  document.getElementById("ap-details").open = false;
  document.getElementById("ap-payload").textContent = JSON.stringify(next.input, null, 2);
  document.getElementById("ap-reason").textContent = next.reason || "";
  // No "always" for voice (per-instance only) OR a non-persistable decision (tainted egress:
  // a private read happened this turn, so sending off-box must not become a standing grant).
  const noAlways = next.kind === "voice" || next.persistable === false;
  document.getElementById("ap-always").hidden = noAlways;
  setApprovalControlsEnabled(false);
  overlay.dataset.decision = next.decision_id;
  _approvalRestoreFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null;
  overlay.classList.add("show");
  setApprovalBackgroundInert(true);
  const dialog = document.getElementById("approval-dialog");
  if (_approvalEsc) _approvalEsc();
  _approvalEsc = pushEscape(hideApproval, dialog);   // Escape dismisses (leaves the item pending)
  dialog.focus();
  setSurface("gate", true);                          // the screen is now watching
  requestApprovalNonce(next);                        // prove visibility ⇒ mint a bound nonce
}

function onNonce(msg) {
  const p = state.pending.get(msg.decision_id);
  const overlay = document.getElementById("overlay");
  const visible = overlay.classList.contains("show")
    && overlay.dataset.decision === msg.decision_id;
  if (!p || !visible || _approvalResolving.has(msg.decision_id)
      || _approvalRecovering.has(msg.decision_id)
      || typeof msg.nonce !== "string" || !msg.nonce) return;
  clearApprovalNonceTimer();
  p.nonce = msg.nonce;
  const retry = p.error ? `${p.error} A fresh confirmation is ready; choose again.` : "";
  setApprovalStatus(retry || "Confirm below to commit this action.", { error: !!p.error });
  setApprovalControlsEnabled(true);
}

function onParkedTaskNonce(msg) {
  const runId = Number(msg.run_id);
  const pending = state.parkedTaskApprovals.get(runId);
  if (!pending || typeof msg.nonce !== "string") return;
  pending.nonce = msg.nonce;
  if (_parkedTaskDialog && _parkedTaskDialog.runId === runId) {
    _parkedTaskDialog.setNonce(msg.nonce);
  }
}

async function resolveApproval(action) {
  const overlay = document.getElementById("overlay");
  const did = overlay.dataset.decision;
  const p = state.pending.get(did);
  if (!p) { hideApproval(); return; }
  if (!p.nonce || _approvalResolving.has(did)) return;
  const nonce = p.nonce;
  p.nonce = null; // never reuse a credential whose delivery outcome could become uncertain
  p.error = "";
  _approvalResolving.add(did);
  setApprovalControlsEnabled(false);
  setApprovalStatus(action === "deny" ? "Rejecting this action…" : "Submitting approval…", {
    busy: true,
  });
  let result;
  try {
    result = await api.post(`/api/approvals/${did}/resolve`, { nonce, action });
  } catch {
    _approvalResolving.delete(did);
    if (state.pending.get(did) !== p) return;
    await recoverApproval(p, "Not confirmed because Kairo could not reach the approval service.");
    return;
  }
  _approvalResolving.delete(did);
  if (state.pending.get(did) !== p) return;
  if (!result.ok || result.data?.ok !== true) {
    const detail = String(result.data?.message || "The secure confirmation was rejected.");
    await recoverApproval(p, `Not confirmed: ${detail.replace(/[.\s]+$/, "")}.`);
    return;
  }
  state.pending.delete(did);
  if (overlay.dataset.decision === did) hideApproval();
  updateGateBadge();
  refreshIfActive("gate");
  showTopApproval(); // surface the next pending approval, if any
}

function hideApproval() {
  clearApprovalNonceTimer();
  if (_approvalEsc) { _approvalEsc(); _approvalEsc = null; }
  const overlay = document.getElementById("overlay");
  const wasShown = overlay.classList.contains("show");
  const pending = state.pending.get(overlay.dataset.decision);
  if (pending) pending.nonce = null;
  overlay.classList.remove("show");
  overlay.dataset.decision = "";
  setApprovalBackgroundInert(false);
  setApprovalControlsEnabled(false);
  document.getElementById("approval-dialog").setAttribute("aria-busy", "false");
  if (state.route !== "gate") setSurface("gate", false); // stop advertising the screen
  const restore = _approvalRestoreFocus;
  _approvalRestoreFocus = null;
  if (wasShown && restore instanceof HTMLElement && restore.isConnected) restore.focus();
}

function clearPendingApprovals({ refresh = true } = {}) {
  state.pending.clear();
  _approvalResolving.clear();
  _approvalRecovering.clear();
  hideApproval();
  updateGateBadge();
  if (refresh) refreshIfActive("gate");
}

function clearPendingApprovalIds(decisionIds, { refresh = true } = {}) {
  let changed = false;
  for (const decisionId of decisionIds) {
    changed = state.pending.delete(decisionId) || changed;
    _approvalResolving.delete(decisionId);
    _approvalRecovering.delete(decisionId);
  }
  if (!changed) return;
  const overlay = document.getElementById("overlay");
  if (decisionIds.has(overlay.dataset.decision)) hideApproval();
  updateGateBadge();
  if (refresh) refreshIfActive("gate");
  showTopApproval();
}

function pendingDecisionIdsOfKind(kind) {
  return new Set(
    [...state.pending.entries()]
      .filter(([, pending]) => pending.kind === kind)
      .map(([decisionId]) => decisionId),
  );
}

function turnPendingDecisionIds() {
  return pendingDecisionIdsOfKind(TURN_APPROVAL_KIND);
}

function subagentPendingDecisionIds() {
  return pendingDecisionIdsOfKind(SUBAGENT_APPROVAL_KIND);
}

function clearTurnPendingApprovals({ refresh = true } = {}) {
  clearPendingApprovalIds(turnPendingDecisionIds(), { refresh });
}

async function reconcilePendingApprovalIds(
  decisionIds,
  authorityToken,
  timeoutMs = RUNNER_CONTROL_RECONCILE_TIMEOUT_MS,
) {
  if (!decisionIds.size) return true;
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), timeoutMs);
  let snapshot = null;
  try {
    snapshot = await api.get("/api/approvals", { signal: controller.signal });
  } finally {
    clearTimeout(timeout);
  }
  if (authorityToken !== authorityGeneration || !Array.isArray(snapshot?.pending)) return false;
  const live = new Set(snapshot.pending.map((pending) => pending.decision_id));
  clearPendingApprovalIds(new Set([...decisionIds].filter((decisionId) => !live.has(decisionId))));
  return true;
}

function clearAuthorityLocalState({ clearRunner = false } = {}) {
  api.cancelVoiceCapture();
  closePalette();
  clearPendingApprovals({ refresh: false });
  state.parkedTaskApprovals.clear();
  _parkedTaskDialog?.dismiss?.();
  _parkedTaskDialog = null;
  dismissTaskDialogs();
  dismissMemoryDraft();
  dismissFeedbackDialogs();
  dismissProjectDialogs();
  dismissProjectReport();
  state.chat = [];
  state.chatAttachments = [];
  state.projectImport = null;
  state.turnCancelling = false;
  state.turnAdmission = null;
  state.turnDraft = null;
  state.notices = [];
  state.trace = [];
  if (clearRunner) {
    state.runner = null;
    state.runnerStatusError = false;
  }
  invalidateConversationHydration();
  if (clearRunner) renderRunnerState();
}

function clearWorkspaceLocalState() {
  clearAuthorityLocalState({ clearRunner: true });
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
  syncRunnerControlCopies(runnerFacts(runner));
  syncStatusChrome(runner);
}

function runnerFacts(runner = state.runner || {}) {
  const statusCurrent = !state.runnerStatusError;
  const runnerAvailable = runner.runner_available === true;
  const currentTurnBusy = !!runner.turn_busy;
  const globalTurnBusy = currentTurnBusy || !!runner.global_turn_busy;
  const backgroundBusy = !!runner.background_busy;
  const workActive = globalTurnBusy || backgroundBusy;
  const turnApprovalPending = [...state.pending.values()]
    .some((pending) => pending.kind === TURN_APPROVAL_KIND);
  return {
    statusCurrent,
    runnerAvailable,
    pauseAvailable: workActive || turnApprovalPending || (statusCurrent && runnerAvailable),
    resumeAvailable: statusCurrent && runnerAvailable,
    currentTurnBusy,
    globalTurnBusy,
    backgroundBusy,
    workActive,
    paused: runnerAvailable && runner.runner_running === false,
  };
}

function syncRunnerControlCopies(facts = runnerFacts()) {
  const operation = runnerControlOperation;
  for (const control of document.querySelectorAll("[data-runner-control]")) {
    const action = control.dataset.runnerControl;
    const approvalCopy = control.dataset.runnerControlSurface === "approval";
    const stopping = operation?.action === "pause";
    const resuming = operation?.action === "resume";
    const stopVisible = operation ? stopping : (facts.pauseAvailable
      && (facts.workActive || (approvalCopy && state.pending.size > 0)));
    const resumeVisible = operation ? resuming : (facts.resumeAvailable && facts.paused);
    const visible = action === "pause" ? stopVisible : resumeVisible;
    control.classList.toggle("is-hidden", !visible);
    control.disabled = !!operation
      || (action === "pause" ? !facts.pauseAvailable : !facts.resumeAvailable);
    if (action === "pause") {
      control.textContent = stopping ? "Stopping…" : "Stop all";
      control.setAttribute("aria-label", stopping
        ? "Stopping all chats and pausing schedules"
        : "Stop all chats and pause schedules");
    } else {
      control.textContent = resuming ? "Resuming…" : "Resume schedules";
      control.setAttribute("aria-label", resuming
        ? "Resuming schedules; stopped chats stay stopped"
        : "Resume schedules; stopped chats stay stopped");
    }
  }
  const emergency = document.querySelector(".approval-emergency");
  const approvalStop = document.getElementById("ap-stop-all");
  if (emergency && approvalStop) {
    emergency.classList.toggle("is-hidden", approvalStop.classList.contains("is-hidden"));
  }
}

function syncStatusChrome(runner = state.runner || {}) {
  const status = document.querySelector(".status");
  if (!status) return;
  const facts = runnerFacts(runner);
  const controlling = !!runnerControlOperation;
  const stopping = runnerControlOperation?.action === "pause";
  const resuming = runnerControlOperation?.action === "resume";
  // Idle is not a user task. Keep the global chrome out of the way unless work is active or a
  // non-Chat surface needs an approval shortcut; Chat already owns its own approval pill.
  const attentionElsewhere = state.pending.size > 0 && state.route !== "chat";
  status.classList.toggle("status-active", facts.workActive || facts.paused
    || controlling || attentionElsewhere);
  status.classList.toggle("is-working", facts.workActive || controlling);
  status.classList.toggle("is-paused", facts.paused && !controlling);
  status.classList.toggle("is-controlling", controlling);
  status.classList.toggle("is-stopping", stopping);
  status.classList.toggle("is-resuming", resuming);
  status.classList.toggle("has-global-work", facts.workActive && !controlling);
}

function announceRunnerControl(message) {
  const feedback = document.getElementById("runner-control-feedback");
  if (feedback) feedback.textContent = message;
}

function cancelledTurnCount(data) {
  const cancelled = data?.cancelled_turns;
  if (Array.isArray(cancelled)) return cancelled.length;
  const count = Number(cancelled);
  return Number.isInteger(count) && count >= 0 ? count : null;
}

function runnerControlSuccessMessage(action, data, reconciled) {
  const runner = state.runner || {};
  if (action === "resume") {
    if (!reconciled) {
      return "Resume schedules completed. Current schedule status will refresh automatically. Stopped chats stay stopped.";
    }
    if (runner.runner_available === false) {
      return "Schedules are unavailable. Stopped chats stay stopped.";
    }
    if (runner.runner_running === true) {
      return "Schedules resumed. Stopped chats stay stopped.";
    }
    return "Resume schedules completed, but schedules are currently paused. Stopped chats stay stopped.";
  }

  const count = cancelledTurnCount(data);
  const stopped = count === null ? "Stop all completed"
    : (count === 0 ? "No live chats needed stopping"
      : `Stopped ${count} live chat${count === 1 ? "" : "s"}`);
  if (!reconciled) return `${stopped}. Current schedule status will refresh automatically.`;
  const newChat = runner.global_turn_busy ? " Another live chat is active." : "";
  let schedule = "Current schedule status is still refreshing.";
  if (runner.runner_available === false) schedule = "Schedules are unavailable.";
  else if (runner.runner_running === false) schedule = "Schedules are paused.";
  else if (runner.runner_running === true) schedule = "Schedules are currently running.";
  return `${stopped}.${newChat} ${schedule}`;
}

async function runRunnerControl(action) {
  if (runnerControlOperation) return runnerControlOperation.promise;
  const facts = runnerFacts();
  if (action === "pause" && !facts.pauseAvailable) return false;
  if (action === "resume" && !facts.resumeAvailable) return false;
  if (action === "pause" && !facts.workActive && state.pending.size === 0) return false;
  if (action === "resume" && !facts.paused) return false;
  if (action !== "pause" && action !== "resume") return false;

  const operation = {
    id: ++runnerControlSequence,
    action,
    authorityToken: authorityGeneration,
    turnDecisionIds: turnPendingDecisionIds(),
    subagentDecisionIds: subagentPendingDecisionIds(),
    promise: null,
  };
  runnerControlOperation = operation;
  const startingMessage = action === "pause"
    ? "Stopping all live chats and pausing schedules…"
    : "Resuming schedules… Stopped chats stay stopped.";
  announceRunnerControl(startingMessage);
  renderRunnerState();

  operation.promise = (async () => {
    let result = null;
    let failed = false;
    let reconciled = false;
    try {
      try {
        const endpoint = action === "pause" ? "/api/runner/pause" : "/api/runner/resume";
        result = await api.post(endpoint, {});
        failed = !result.ok;
        // The POST snapshot can already be stale if another tab starts work while a scheduled
        // job drains. The scoped GET below is the sole ongoing source of runner/turn state.
      } catch {
        failed = true;
      }

      // A write response is not the ongoing source of truth. Re-read this tab's scoped status on
      // success, rejection, and transport ambiguity before releasing any control copy.
      try {
        reconciled = !!await refreshRunnerStatus({
          refreshChatHeader: true,
          timeoutMs: RUNNER_CONTROL_RECONCILE_TIMEOUT_MS,
          adoptSuperseding: false,
        });
      } catch {
        state.runnerStatusError = true;
      }
      if (runnerControlOperation !== operation) return false;
      if (!failed && action === "pause"
          && operation.authorityToken === authorityGeneration
          && !state.runner?.turn_busy) {
        // Terminal websocket frames normally retire top-level turn approvals first. This
        // fallback is limited to decisions present when Stop began, while ambiguous subagent
        // approvals are removed only when the server confirms they did not survive. Studio,
        // voice, and newly admitted approvals are therefore outside Global Stop's cleanup.
        clearPendingApprovalIds(operation.turnDecisionIds);
        await reconcilePendingApprovalIds(
          operation.subagentDecisionIds,
          operation.authorityToken,
        );
      }
    } finally {
      // The shell owns this lock, not the mounted route. Release it even if reconciliation or a
      // render helper unexpectedly fails so every copy can recover on the next runner snapshot.
      if (runnerControlOperation === operation) {
        runnerControlOperation = null;
        renderRunnerState();
      }
    }

    if (failed) {
      const statusNote = reconciled
        ? "Current runner status was refreshed"
        : "Current runner status could not be refreshed yet and will retry automatically";
      const message = action === "pause"
        ? `Could not confirm Stop all. ${statusNote}; try again if work is still active.`
        : `Could not confirm Resume schedules. ${statusNote}; stopped chats remain stopped.`;
      announceRunnerControl(message);
      showToast(message, "error");
      return false;
    }

    const message = runnerControlSuccessMessage(action, result?.data, reconciled);
    announceRunnerControl(message);
    showToast(message);
    return true;
  })();
  return operation.promise;
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

// A route render is a small transaction. Async screens must never keep the prior screen usable
// while they load, overwrite a newer route when their read returns late, or reject into a blank
// shell. The live container is replaced only when the route identity changes (or the user retries),
// so ordinary same-screen refreshes still preserve drafts and focus.
let routeRenderGeneration = 0;
let activeRouteKey = null;
let activeRouteController = null;
let navigationGeneration = 0;
const ROUTE_RENDER_STALE = Symbol("route-render-stale");
let kiraAppReadySignaled = false;

function signalKiraAppReady() {
  if (kiraAppReadySignaled) return;
  kiraAppReadySignaled = true;
  document.dispatchEvent(new Event("kira:app-ready"));
}

function routeKey() {
  // A route rendered under another server-owned workspace/session is a different surface even if
  // its hash is unchanged. Same-workspace reconnects keep this identity and preserve live drafts.
  return JSON.stringify([
    workspaceId,
    state.context?.session_id ?? null,
    state.context?.project_id ?? null,
    state.context?.context_revision ?? null,
    state.route,
    ...(state.routeArgs || []),
  ]);
}

function routeLabel(name = state.route) {
  return Object.hasOwn(ROUTE_LABELS, name) ? ROUTE_LABELS[name] : cap(name);
}

function replaceRouteContainer(container) {
  const replacement = container.cloneNode(false);
  replacement.className = "screen";
  container.replaceWith(replacement);
  return replacement;
}

function scopedRouteApi(container, generation, context, signal) {
  const scoped = Object.create(api);
  const isCurrent = () => generation === routeRenderGeneration && container.isConnected;
  scoped.renderIsCurrent = isCurrent;
  // A write that began on this still-live route may finish after a passive same-key refresh.
  // Let it schedule a newer router-owned read; navigation/authority changes replace this node.
  scoped.refreshRoute = () => (
    container.isConnected && document.getElementById("screen") === container
      ? renderRoute()
      : Promise.resolve()
  );
  scoped.retryRoute = () => (isCurrent() ? renderRoute({ reset: true }) : Promise.resolve());
  // Only the reads that belong to the returned initial-render Promise are strict. Once that
  // Promise settles, controls may keep this facade for normal long-lived clicks without becoming
  // invalid merely because a passive same-screen refresh ran later.
  const read = async (path, options = {}, required = false) => {
    const diagnostic = context.initial;
    const outcome = await api.get(path, diagnostic
      ? { ...options, signal, diagnostic: true }
      : options);
    const wrapped = outcome && outcome[API_READ_OUTCOME] === true;
    if (context.initial && !isCurrent()) throw ROUTE_RENDER_STALE;
    if (required && context.initial && wrapped && outcome.failure) throw outcome.failure;
    return wrapped ? outcome.data : outcome;
  };
  scoped.get = (path, options = {}) => read(path, options);
  // Required is intentionally opt-in. Composite screens such as Notifications keep their useful
  // sections visible when one independent read fails; a primary screen read instead reaches the
  // shell retry boundary on transport or malformed-JSON failure. HTTP feature-off responses stay
  // null so the screen can retain its precise unavailable copy.
  scoped.getRequired = (path, options = {}) => read(path, options, true);
  return scoped;
}

// Bound the whole initial screen transaction, including dependencies such as voiceStatus() and
// runnerStatus() that do not use scoped get(). One timer owns the render; post-mount callbacks are
// outside this promise and retain their existing unbounded/action-specific behavior.
function boundedInitialRouteRender(result, controller) {
  const configuredTimeout = Number(globalThis.__KAIRO_INITIAL_ROUTE_READ_TIMEOUT_MS__);
  const timeoutMs = Number.isFinite(configuredTimeout) && configuredTimeout > 0
    ? configuredTimeout : INITIAL_ROUTE_READ_TIMEOUT_MS;
  return new Promise((resolve, reject) => {
    let timeoutFailure = null;
    const cleanup = () => {
      clearTimeout(timer);
      controller.signal.removeEventListener("abort", onAbort);
    };
    const onAbort = () => {
      cleanup();
      reject(timeoutFailure || ROUTE_RENDER_STALE);
    };
    const timer = setTimeout(() => {
      timeoutFailure = new Error("initial route render timed out");
      timeoutFailure.routeRenderTimeout = true;
      controller.abort();
    }, timeoutMs);
    controller.signal.addEventListener("abort", onAbort, { once: true });
    if (controller.signal.aborted) {
      onAbort();
      return;
    }
    Promise.resolve(result).then(
      (value) => { cleanup(); resolve(value); },
      (error) => { cleanup(); reject(error); },
    );
  });
}

function renderRouteLoading(container) {
  container.textContent = "";
  container.setAttribute("aria-busy", "true");
  const status = document.createElement("section");
  status.className = "route-state route-loading";
  status.setAttribute("role", "status");
  status.setAttribute("aria-live", "polite");
  status.setAttribute("aria-atomic", "true");
  const pulse = document.createElement("span");
  pulse.className = "route-state-pulse";
  pulse.setAttribute("aria-hidden", "true");
  const copy = document.createElement("div");
  const heading = document.createElement("h1");
  heading.textContent = `Opening ${routeLabel()}`;
  const detail = document.createElement("p");
  detail.textContent = "Loading the latest workspace state…";
  copy.append(heading, detail);
  status.append(pulse, copy);
  container.appendChild(status);
  return status;
}

function renderRouteFailure(container, error) {
  container.textContent = "";
  container.setAttribute("aria-busy", "false");
  const failure = document.createElement("section");
  failure.className = "route-state route-failure";
  failure.setAttribute("role", "alert");
  const heading = document.createElement("h1");
  heading.tabIndex = -1;
  heading.textContent = `${routeLabel()} couldn't open`;
  const detail = document.createElement("p");
  detail.textContent = "Kairo couldn't load this screen. Check the connection and try again.";
  const actions = document.createElement("div");
  actions.className = "route-state-actions";
  const retry = document.createElement("button");
  retry.type = "button";
  retry.className = "plain-button primary";
  retry.textContent = "Try again";
  retry.addEventListener("click", () => {
    if (container.isConnected) void renderRoute({ reset: true });
  });
  const chat = document.createElement("a");
  chat.className = "plain-button ghost";
  chat.href = "#chat";
  chat.textContent = "Open Chat";
  actions.append(retry, chat);
  failure.append(heading, detail, actions);
  container.appendChild(failure);
  // Keep provider/data detail out of the UI, while retaining the local exception for developers.
  console.error(`Kairo failed to render ${state.route}`, error);
  requestAnimationFrame(() => { if (heading.isConnected) heading.focus(); });
}

function refreshIfActive(name) { if (state.route === name) renderRoute(); }
function vaultHasDraft() {
  const input = document.getElementById("vault-ingest-input");
  return Boolean(input && (input === document.activeElement || input.value.trim() !== ""));
}
function refreshWorkspaceTabs(...tabs) {
  if (state.route === "workspace" && tabs.includes(state.routeArgs[1] || "overview")) {
    renderRoute();
  }
}
function refreshConversation() { refreshIfActive("chat"); refreshIfActive("daily"); }

function rerenderAfterContextChange({ projectChanged = false } = {}) {
  if (projectChanged && state.route === "workspace") {
    const nextProjectId = Number(state.context?.project_id);
    const viewedProjectId = Number(state.routeArgs?.[0]);
    if (!Number.isInteger(nextProjectId) || nextProjectId < 1
        || viewedProjectId !== nextProjectId) {
      const nextHash = Number.isInteger(nextProjectId) && nextProjectId > 0
        ? `#workspace/${nextProjectId}`
        : "#projects";
      // The old workspace is no longer the live authority. Replace this history entry so Back
      // cannot reopen an archived/foreign project, then synchronously remove its controls.
      history.replaceState(null, "", nextHash);
      navigate({ preserveIntent: true });
      return;
    }
  }
  renderRoute();
}

function renderRoute({ reset = false } = {}) {
  const nextRouteKey = routeKey();
  const routeChanged = nextRouteKey !== activeRouteKey;
  if (activeRouteController) activeRouteController.abort();
  activeRouteController = new AbortController();
  const controller = activeRouteController;
  let shellLoading = null;
  let container = document.getElementById("screen");
  if (routeChanged || reset) {
    activeRouteKey = nextRouteKey;
    container = replaceRouteContainer(container);
    shellLoading = renderRouteLoading(container);
  } else {
    container.className = "screen";
    container.setAttribute("aria-busy", "true");
  }
  const generation = ++routeRenderGeneration;
  const renderContext = { initial: true };
  const screenApi = scopedRouteApi(container, generation, renderContext, controller.signal);
  const routeName = state.route;
  const routeArgs = [...(state.routeArgs || [])];
  document.body.dataset.route = state.route;
  // The hash is available before the server-owned workspace handshake. Keep every interactive
  // screen fail-closed until that authority exists; otherwise a user can type into a composer
  // that must be destroyed as soon as the first workspace frame arrives.
  if (!state.context) {
    if (!shellLoading?.isConnected) renderRouteLoading(container);
    if (activeRouteController === controller) activeRouteController = null;
    return Promise.resolve();
  }
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
    container.setAttribute("aria-busy", "false");
    if (activeRouteController === controller) activeRouteController = null;
    signalKiraAppReady();
    return Promise.resolve();
  }
  // Own-property lookup only: state.route is hash-derived, so "#__proto__" etc. must fall
  // through to the safe unknown-route branch, never resolve an inherited Object member.
  const fn = Object.hasOwn(screens, state.route) ? screens[routeName] : null;
  if (fn) {
    let result;
    try {
      result = fn(container, screenApi, routeArgs);
    } catch (error) {
      renderContext.initial = false;
      if (activeRouteController === controller) activeRouteController = null;
      if (generation === routeRenderGeneration && container.isConnected) {
        renderRouteFailure(container, error);
        signalKiraAppReady();
      }
      return Promise.resolve();
    }
    if (!result || typeof result.then !== "function") {
      renderContext.initial = false;
      // Sync screens may launch their own guarded background reads (Chat header, Daily cards,
      // Settings status). They are not owned by the returned route render, so do not abort them
      // when a passive same-screen refresh creates a newer generation.
      if (activeRouteController === controller) activeRouteController = null;
      if (generation === routeRenderGeneration && container.isConnected) {
        container.setAttribute("aria-busy", "false");
        signalKiraAppReady();
      }
      return Promise.resolve();
    }
    // Some async screens clear before their first await. Restore the shell-level loading state
    // for that gap; screens with a richer local skeleton keep it.
    if (!container.hasChildNodes()) shellLoading = renderRouteLoading(container);
    const job = boundedInitialRouteRender(result, controller).then(
      () => {
        renderContext.initial = false;
        if (activeRouteController === controller) activeRouteController = null;
        if (generation === routeRenderGeneration && container.isConnected) {
          if (shellLoading?.isConnected) shellLoading.remove();
          container.setAttribute("aria-busy", "false");
          signalKiraAppReady();
        }
      },
      (error) => {
        const timedOut = error?.routeRenderTimeout === true;
        // A superseded or timed-out underlying renderer may still settle if a dependency ignored
        // AbortSignal. Keep its scoped facade strict, and disconnect its container on timeout, so
        // that orphan can never paint over the retry surface.
        if (error !== ROUTE_RENDER_STALE && !timedOut) renderContext.initial = false;
        if (activeRouteController === controller) activeRouteController = null;
        if (error === ROUTE_RENDER_STALE) return;
        if (generation === routeRenderGeneration && container.isConnected) {
          if (timedOut) container = replaceRouteContainer(container);
          renderRouteFailure(container, error);
          signalKiraAppReady();
        }
      },
    );
    return job;
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
  container.setAttribute("aria-busy", "false");
  if (activeRouteController === controller) activeRouteController = null;
  signalKiraAppReady();
  return Promise.resolve();
}

// Parse the hash into a screen name + positional args: "#workspace/12" -> {name:"workspace",
// args:["12"]}. Args let a screen deep-link (the project Workspace, T10) with no new route.
function parseHash() {
  const parts = location.hash.replace(/^#/, "").split("/").filter((s) => s !== "");
  return { name: parts[0] || "chat", args: parts.slice(1) };
}

function navigate({ preserveIntent = false } = {}) {
  // A service policy editor belongs to the route on which it was opened. Browser Back/Forward
  // and direct hash changes do not pass through the modal's controls, so close it here as the
  // navigation boundary. Force also aborts an in-flight fetch; the dialog owner is retired before
  // aborting, which keeps a late server response from refreshing or announcing into the new route.
  dismissProjectServiceAccess({ force: true });
  if (!preserveIntent) navigationGeneration += 1;
  const { name, args } = parseHash();
  if (state.route && state.route !== name) setSurface(state.route, false);
  state.route = name;
  state.routeArgs = args;
  clearScope();                                      // a screen's local keys never leak forward
  setSurface(name, true);
  if (name === "gate") setSurface("gate", true);
  document.querySelector(".mobile-more")?.removeAttribute("open");
  for (const a of document.querySelectorAll(".rail a")) {
    const active = a.dataset.screen === name;
    a.classList.toggle("active", active);
    if (active) a.setAttribute("aria-current", "page");
    else a.removeAttribute("aria-current");
  }
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
  const facts = runnerFacts(s);
  const operation = runnerControlOperation;
  const busy = facts.workActive || !!operation;
  const setText = (id, t) => { const el = document.getElementById(id); if (el) el.textContent = t; };
  const setClass = (id, c) => { const el = document.getElementById(id); if (el) el.className = c; };
  const dotClass = "runner-dot" + (busy ? " busy" : "");
  let runnerText = "Kairo is idle";
  if (operation?.action === "pause") runnerText = "Stopping all chats and pausing schedules";
  else if (operation?.action === "resume") runnerText = "Resuming schedules";
  else if (!facts.statusCurrent) runnerText = "Runner status is unavailable";
  else if (facts.currentTurnBusy) runnerText = "Kairo is working in this chat";
  else if (facts.globalTurnBusy) runnerText = "Kairo is working in another chat";
  else if (facts.backgroundBusy) runnerText = "Scheduled work is running";
  else if (facts.paused) runnerText = "Schedules are paused";
  else if (s.runner_available === false) runnerText = "Schedules are unavailable";
  // status bar
  setText("st-runner", runnerText);
  setClass("runner-dot", dotClass);
  setText("st-turn", operation ? operation.action : (facts.workActive ? "working" : (facts.paused ? "paused" : "ready")));
  syncRunnerControlCopies(facts);
  // Phase 10 status strip: active project, run mode, today's spend, cost-ledger health.
  setText("st-project", s.project && s.project.name ? s.project.name : "global");
  setText("st-mode", s.mode || "approval");
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
    let lead = "Kairo is idle";
    let desc = "Your briefing is up to date.";
    if (operation?.action === "pause") {
      lead = "Stopping all chats";
      desc = "Scheduled work is being paused safely.";
    } else if (operation?.action === "resume") {
      lead = "Resuming schedules";
      desc = "Stopped chats stay stopped.";
    } else if (!facts.statusCurrent) {
      lead = "Runner status is unavailable";
      desc = facts.workActive
        ? "Last known work may still be running. Stop all remains available."
        : "Kairo will retry automatically.";
    } else if (facts.currentTurnBusy) {
      lead = "Kairo is working";
      desc = "Progress is available in Chat.";
    } else if (facts.globalTurnBusy) {
      lead = "Kairo is working in another chat";
      desc = "That chat's progress is available in Chat.";
    } else if (facts.backgroundBusy) {
      lead = "Scheduled work is running";
      desc = "Background work continues independently of this chat.";
    } else if (facts.paused) {
      lead = "Schedules are paused";
      desc = "Stopped chats stay stopped. Resume schedules when you're ready.";
    } else if (s.runner_available === false) {
      lead = "Schedules are unavailable";
      desc = "Global runner controls are unavailable.";
    }
    setClass("daily-now-dot", dotClass);
    setText("daily-now-lead", lead);
    setText("daily-now-desc", desc);
    setText("daily-cost-today", typeof s.today_spend_usd === "number"
      ? `${money(s.today_spend_usd)} today` : "Cost unavailable");
  }
  syncStatusChrome(s);
}

// Fix the "No messages yet" reload: the server keeps the active conversation alive across a
// browser reload, so pull the transcript for the exact live authority back into the view. A late
// response from a replaced workspace/session is ignored and a failed read remains retryable.
let rehydratedConversationKey = null;
let conversationHydrationRevision = 0;

function conversationHydrationKey() {
  const sid = state.context?.session_id ?? state.runner?.session_id;
  if (sid == null) return null;
  return JSON.stringify([
    workspaceId, sid, state.context?.project_id ?? null,
    state.context?.context_revision ?? null,
  ]);
}

function invalidateConversationHydration() {
  conversationHydrationRevision += 1;
  rehydratedConversationKey = null;
}

async function rehydrateConversation() {
  const key = conversationHydrationKey();
  const sid = state.context?.session_id ?? state.runner?.session_id;
  if (!key || rehydratedConversationKey === key || sid == null) return;
  rehydratedConversationKey = key;
  if (state.chat.length) return;
  const emptyChat = state.chat;
  const revision = ++conversationHydrationRevision;
  const t = await api.get(`/api/sessions/${sid}`);
  if (revision !== conversationHydrationRevision || key !== conversationHydrationKey()) return;
  if (state.chat !== emptyChat || state.chat.length) return;
  if (t && Array.isArray(t.messages)) {
    state.chat = hydrateTranscript(t);
    refreshConversation();
  } else {
    rehydratedConversationKey = null;
  }
}

async function refreshVoiceStatus() {
  // Every consumer goes through this sequencer. A later-started screen read wins over an older
  // shell poll, while monotonic server revisions reject delayed WebSocket/HTTP observations.
  const statusSequence = ++voiceStatusSequence;
  const eventGeneration = meetingEventGeneration;
  const recordingEventGeneration = meetingRecordingEventGeneration;
  const voiceStatus = await api.get("/api/voice/status");
  if (statusSequence !== voiceStatusSequence) return lastPublishedVoiceStatus;
  const v = voiceStatus || { enabled: false, reason: "" };
  const previousVoice = state.voice;
  const currentVoiceState = state.voice.listening || "idle";
  const statusMeetingRevision = wireRevision(v.meeting_revision);
  const meetingStatusIsFresh = voiceStatus
    && eventGeneration === meetingEventGeneration
    && (statusMeetingRevision === null || statusMeetingRevision >= meetingStateRevision);
  const meetingState = meetingStatusIsFresh
    ? (v.meeting || "idle")
    : (previousVoice.meeting || "idle");
  if (meetingStatusIsFresh && statusMeetingRevision !== null) {
    meetingStateRevision = statusMeetingRevision;
  }
  const statusRecordingEpoch = wireEpoch(v.meeting_recording_epoch);
  if (statusRecordingEpoch && statusRecordingEpoch !== meetingRecordingEpoch) {
    meetingRecordingEpoch = statusRecordingEpoch;
    meetingRecordingRevision = -1;
  }
  const statusRecordingRevision = wireRevision(v.meeting_recording_revision);
  const meetingRecordingStatusIsFresh = voiceStatus
    && recordingEventGeneration === meetingRecordingEventGeneration
    && (statusRecordingRevision === null
      || statusRecordingRevision >= meetingRecordingRevision);
  const meetingRecording = meetingRecordingStatusIsFresh
    ? Boolean(v.meeting_recording)
    : Boolean(previousVoice.meeting_recording);
  if (meetingRecordingStatusIsFresh && statusRecordingRevision !== null) {
    meetingRecordingRevision = statusRecordingRevision;
  }
  const unavailableReason = !v.enabled ? (v.reason || "Voice is unavailable.")
    : (!canCapture() ? "This browser can't record audio." : "");
  state.voice = {
    ...state.voice, ...v,
    listening: (!v.enabled || currentVoiceState === "idle" || currentVoiceState === "error")
      ? (v.listening || "idle") : currentVoiceState,
    reason: unavailableReason || (currentVoiceState === "error" ? state.voice.reason : ""),
    browserCapture: canCapture(),
    meeting: meetingState,
    meeting_revision: meetingStateRevision,
    meeting_recording: meetingRecording,
    meeting_recording_epoch: meetingRecordingEpoch,
    meeting_recording_revision: meetingRecordingRevision,
  };
  showMeetingRecording(meetingRecording);
  // Consumers need provider/capability fields too, but must never receive raw local/global
  // meeting fields that were just rejected as stale against newer lifecycle observations.
  const publishedVoiceStatus = voiceStatus
    ? {
      ...voiceStatus,
      meeting: meetingState,
      meeting_revision: meetingStateRevision,
      meeting_recording: meetingRecording,
      meeting_recording_epoch: meetingRecordingEpoch,
      meeting_recording_revision: meetingRecordingRevision,
    }
    : null;
  lastPublishedVoiceStatus = publishedVoiceStatus;
  busEmit("voice_status", { status: publishedVoiceStatus });
  if (meetingStatusIsFresh && previousVoice.meeting !== meetingState) {
    busEmit("meeting_state", { state: meetingState, revision: meetingStateRevision });
  }
  const voiceEl = document.getElementById("st-voice");
  if (voiceEl) {
    voiceEl.textContent = v.enabled ? (v.listening || "ready") : "off";
    voiceEl.title = v.enabled ? "" : (v.reason || "");
    if (v.enabled && !canCapture()) voiceEl.title = "This browser can't record audio.";
  }
  const mic = document.getElementById("st-mic");
  if (mic) {
    mic.classList.toggle("is-hidden", !(v.enabled && canCapture()));
    if (mic.dataset.busy !== "1" && !recording()) mic.textContent = "🎤 Talk";
  }
  const play = document.getElementById("st-play");
  if (play) {
    play.classList.toggle("is-hidden", !(v.enabled && v.playback));
    play.classList.toggle("active", playbackOn());
    play.title = playbackOn() ? "Spoken replies: on" : "Spoken replies: off";
  }
  if (previousVoice.enabled !== state.voice.enabled
      || previousVoice.reason !== state.voice.reason
      || previousVoice.browserCapture !== state.voice.browserCapture) refreshConversation();
  return publishedVoiceStatus;
}

async function refreshRunnerStatus({
  refreshChatHeader = false, timeoutMs = null, adoptSuperseding = true,
} = {}) {
  const runnerWasUnavailable = state.runnerStatusError;
  const s = await api.runnerStatus({ refresh: true, timeoutMs, adoptSuperseding });
  if (s) {
    state.runner = s;
    renderRunnerState(); rehydrateConversation();
    // Re-enable the header immediately after an outage; otherwise its deliberately disabled
    // controls would remain frozen until an unrelated event happens to refresh it.
    if (runnerWasUnavailable || refreshChatHeader) refreshHeader();
  } else {
    // Keep last-known status-bar data, but stop presenting it as writable header state.
    renderRunnerState();
    refreshHeader();
  }
  return s;
}

async function pollStatus({ refreshChatHeader = false } = {}) {
  await refreshRunnerStatus({ refreshChatHeader });
  await refreshVoiceStatus();
}

// --- wire up ---
function init() {
  document.getElementById("ap-approve").addEventListener("click", () => resolveApproval("approve"));
  document.getElementById("ap-always").addEventListener("click", () => resolveApproval("always"));
  document.getElementById("ap-deny").addEventListener("click", () => resolveApproval("deny"));
  document.getElementById("ap-retry").addEventListener("click", () => {
    const did = document.getElementById("overlay").dataset.decision;
    const pending = state.pending.get(did);
    if (pending && !_approvalResolving.has(did) && !_approvalRecovering.has(did)) {
      void recoverApproval(
        pending,
        pending.error || "A secure confirmation was not received.",
      );
    }
  });
  document.querySelectorAll("[data-runner-control]").forEach((control) => {
    control.addEventListener("click", () => { void runRunnerControl(control.dataset.runnerControl); });
  });
  document.getElementById("st-logout").addEventListener("click", async (event) => {
    event.currentTarget.disabled = true;
    await api.post("/auth/logout", {});
    location.replace("/login");
  });
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
  window.addEventListener("hashchange", () => navigate());
  connect();
  navigate();
  pollStatus();
  setInterval(pollStatus, 4000);
}

init();
