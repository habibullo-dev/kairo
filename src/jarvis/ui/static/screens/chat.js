// Chat — Kairo's primary talking surface. It reuses the existing session/model/mode controls and
// the sole attended turn route; no new route, authority, or event stream is introduced here.
import { mountHeader } from "../ui/header.js";
import { renderConversation, submitConversationTurn } from "./conversation.js";

export function render(container, api) {
  // app.js clears per-route classes before each render; retain the full-height Chat shell during
  // streamed events and status refreshes, not just on its first construction.
  container.classList.add("chat-screen");
  if (!container.querySelector("#chat-input")) {
    container.innerHTML = `
      <section class="chat-shell">
        <div class="chat-intro">
          <div><div class="chat-kicker">Kairo</div><h1>Chat</h1></div>
          <div class="chat-turn-meta" id="chat-turn-meta"></div>
        </div>
        <div id="chat-convo-header"></div>
        <div id="chat-pending"></div>
        <div class="chat-thread" id="chat-thread"></div>
        <form class="chat-composer" id="chat-composer">
          <textarea id="chat-input" rows="1" placeholder="Message Kairo…" autocomplete="off" aria-label="Message Kairo"></textarea>
          <div class="chat-composer-foot">
            <div class="chat-composer-tools">
              <div class="voice-mode" aria-label="Voice mode">
                <button type="button" data-voice-mode="dictation">Dictate</button>
                <button type="button" data-voice-mode="conversation">Conversation</button>
              </div>
              <button class="chat-mic" id="chat-mic" type="button" aria-label="Start voice capture">🎙</button>
              <button class="chat-voice-cancel is-hidden" id="chat-voice-cancel" type="button">Cancel</button>
              <div class="live-chips"><span id="chat-model"></span><span id="chat-mode"></span></div>
            </div>
            <div class="chat-composer-actions"><span class="chat-voice-status" id="chat-voice-status"></span><button class="chat-send" type="submit">Send <span aria-hidden="true">↵</span></button></div>
          </div>
        </form>
      </section>`;
    const input = container.querySelector("#chat-input");
    const redraw = () => renderThread(container, api);
    const submit = async (event) => {
      event.preventDefault();
      await submitConversationTurn(api, input, redraw);
    };
    container.querySelector("#chat-composer").addEventListener("submit", submit);
    input.addEventListener("keydown", (event) => {
      if (event.key === "Enter" && !event.shiftKey) submit(event);
    });
    input.addEventListener("input", () => {
      input.style.height = "auto";
      input.style.height = `${Math.min(input.scrollHeight, 200)}px`;
    });
    for (const button of container.querySelectorAll("[data-voice-mode]")) {
      button.addEventListener("click", () => {
        api.state.voice.mode = button.dataset.voiceMode;
        renderVoiceControls(container, api);
      });
    }
    container.querySelector("#chat-mic").addEventListener("click", async () => {
      const mode = api.state.voice.mode || "dictation";
      await api.toggleVoiceCapture(mode, (transcript) => {
        input.value = input.value ? `${input.value} ${transcript}` : transcript;
        input.dispatchEvent(new Event("input"));
        input.focus();
      });
    });
    container.querySelector("#chat-voice-cancel").addEventListener("click", () => api.cancelVoiceCapture());
    mountHeader(container.querySelector("#chat-convo-header"), api, { onChanged: () => redraw() });
  }
  renderPending(container, api);
  renderThread(container, api);
  renderMeta(container, api);
  renderVoiceControls(container, api);
}

const VOICE_STATES = {
  idle: "Voice ready",
  listening: "Listening…",
  capturing: "Listening…",
  transcribing: "Transcribing…",
  thinking: "Thinking…",
  speaking: "Speaking safe reply…",
  error: "Voice unavailable",
};

function renderVoiceControls(container, api) {
  const voice = api.state.voice || {};
  const mode = voice.mode || "dictation";
  const state = voice.listening || "idle";
  const disabled = !voice.enabled || voice.browserCapture === false;
  const mic = container.querySelector("#chat-mic");
  const cancel = container.querySelector("#chat-voice-cancel");
  const status = container.querySelector("#chat-voice-status");
  if (!mic || !cancel || !status) return;
  for (const button of container.querySelectorAll("[data-voice-mode]")) {
    button.classList.toggle("active", button.dataset.voiceMode === mode);
    button.disabled = disabled || ["listening", "capturing", "transcribing", "thinking", "speaking"].includes(state);
  }
  mic.disabled = disabled || ["transcribing", "thinking", "speaking"].includes(state);
  mic.textContent = state === "listening" || state === "capturing" ? "■ Stop" : "🎙";
  mic.title = disabled ? (voice.reason || "Voice is unavailable.")
    : (state === "listening" || state === "capturing" ? "Stop and continue" : `Start ${mode}`);
  const cancellable = ["listening", "capturing", "speaking"].includes(state);
  cancel.classList.toggle("is-hidden", !cancellable);
  status.textContent = disabled ? `Voice unavailable: ${voice.reason || "not enabled"}`
    : (state === "error" ? `Voice error: ${voice.reason || "try again"}` : (VOICE_STATES[state] || "Voice ready"));
  status.classList.toggle("error", disabled || state === "error");
}

function renderPending(container, api) {
  const host = container.querySelector("#chat-pending");
  if (!host) return;
  const pending = [...api.state.pending.values()];
  host.textContent = "";
  if (!pending.length) return;
  const button = document.createElement("button");
  button.className = "chat-approval";
  button.type = "button";
  button.textContent = `${pending.length} approval${pending.length === 1 ? "" : "s"} waiting`;
  button.addEventListener("click", () => api.reviewPending());
  host.appendChild(button);
}

function renderThread(container, api) {
  renderConversation(container.querySelector("#chat-thread"), api.state, {
    emptyHeading: "What can I help with?",
    emptyHint: "Ask a question, plan work, or begin with the project you have open.",
  });
}

function renderMeta(container, api) {
  const meta = container.querySelector("#chat-turn-meta");
  if (!meta) return;
  const runner = api.state.runner || {};
  const project = runner.project && runner.project.name ? runner.project.name : "Global";
  const title = runner.session_title || "New chat";
  const spend = typeof runner.today_spend_usd === "number" ? `$${runner.today_spend_usd.toFixed(4)} today` : "";
  meta.textContent = [project, title, spend].filter(Boolean).join(" · ");
}
