// Meetings currently captures one endpointed note from the workstation microphone: at most 30
// seconds and ending after silence.  The UI names that exact boundary; a browser-controlled,
// long-running meeting recorder is a separate feature and must not be implied here.
import { on as busOn } from "../ui/bus.js";
import { readStored, removeStoredIfValue, writeStored } from "../ui/storage.js";

let activeContainer = null;
let activeApi = null;
let activeReady = false;
let inFlight = false;
let inFlightAuthorityToken = null;
let serverPhase = "idle";
let lifecycleRevision = 0;
let voiceStatusRevision = 0;
let renderGeneration = 0;
let lastVoiceStatus = null;
let operationOutcome = null;
const memoryReceipts = new Map();

const CAPTURE_ID = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;

function captureStorageKeys(api) {
  const context = api.state?.context;
  if (!Number.isInteger(context?.session_id)) return null;
  const scope = context.project_id == null ? "global" : `project-${context.project_id}`;
  return {
    key: `kira:meeting-capture:${scope}`,
    legacyKey: `kairo:meeting-capture:${scope}`,
  };
}

function captureReceiptFor(api) {
  const keys = captureStorageKeys(api);
  if (!keys) return null;
  const remembered = memoryReceipts.get(keys.key);
  if (CAPTURE_ID.test(remembered || "")) return { ...keys, id: remembered };
  const canonical = readStored("session", keys.key);
  const legacy = readStored("session", keys.legacyKey);
  let id = CAPTURE_ID.test(canonical || "") ? canonical : legacy;
  if (!CAPTURE_ID.test(id || "")) id = crypto.randomUUID();
  memoryReceipts.set(keys.key, id);
  // A meeting receipt is recovery state, not a preference. Dual-write it during the compatibility
  // window so either a current reload or a rollback to a legacy page retries the exact same UUID.
  writeStored("session", keys.key, id);
  writeStored("session", keys.legacyKey, id);
  return { ...keys, id };
}

function clearCaptureReceipt(receipt) {
  if (!receipt) return;
  if (memoryReceipts.get(receipt.key) === receipt.id) memoryReceipts.delete(receipt.key);
  removeStoredIfValue("session", [receipt.key, receipt.legacyKey], receipt.id);
}

const BUSY_PHASES = new Set(["requesting", "recording", "transcribing", "saving", "finalizing"]);
const PHASE_COPY = {
  idle: ["Ready for a short spoken note", "Capture spoken note"],
  requesting: ["Checking the capture receipt and preparing audio…", "Preparing…"],
  recording: ["Listening through the workstation microphone…", "Listening…"],
  transcribing: ["Transcribing the captured note…", "Transcribing…"],
  saving: ["Saving the transcript for review…", "Saving…"],
  finalizing: ["Finalizing the capture result…", "Finalizing…"],
  unknown: ["The last capture result is unconfirmed", "Check result or retry"],
};

function setPhase(container, phase) {
  const normalized = Object.hasOwn(PHASE_COPY, phase) ? phase : "idle";
  const displayPhase = inFlight && normalized === "idle" ? "finalizing" : normalized;
  const busy = BUSY_PHASES.has(displayPhase) || inFlight;
  container.dataset.meetingPhase = displayPhase;
  const controls = container.querySelector("#mtg-controls");
  const dot = container.querySelector("#mtg-state-dot");
  const lead = container.querySelector("#mtg-state");
  const button = container.querySelector("#mtg-start");
  const consent = container.querySelector("#mtg-consent");
  const title = container.querySelector("#mtg-title");
  // Keep both live regions outside this busy subtree; otherwise assistive technology may defer
  // recording/transcribing announcements until the operation has already completed.
  if (controls) controls.setAttribute("aria-busy", busy ? "true" : "false");
  if (dot) {
    dot.classList.toggle("meeting-recording", displayPhase === "recording");
    dot.classList.toggle(
      "busy",
      BUSY_PHASES.has(displayPhase) && displayPhase !== "recording",
    );
  }
  if (lead) {
    lead.textContent = PHASE_COPY[displayPhase][0];
    lead.classList.toggle("idle", displayPhase === "idle");
  }
  if (button) {
    button.textContent = PHASE_COPY[displayPhase][1];
    button.disabled = busy || button.dataset.available !== "1" || !consent?.checked;
  }
  if (consent) consent.disabled = busy;
  if (title) title.disabled = busy;
}

function showMessage(container, message, { openKnowledge = false } = {}) {
  const output = container.querySelector("#mtg-out");
  if (!output) return;
  output.replaceChildren(document.createTextNode(message));
  if (openKnowledge) {
    output.appendChild(document.createTextNode(" "));
    const link = document.createElement("a");
    link.href = "#vault";
    link.textContent = "Open Knowledge →";
    output.appendChild(link);
  }
}

function authorityIsCurrent(api, token) {
  return token === null || typeof api.authorityIsCurrent !== "function"
    || api.authorityIsCurrent(token);
}

// The route can remount while capture is pending. Resolve only into the currently connected
// Meetings container under the exact authority that started the request; the captured container
// is merely an obsolete DOM snapshot after a remount.
function liveOperationContainer(api, authorityToken) {
  if (!authorityIsCurrent(api, authorityToken) || activeApi?.state?.route !== "meetings"
      || !activeContainer?.isConnected) return null;
  const activeToken = typeof activeApi.authorityToken === "function"
    ? activeApi.authorityToken() : null;
  if (authorityToken !== null && activeToken !== authorityToken) return null;
  return activeContainer;
}

function rememberOperationOutcome(api, authorityToken, message, options = {}) {
  if (!authorityIsCurrent(api, authorityToken)) return null;
  operationOutcome = {
    authorityToken,
    message,
    options: { openKnowledge: Boolean(options.openKnowledge) },
  };
  const liveContainer = liveOperationContainer(api, authorityToken);
  if (liveContainer) showMessage(liveContainer, message, options);
  return liveContainer;
}

function showRememberedOutcome(container, api, authorityToken) {
  if (!operationOutcome || operationOutcome.authorityToken !== authorityToken
      || !authorityIsCurrent(api, authorityToken)) return;
  showMessage(container, operationOutcome.message, operationOutcome.options);
}

function failureMessage(result) {
  if (result?.status === 409) return "Another voice action is active. Wait for it to finish, then retry.";
  if (result?.status === 422) return "No speech was detected, so nothing was saved. Try again closer to the microphone.";
  if (result?.status === 503) return "Meeting-note capture is unavailable. Check the voice setup and microphone.";
  return "The note could not be captured. Check the microphone and try again.";
}

// Voice lifecycle frames are content-free. Keep the open-microphone state visible on this screen
// without re-rendering the form (which would erase the consent/title the user just entered).
busOn("meeting_state", ({ state } = {}) => {
  lifecycleRevision += 1;
  serverPhase = Object.hasOwn(PHASE_COPY, state) ? state : "idle";
  if (activeContainer && activeApi?.state?.route === "meetings") {
    setPhase(activeContainer, serverPhase);
  }
});

busOn("voice_status", ({ status } = {}) => {
  voiceStatusRevision += 1;
  lastVoiceStatus = status || null;
  if (activeReady && activeContainer && activeApi?.state?.route === "meetings") {
    applyVoiceStatus(activeContainer, lastVoiceStatus, activeApi);
  }
});

function applyVoiceStatus(container, status, api) {
  const contextReady = Number.isInteger(api.state?.context?.session_id);
  const available = Boolean(status && contextReady && status.meeting_available);
  const availability = container.querySelector("#mtg-availability");
  if (availability) {
    availability.textContent = !contextReady
      ? "Waiting for the authenticated workspace context before capture can start."
      : (!status
        ? "Voice provider status is temporarily unavailable; capture is disabled."
        : (available
          ? "The microphone opens only after consent and closes automatically after the note."
          : (status.meeting_reason || "Meeting-note capture is unavailable.")));
  }
  const privacy = container.querySelector("#mtg-privacy");
  if (privacy) {
    privacy.textContent = !status
      ? "Provider status is unavailable. Kira will not open the microphone until it recovers."
      : (status.stt === "openai"
        ? "Audio is sent to the configured OpenAI transcription service. Transcript text may also be sent to configured Knowledge providers for indexing; raw audio is not retained by Kira."
        : "Speech-to-text runs on this workstation. Transcript text may be sent to configured Knowledge providers for indexing; raw audio is not retained by Kira.");
  }
  const button = container.querySelector("#mtg-start");
  if (button) button.dataset.available = available ? "1" : "0";
  setPhase(container, serverPhase);
}

export async function render(container, api) {
  const renderAuthorityToken = typeof api.authorityToken === "function"
    ? api.authorityToken() : null;
  if (inFlight && inFlightAuthorityToken !== null
      && typeof api.authorityIsCurrent === "function"
      && !api.authorityIsCurrent(inFlightAuthorityToken)) {
    inFlight = false;
    inFlightAuthorityToken = null;
  }
  if (operationOutcome && (operationOutcome.authorityToken !== renderAuthorityToken
      || !authorityIsCurrent(api, operationOutcome.authorityToken))) {
    operationOutcome = null;
  }
  const generation = ++renderGeneration;
  activeContainer = container;
  activeApi = api;
  activeReady = false;
  const revisionBeforeRead = lifecycleRevision;
  const statusRevisionBeforeRead = voiceStatusRevision;
  const cachedPhase = api.state?.voice?.meeting;
  if (Object.hasOwn(PHASE_COPY, cachedPhase)) serverPhase = cachedPhase;
  // Replace the prior route synchronously. The shell stays fail-closed while the shared,
  // sequenced voice-status reader resolves; an old Chat form is never left actionable here.
  container.innerHTML = `
    <div class="rise"><h1>Meetings</h1>
      <div class="sub">Capture one short spoken note as meeting reference. The workstation microphone stops after silence or the 30-second limit.</div></div>
    <div class="card rise meeting-capture-card" id="mtg-capture-card">
      <div class="zone-now">
        <span class="runner-dot" id="mtg-state-dot" aria-hidden="true"></span>
        <div class="body">
          <div class="lead idle" id="mtg-state">Ready for a short spoken note</div>
          <div class="desc" id="mtg-availability"></div>
        </div>
      </div>
      <div class="meeting-form" id="mtg-controls" aria-busy="false">
        <label class="field-label" for="mtg-title">Note title</label>
        <input class="text-input" id="mtg-title" maxlength="120" value="Meeting note" autocomplete="off">
        <label class="meeting-consent" for="mtg-consent">
          <input id="mtg-consent" type="checkbox">
          <span>I confirm everyone present consents to this short audio capture.</span>
        </label>
        <div class="dim meeting-privacy" id="mtg-privacy"></div>
        <button class="btn btn-amber" id="mtg-start" type="button" disabled>Capture spoken note</button>
      </div>
      <div class="dim meeting-output" id="mtg-out" role="status" aria-live="polite" aria-atomic="true"></div>
    </div>
    <div class="card rise"><div class="card-label">Where this note goes</div>
      <div class="dim meeting-history-note">Only the transcript is kept. It appears in the <a href="#vault">Knowledge</a> review queue as an unreviewed source and is never acted on automatically.</div></div>`;
  showRememberedOutcome(container, api, renderAuthorityToken);

  const loadingAvailability = container.querySelector("#mtg-availability");
  if (loadingAvailability) {
    loadingAvailability.textContent = "Checking voice provider and microphone availability…";
  }
  const loadingPrivacy = container.querySelector("#mtg-privacy");
  if (loadingPrivacy) {
    loadingPrivacy.textContent = "Capture stays disabled until Kira confirms the provider.";
  }
  setPhase(container, serverPhase);

  const readStatus = await api.voiceStatus();
  // The router reuses one #screen node. A delayed status response from a Meetings render must
  // never mutate a newer route (or a newer Meetings render).
  if (generation !== renderGeneration || api.state?.route !== "meetings") return;
  activeReady = true;
  const v = statusRevisionBeforeRead === voiceStatusRevision ? readStatus : lastVoiceStatus;
  if (statusRevisionBeforeRead === voiceStatusRevision) lastVoiceStatus = readStatus;
  if (revisionBeforeRead === lifecycleRevision) {
    const phase = v ? (v.meeting || "idle") : (api.state?.voice?.meeting || serverPhase);
    serverPhase = Object.hasOwn(PHASE_COPY, phase) ? phase : "idle";
  }

  const button = container.querySelector("#mtg-start");
  const consent = container.querySelector("#mtg-consent");
  applyVoiceStatus(container, v, api);
  consent?.addEventListener(
    "change",
    () => setPhase(container, container.dataset.meetingPhase || "idle"),
  );
  setPhase(container, serverPhase);

  if (!button) return;
  button.addEventListener("click", async () => {
    if (inFlight || button.dataset.available !== "1" || !consent?.checked) return;
    const operationAuthorityToken = typeof api.authorityToken === "function"
      ? api.authorityToken() : null;
    const expectedContext = api.state.context && {
      session_id: api.state.context.session_id,
      project_id: api.state.context.project_id,
      context_revision: api.state.context.context_revision,
    };
    const operationIsCurrent = () => operationAuthorityToken === null
      || typeof api.authorityIsCurrent !== "function"
      || api.authorityIsCurrent(operationAuthorityToken);
    inFlight = true;
    inFlightAuthorityToken = operationAuthorityToken;
    operationOutcome = null;
    setPhase(container, "requesting");
    showMessage(container, "Checking for a saved result first. Kira will show Listening only if the microphone opens.");
    const title = container.querySelector("#mtg-title")?.value.trim() || "Meeting note";
    const receipt = captureReceiptFor(api);
    if (!receipt) {
      if (inFlightAuthorityToken === operationAuthorityToken) {
        inFlight = false;
        inFlightAuthorityToken = null;
      }
      showMessage(container, "The workspace context is not ready. Wait for Kira to reconnect.");
      setPhase(container, serverPhase);
      return;
    }
    let responseStatus = null;
    try {
      const r = await api.post(
        "/api/voice/meeting",
        { title, consent: true, capture_id: receipt.id, expected_context: expectedContext },
      );
      responseStatus = r.status;
      const sourceId = Number(r?.data?.source_id);
      if (!r?.ok || !r.data.ok || !Number.isInteger(sourceId) || sourceId < 1) {
        rememberOperationOutcome(api, operationAuthorityToken, failureMessage(r));
        return;
      }
      clearCaptureReceipt(receipt);
      const liveContainer = liveOperationContainer(api, operationAuthorityToken);
      const liveConsent = liveContainer?.querySelector("#mtg-consent");
      if (liveConsent) liveConsent.checked = false;
      const sourceStatus = r.data.source_status || "live";
      if (sourceStatus !== "live") {
        rememberOperationOutcome(
          api,
          operationAuthorityToken,
          `This capture receipt already belongs to ${sourceStatus} source #${sourceId}. No new audio was recorded; consent again to start a new note.`,
        );
        return;
      }
      const indexNote = r.data.index_state === "pending"
        ? " Indexing is pending; it remains unreviewed. Retry approval after Knowledge providers recover."
        : "";
      const reviewStatus = r.data.review_status === "reviewed" ? "reviewed" : "unreviewed";
      rememberOperationOutcome(
        api,
        operationAuthorityToken,
        `Saved “${r.data.title || title}” as ${reviewStatus} source #${sourceId}.${indexNote}`,
        { openKnowledge: true },
      );
    } catch {
      if (operationIsCurrent() && serverPhase === "idle") serverPhase = "unknown";
      const liveContainer = rememberOperationOutcome(
        api,
        operationAuthorityToken,
        "The connection ended before Kira could confirm whether the note was saved. Check Knowledge before capturing again.",
      );
      const liveConsent = liveContainer?.querySelector("#mtg-consent");
      if (liveConsent) liveConsent.checked = false;
    } finally {
      if (inFlightAuthorityToken === operationAuthorityToken) {
        inFlight = false;
        inFlightAuthorityToken = null;
      }
      if (responseStatus !== null && responseStatus !== 409) {
        const liveConsent = liveOperationContainer(api, operationAuthorityToken)
          ?.querySelector("#mtg-consent");
        if (liveConsent) liveConsent.checked = false;
      }
      // The HTTP response has no lifecycle ordering relative to another tab. Production emits
      // an authoritative terminal ``idle`` frame before returning; keep any newer WS/status phase.
      if (operationIsCurrent()) {
        const liveContainer = liveOperationContainer(api, operationAuthorityToken);
        if (liveContainer) setPhase(liveContainer, serverPhase);
      }
    }
  });
}
