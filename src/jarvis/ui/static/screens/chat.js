// Chat — Kairo's primary talking surface. It reuses the existing session/model/mode controls and
// the sole attended turn route; no new route, authority, or event stream is introduced here.
import { mountHeader, refreshHeader } from "../ui/header.js";
import { confirmDialog, promptDialog, showToast } from "../ui/feedback.js";
import { renderConversation, submitConversationTurn } from "./conversation.js";
import { renderSourceTree } from "../ui/source-tree.js";

export function render(container, api) {
  // app.js clears per-route classes before each render; retain the full-height Chat shell during
  // streamed events and status refreshes, not just on its first construction.
  container.classList.add("chat-screen");
  if (!container.querySelector("#chat-input")) {
    container.innerHTML = `
      <section class="chat-shell">
        <div class="chat-lifecycle-status" id="chat-lifecycle-status"></div>
        <div id="chat-pending"></div>
        <div class="chat-thread" id="chat-thread"></div>
        <section class="project-import-progress is-hidden" id="project-import-progress" role="status" aria-live="polite" aria-atomic="true">
          <div class="project-import-orbit" aria-hidden="true"><i></i><i></i><i></i></div>
          <div class="project-import-copy">
            <strong id="project-import-title"></strong>
            <span id="project-import-detail"></span>
          </div>
        </section>
        <form class="chat-composer" id="chat-composer">
          <textarea id="chat-input" rows="1" placeholder="Message Kairo…" autocomplete="off" aria-label="Message Kairo"></textarea>
          <div class="chat-composer-toolbar">
            <div id="chat-convo-header"></div>
            <div class="chat-turn-meta" id="chat-turn-meta"></div>
          </div>
          <div class="chat-composer-foot">
            <div class="chat-composer-tools">
              <button class="chat-attach" id="chat-attach" type="button" aria-label="Add project folder" title="Add project folder">＋</button>
              <input class="chat-file-input" id="chat-file-input" type="file" multiple accept=".pdf,.docx,.pptx,.xlsx,.epub,.md,.markdown,.txt,.text,.csv,.json,.jsonl,.py,.pyi,.js,.mjs,.cjs,.jsx,.ts,.tsx,.yaml,.yml,.toml,.ini,.cfg,.conf,.css,.scss,.html,.htm,.xml,.sql,.sh,.ps1,.bat,.cmd,.go,.rs,.java,.kt,.c,.h,.cc,.cpp,.hpp,.cs,.rb,.php,.swift,.vue,.svelte">
              <input class="chat-file-input" id="chat-folder-input" type="file" webkitdirectory multiple aria-label="Add project folder">
              <div class="chat-attachments" id="chat-attachments" aria-live="polite"></div>
              <button class="chat-mic" id="chat-mic" type="button" aria-label="Start voice capture">🎙</button>
              <button class="chat-voice-cancel is-hidden" id="chat-voice-cancel" type="button">Cancel</button>
            </div>
            <div class="chat-composer-actions"><span class="chat-voice-status" id="chat-voice-status"></span><button class="chat-turn-cancel is-hidden" id="chat-turn-cancel" type="button">Stop</button><button class="chat-send" type="submit">Send</button></div>
          </div>
        </form>
      </section>`;
    const handle = document.createElement("button");
    handle.className = "chat-context-handle";
    handle.id = "chat-context-handle";
    handle.type = "button";
    handle.setAttribute("aria-label", "Open chat library");
    handle.title = "Chats, files, outputs, and project knowledge";
    handle.textContent = "Library";
    container.appendChild(handle);
    const history = document.createElement("div");
    history.className = "chat-history-layer";
    history.id = "chat-history-layer";
    history.innerHTML = `<button class="chat-history-scrim" id="chat-history-scrim" type="button" aria-label="Close chat history"></button>
      <aside class="chat-history-panel" id="chat-history-panel" aria-label="Chat context" aria-hidden="true"></aside>`;
    container.appendChild(history);
    const input = container.querySelector("#chat-input");
    const redraw = () => renderThread(container, api);
    const submit = async (event) => {
      event.preventDefault();
      if (api.state.projectImport) {
        showToast("Kairo is still preparing this project's knowledge.", "error");
        return;
      }
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
    const fileInput = container.querySelector("#chat-file-input");
    const folderInput = container.querySelector("#chat-folder-input");
    container.querySelector("#chat-attach").addEventListener("click", () => {
      if (!(api.state.runner && api.state.runner.project && api.state.runner.project.id)) {
        showToast("Choose a project before adding a project folder.", "error");
        return;
      }
      folderInput?.click();
    });
    fileInput?.addEventListener("change", async () => {
      await uploadAttachments(container, api, [...fileInput.files]);
      fileInput.value = "";
    });
    folderInput?.addEventListener("change", async () => {
      await uploadAttachments(container, api, [...folderInput.files], { projectFolder: true });
      folderInput.value = "";
    });
    container.querySelector("#chat-mic").addEventListener("click", async () => {
      const mode = api.state.voice.mode || "dictation";
      await api.toggleVoiceCapture(mode, (transcript) => {
        input.value = input.value ? `${input.value} ${transcript}` : transcript;
        input.dispatchEvent(new Event("input"));
        input.focus();
      });
    });
    container.querySelector("#chat-voice-cancel").addEventListener("click", () => api.cancelVoiceCapture());
    container.querySelector("#chat-turn-cancel").addEventListener("click", async () => {
      if (!api.state.runner?.turn_busy || api.state.turnCancelling) return;
      api.state.turnCancelling = true;
      renderTurnControls(container, api);
      try {
        const result = await api.post("/api/turn/cancel", {});
        if (result.ok && result.data.cancelled) {
          // A successful cancellation settles only when the workspace-scoped turn_cancelled
          // event arrives, or when the server's runner read confirms that the turn is already
          // gone. The latter recovers a transiently missed WebSocket frame without guessing.
          const runner = await api.runnerStatus({ refresh: true });
          if (runner && !runner.turn_busy) api.state.turnCancelling = false;
          renderTurnControls(container, api);
          return;
        }
        // The attended turn can complete between the Stop click and this response. Reconcile
        // from the server so a 200 {cancelled:false} never leaves this control stuck disabled.
        api.state.turnCancelling = false;
        await api.runnerStatus({ refresh: true });
        renderTurnControls(container, api);
        showToast(result.data?.message || "This turn has already finished.", "error");
      } catch {
        api.state.turnCancelling = false;
        await api.runnerStatus({ refresh: true });
        renderTurnControls(container, api);
        showToast("Kairo couldn't stop this turn. Please try again.", "error");
      }
    });
    handle.addEventListener("click", async () => openChatHistory(container, api, {}, redraw));
    container.querySelector("#chat-history-scrim").addEventListener("click", () => closeChatHistory(container));
    mountHeader(container.querySelector("#chat-convo-header"), api, { onChanged: () => redraw() });
  }
  renderPending(container, api);
  renderThread(container, api);
  renderMeta(container, api);
  renderLifecycle(container, api);
  renderVoiceControls(container, api);
  renderTurnControls(container, api);
  renderAttachments(container, api);
  renderImportControls(container, api);
  renderProjectImportProgress(container, api);
}

const VOICE_STATES = {
  listening: "Listening…",
  capturing: "Listening…",
  transcribing: "Transcribing…",
  thinking: "Thinking…",
  speaking: "Speaking safe reply…",
  error: "Voice unavailable",
};

function attachments(api) {
  if (!Array.isArray(api.state.chatAttachments)) api.state.chatAttachments = [];
  return api.state.chatAttachments;
}

function renderAttachments(container, api) {
  const host = container.querySelector("#chat-attachments");
  if (!host) return;
  host.textContent = "";
  for (const attachment of attachments(api)) {
    const chip = document.createElement("span");
    chip.className = `chat-attachment ${attachment.state || "ready"}`;
    chip.title = attachment.error || attachment.title;
    chip.textContent = attachment.error
      ? `${attachment.title} · couldn't add`
      : (attachment.state === "uploading" ? `${attachment.title} · adding…` : attachment.title);
    host.appendChild(chip);
  }
  const importState = api.state.projectImport;
  if (importState) {
    const progress = document.createElement("span");
    progress.className = "chat-attachment uploading";
    const failures = importState.failed ? ` · ${importState.failed} couldn't add` : "";
    const stage = importState.stage === "graph" ? "Building knowledge map" : "Analyzing files";
    progress.textContent = `${stage} ${importState.done}/${importState.total}${failures}`;
    host.appendChild(progress);
  }
}

function renderProjectImportProgress(container, api) {
  const panel = container.querySelector("#project-import-progress");
  if (!panel) return;
  const state = api.state.projectImport;
  panel.classList.toggle("is-hidden", !state);
  if (!state) return;
  const title = panel.querySelector("#project-import-title");
  const detail = panel.querySelector("#project-import-detail");
  const failures = state.failed
    ? ` ${state.failed} file${state.failed === 1 ? "" : "s"} could not be added.` : "";
  if (state.stage === "graph") {
    title.textContent = "Building your local knowledge map";
    detail.textContent = `Graphify is connecting folders and files. ${state.done}/${state.total} files are ready.${failures}`;
  } else {
    title.textContent = "Analyzing your project files";
    detail.textContent = `Kairo is reading and indexing ${state.done}/${state.total} files.${failures}`;
  }
}

function renderImportControls(container, api) {
  const disabled = Boolean(api.state.projectImport);
  for (const selector of ["#chat-input", "#chat-attach", ".chat-send", "#chat-mic"]) {
    const control = container.querySelector(selector);
    if (control) control.disabled = disabled;
  }
}

function renderTurnControls(container, api) {
  const button = container.querySelector("#chat-turn-cancel");
  if (!button) return;
  const busy = Boolean(api.state.runner?.turn_busy);
  if (!busy) api.state.turnCancelling = false;
  button.classList.toggle("is-hidden", !busy);
  button.disabled = !busy || Boolean(api.state.turnCancelling);
  button.textContent = api.state.turnCancelling ? "Stopping…" : "Stop";
  button.title = "Stop this conversation only. Background jobs keep running.";
}

const PROJECT_UPLOADABLE = new Set([
  ".pdf", ".docx", ".pptx", ".xlsx", ".epub", ".md", ".markdown", ".txt", ".text",
  ".csv", ".json", ".jsonl", ".py", ".pyi", ".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx",
  ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf", ".css", ".scss", ".html", ".htm", ".xml",
  ".sql", ".sh", ".ps1", ".bat", ".cmd", ".go", ".rs", ".java", ".kt", ".c", ".h", ".cc",
  ".cpp", ".hpp", ".cs", ".rb", ".php", ".swift", ".vue", ".svelte",
]);
const PROJECT_IMPORT_MAX_FILES = 2000;
const PROJECT_IMPORT_CONCURRENCY = 4;
const PROJECT_EXCLUDED_DIRS = new Set([
  ".git", ".venv", "venv", "node_modules", "dist", "build", "__pycache__", ".pytest_cache",
  ".mypy_cache", ".ruff_cache", ".next", ".turbo", ".cache", ".claude",
]);
const PROJECT_EXCLUDED_PATHS = ["data/connectors", "data/backups", "data/evals"];

function projectPath(file) {
  return String(file.webkitRelativePath || file.name || "").replaceAll("\\", "/");
}

function projectPriority(path) {
  const parts = path.toLowerCase().split("/");
  if (parts.includes("src") || parts.includes("app") || parts.includes("lib")) return 0;
  if (parts.length <= 2 || parts.includes("config")) return 1;
  if (parts.includes("docs")) return 2;
  if (parts.includes("test") || parts.includes("tests")) return 4;
  return 3;
}

function projectFiles(files) {
  return files.filter((file) => {
    const path = projectPath(file);
    const parts = path.toLowerCase().split("/");
    const suffix = path.slice(path.lastIndexOf(".")).toLowerCase();
    const normalized = parts.join("/");
    return PROJECT_UPLOADABLE.has(suffix)
      && !parts.some((part) => PROJECT_EXCLUDED_DIRS.has(part) || part.startsWith(".env"))
      && !PROJECT_EXCLUDED_PATHS.some((excluded) => normalized.includes(`/${excluded}/`));
  }).sort((left, right) => {
    const leftPath = projectPath(left);
    const rightPath = projectPath(right);
    return projectPriority(leftPath) - projectPriority(rightPath)
      || leftPath.localeCompare(rightPath, undefined, { sensitivity: "base" });
  });
}

async function uploadAttachments(container, api, files, { projectFolder = false } = {}) {
  const selected = projectFolder ? projectFiles(files) : files;
  if (!selected.length) {
    if (projectFolder) showToast("No supported, non-sensitive files were found in that folder.", "error");
    return;
  }
  if (projectFolder && selected.length > PROJECT_IMPORT_MAX_FILES) {
    showToast(
      `This folder has ${selected.length.toLocaleString()} eligible files. Split it into folders of at most ${PROJECT_IMPORT_MAX_FILES.toLocaleString()} files.`,
      "error",
    );
    return;
  }
  if (projectFolder) api.state.projectImport = {
    stage: "files", done: 0, total: selected.length, added: 0, duplicates: 0, failed: 0,
    secretFiles: 0,
  };
  renderImportControls(container, api);
  renderProjectImportProgress(container, api);
  let failures = 0;
  const uploadOne = async (file) => {
    const attachment = projectFolder ? null : { title: file.name || "Untitled file", state: "uploading" };
    if (attachment) attachments(api).push(attachment);
    renderAttachments(container, api);
    const form = new FormData();
    form.append("file", file, file.name);
    if (projectFolder) {
      form.append("relative_path", file.webkitRelativePath || file.name);
    }
    try {
      const result = await api.upload("/api/chat/attachments", form);
      if (result.ok) {
        const secretHits = Number(result.data.suspected_secret_hits) || 0;
        if (secretHits > 0) {
          if (projectFolder) api.state.projectImport.secretFiles += 1;
          else showToast(
            "Kairo redacted suspected credentials from AI indexing. Review Notifications.",
            "error",
          );
        }
        if (projectFolder) {
          if (result.data.action === "duplicate") api.state.projectImport.duplicates += 1;
          else api.state.projectImport.added += 1;
        }
        if (attachment) {
          attachment.state = "ready";
          attachment.sourceId = result.data.source_id;
          attachment.title = result.data.title || attachment.title;
        }
      } else {
        if (attachment) {
          attachment.state = "error";
          attachment.error = result.data.message || "Kairo couldn't add this file.";
        }
        failures += 1;
        if (projectFolder) api.state.projectImport.failed += 1;
      }
    } catch {
      if (attachment) {
        attachment.state = "error";
        attachment.error = "Kairo couldn't add this file.";
      }
      failures += 1;
      if (projectFolder) api.state.projectImport.failed += 1;
    } finally {
      if (projectFolder) api.state.projectImport.done += 1;
      renderAttachments(container, api);
      renderProjectImportProgress(container, api);
    }
  };
  if (projectFolder) {
    let next = 0;
    const worker = async () => {
      while (next < selected.length) {
        const file = selected[next];
        next += 1;
        await uploadOne(file);
      }
    };
    await Promise.all(Array.from(
      { length: Math.min(PROJECT_IMPORT_CONCURRENCY, selected.length) }, worker,
    ));
  } else {
    for (const file of selected) await uploadOne(file);
  }
  if (projectFolder) {
    api.state.projectImport.stage = "graph";
    renderAttachments(container, api);
    renderProjectImportProgress(container, api);
    try {
      const finalize = new FormData();
      finalize.append("finalize", "true");
      const graph = await api.upload("/api/chat/attachments", finalize);
      if (!graph.ok) failures += 1;
    } catch {
      failures += 1;
    }
    const summary = api.state.projectImport;
    api.state.projectImport = null;
    renderAttachments(container, api);
    renderImportControls(container, api);
    renderProjectImportProgress(container, api);
    const indexed = (summary.added + summary.duplicates).toLocaleString();
    const secretNote = summary.secretFiles
      ? ` ${summary.secretFiles.toLocaleString()} file${summary.secretFiles === 1 ? "" : "s"} need credential review.`
      : "";
    showToast(
      failures
        ? `Project import finished: ${indexed}/${summary.total.toLocaleString()} files indexed; ${summary.failed.toLocaleString()} couldn't be added.${secretNote}`
        : `Project import complete: ${indexed} files indexed.${secretNote} Open Knowledge to explore the tree.`,
      failures || summary.secretFiles ? "error" : "success",
    );
  }
}

function renderVoiceControls(container, api) {
  const voice = api.state.voice || {};
  const mode = voice.mode || "dictation";
  const state = voice.listening || "idle";
  const disabled = !voice.enabled || voice.browserCapture === false;
  const mic = container.querySelector("#chat-mic");
  const cancel = container.querySelector("#chat-voice-cancel");
  const status = container.querySelector("#chat-voice-status");
  if (!mic || !cancel || !status) return;
  mic.disabled = disabled || ["transcribing", "thinking", "speaking"].includes(state);
  mic.textContent = state === "listening" || state === "capturing" ? "■ Stop" : "🎙";
  mic.title = disabled ? (voice.reason || "Voice is unavailable.")
    : (state === "listening" || state === "capturing" ? "Stop and continue" : `Start ${mode}`);
  const cancellable = ["listening", "capturing", "speaking"].includes(state);
  cancel.classList.toggle("is-hidden", !cancellable);
  // Idle voice is not a status worth showing. Capturing, errors, and an unavailable mic are.
  status.textContent = disabled ? "Voice unavailable"
    : (state === "idle" ? "" : (state === "error"
      ? `Voice error: ${voice.reason || "try again"}` : (VOICE_STATES[state] || "")));
  status.title = disabled ? (voice.reason || "Voice is unavailable.") : "";
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
  const hour = new Date().getHours();
  const greeting = hour < 12 ? "Good morning." : (hour < 18 ? "Good afternoon." : "Good evening.");
  const hints = [
    "Start with a question, or choose a project to keep your work in context.",
    "Try asking for a plan, a review, or help with the project you have open.",
    "Keep it simple: ask, attach a source, and continue when you're ready.",
  ];
  const hint = hints[new Date().getDate() % hints.length];
  renderConversation(container.querySelector("#chat-thread"), api.state, {
    emptyHeading: greeting,
    emptyHint: hint,
  });
}

function renderMeta(container, api) {
  const meta = container.querySelector("#chat-turn-meta");
  if (!meta) return;
  const runner = api.state.runner || {};
  const last = typeof runner.last_turn_cost_usd === "number"
    ? `last $${runner.last_turn_cost_usd.toFixed(4)}`
    : "";
  const route = runner.last_turn_model
    ? `${runner.last_turn_provider ? `${runner.last_turn_provider} · ` : ""}${runner.last_turn_model}` : "";
  meta.textContent = [last, route].filter(Boolean).join(" · ");
  meta.title = runner.auto_may_classify ? "Auto may classify before the main model." : "";
}

function closeChatHistory(container) {
  const layer = container.querySelector("#chat-history-layer");
  const panel = container.querySelector("#chat-history-panel");
  if (!layer || !panel) return;
  layer.classList.remove("open");
  panel.setAttribute("aria-hidden", "true");
}

function historyButton(label, title, onClick, className = "chat-history-action") {
  const button = document.createElement("button");
  button.type = "button";
  button.className = className;
  button.title = title;
  button.setAttribute("aria-label", title);
  button.textContent = label;
  button.addEventListener("click", onClick);
  return button;
}

async function openChatHistory(container, api, detail, redraw) {
  const layer = container.querySelector("#chat-history-layer");
  const panel = container.querySelector("#chat-history-panel");
  if (!layer || !panel) return;
  layer.classList.add("open");
  panel.setAttribute("aria-hidden", "false");

  const refresh = async () => {
    const [data, runner, files, outputs, knowledge] = await Promise.all([
      api.get("/api/sessions?limit=50"), api.runnerStatus(),
      api.get("/api/chat/files"), api.get("/api/chat/outputs"),
      api.get("/api/chat/knowledge"),
    ]);
    if (!layer.classList.contains("open")) return;
    renderChatHistory(
      panel, container, api, data || detail, runner || {}, files || {}, outputs || {}, knowledge || {}, redraw, refresh
    );
    await refreshHeader();
    redraw();
  };
  await refresh();
}

function shelfTab(label, tab, selected, onClick) {
  const button = historyButton(label, `${label} tab`, onClick, "chat-shelf-tab");
  button.classList.toggle("active", selected);
  button.setAttribute("aria-selected", String(selected));
  button.setAttribute("role", "tab");
  return button;
}

function emptyShelf(text) {
  const empty = document.createElement("p");
  empty.className = "chat-history-empty";
  empty.textContent = text;
  return empty;
}

function renderChatHistory(panel, container, api, data, runner, files, outputs, knowledge, redraw, refresh) {
  panel.textContent = "";
  const tab = panel.dataset.tab || "chats";
  const head = document.createElement("div");
  head.className = "chat-history-head";
  const heading = document.createElement("div");
  const title = document.createElement("strong");
  title.textContent = "Chat shelf";
  const scope = document.createElement("span");
  scope.textContent = runner.project && runner.project.name ? runner.project.name : "No project";
  heading.append(title, scope);
  head.append(heading, historyButton("×", "Close chat history", () => closeChatHistory(container)));
  panel.appendChild(head);

  const tabs = document.createElement("div");
  tabs.className = "chat-shelf-tabs";
  tabs.setAttribute("role", "tablist");
  for (const [name, label] of [["chats", "Chats"], ["files", "Files"], ["outputs", "Outputs"], ["knowledge", "Knowledge"]]) {
    tabs.appendChild(shelfTab(label, name, tab === name, () => {
      panel.dataset.tab = name;
      renderChatHistory(panel, container, api, data, runner, files, outputs, knowledge, redraw, refresh);
    }));
  }
  panel.appendChild(tabs);

  if (tab === "files") {
    renderChatFiles(panel, files);
    return;
  }
  if (tab === "outputs") {
    renderChatOutputs(panel, api, outputs);
    return;
  }
  if (tab === "knowledge") {
    renderProjectKnowledge(panel, container, api, knowledge, runner, refresh);
    return;
  }
  renderChatList(panel, container, api, data, runner, redraw, refresh);
}

function renderChatList(panel, container, api, data, runner, redraw, refresh) {

  const create = historyButton("＋ New chat", "Start a new chat", async () => {
    if (runner.session_save_state === "failed" && !await confirmDialog({
      title: "Start a new chat?",
      message: "This chat could not be saved. Keep this tab open and copy anything important before switching.",
      confirmLabel: "Start new chat", tone: "attention",
    })) return;
    const result = await api.post("/api/sessions/new", {});
    if (result.ok) {
      closeChatHistory(container); await refreshHeader(); redraw(); showToast("New chat ready.");
    } else showToast("Kairo couldn't start a new chat.", "error");
  }, "chat-history-new");
  panel.appendChild(create);

  const list = document.createElement("div");
  list.className = "chat-history-list";
  const sessions = Array.isArray(data.sessions) ? data.sessions : [];
  const activeId = runner.session_id;
  if (!sessions.length) {
    const empty = document.createElement("p");
    empty.className = "chat-history-empty";
    empty.textContent = "No saved chats in this scope yet.";
    list.appendChild(empty);
  }
  for (const session of sessions) {
    const item = document.createElement("div");
    item.className = "chat-history-item" + (session.id === activeId ? " active" : "");
    const open = historyButton(session.title || `Chat ${session.id}`, "Open chat", async () => {
      if (session.id === activeId) { closeChatHistory(container); return; }
      if (await api.resumeChat(Number(session.id))) {
        closeChatHistory(container); await refreshHeader(); redraw();
      }
    }, "chat-history-open");
    const titleRow = document.createElement("div");
    titleRow.className = "chat-history-row";
    titleRow.appendChild(open);
    if (session.pinned) {
      const pin = document.createElement("span");
      pin.className = "chat-history-pin";
      pin.title = "Pinned";
      pin.textContent = "⌖";
      titleRow.appendChild(pin);
    }
    const actions = document.createElement("div");
    actions.className = "chat-history-actions";
    actions.append(
      historyButton("✎", "Rename chat", async () => {
        const next = await promptDialog({
          title: "Rename chat", message: "Choose a short title you will recognize later.",
          value: session.title || "", confirmLabel: "Rename",
        });
        if (!next) return;
        const result = await api.post(`/api/sessions/${session.id}/rename`, { title: next });
        if (result.ok) { await refresh(); showToast("Chat renamed."); }
        else showToast("Kairo couldn't rename this chat.", "error");
      }),
      historyButton(session.pinned ? "⌖" : "⌑", session.pinned ? "Unpin chat" : "Pin chat", async () => {
        const result = await api.post(`/api/sessions/${session.id}/pin`, { pinned: !session.pinned });
        if (result.ok) { await refresh(); showToast(session.pinned ? "Chat unpinned." : "Chat pinned."); }
        else showToast("Kairo couldn't update this chat.", "error");
      }),
      historyButton("⌫", "Archive chat", async () => {
        const message = session.id === activeId && runner.session_save_state === "failed"
          ? "This chat could not be saved. Keep this tab open and copy anything important before archiving it."
          : "It stays recoverable, but leaves your active chat list.";
        if (!await confirmDialog({
          title: "Archive this chat?", message, confirmLabel: "Archive chat", tone: "attention",
        })) return;
        const result = await api.post(`/api/sessions/${session.id}/archive`, { archived: true });
        if (result.ok) { await refresh(); showToast("Chat archived."); }
        else showToast("Kairo couldn't archive this chat.", "error");
      }, "chat-history-action danger"),
    );
    item.append(titleRow, actions);
    list.appendChild(item);
  }
  panel.appendChild(list);
}

function renderChatFiles(panel, files) {
  const intro = document.createElement("p");
  intro.className = "chat-shelf-intro";
  intro.textContent = "Files added here are available only to this chat's scoped knowledge.";
  panel.appendChild(intro);
  const list = document.createElement("div");
  list.className = "chat-history-list";
  const rows = Array.isArray(files.files) ? files.files : [];
  if (!rows.length) {
    list.appendChild(emptyShelf("Add a PDF, document, or code file with the + button."));
  }
  for (const file of rows) {
    const item = document.createElement("div");
    item.className = "chat-history-item";
    const name = document.createElement("strong");
    name.className = "chat-shelf-item-title";
    name.textContent = file.title || "Untitled file";
    const meta = document.createElement("span");
    meta.className = "chat-shelf-item-meta";
    const state = file.review_status === "reviewed" ? "Ready in knowledge" : "Needs review";
    meta.textContent = [state, file.mime || file.kind, formatTime(file.created_at)].filter(Boolean).join(" · ");
    item.append(name, meta);
    list.appendChild(item);
  }
  panel.appendChild(list);
}

function renderChatOutputs(panel, api, outputs) {
  const intro = document.createElement("p");
  intro.className = "chat-shelf-intro";
  intro.textContent = "Project outputs are saved here. Exact per-chat output provenance is coming next.";
  panel.appendChild(intro);
  const list = document.createElement("div");
  list.className = "chat-history-list";
  const rows = Array.isArray(outputs.artifacts) ? outputs.artifacts : [];
  if (!rows.length) {
    list.appendChild(emptyShelf("Reports, images, and other generated outputs will appear here."));
  }
  for (const artifact of rows) {
    const item = document.createElement("div");
    item.className = "chat-history-item";
    const row = document.createElement("div");
    row.className = "chat-history-row";
    const name = document.createElement("strong");
    name.className = "chat-shelf-item-title";
    name.textContent = artifact.title || "Untitled output";
    row.appendChild(name);
    if (artifact.has_content) {
      const download = historyButton("↓", "Download output", async () => {
        const ok = await api.download(
          `/api/chat/outputs/${encodeURIComponent(artifact.id)}/content`, artifact.title || "kairo-output"
        );
        if (!ok) download.title = "Kairo couldn't download this output";
      });
      row.appendChild(download);
    }
    const meta = document.createElement("span");
    meta.className = "chat-shelf-item-meta";
    meta.textContent = [artifact.kind, formatTime(artifact.created_at)].filter(Boolean).join(" · ");
    item.append(row, meta);
    list.appendChild(item);
  }
  panel.appendChild(list);
}

function renderProjectKnowledge(panel, container, api, knowledge, runner, refresh) {
  const projectId = Number(knowledge.project_id);
  if (!Number.isInteger(projectId) || projectId <= 0) {
    panel.append(
      emptyShelf("Choose a project to keep its files, retrieval, and knowledge graph together."),
    );
    return;
  }

  const projectName = runner.project && runner.project.name ? runner.project.name : "this project";
  const intro = document.createElement("p");
  intro.className = "chat-shelf-intro";
  intro.textContent = `Files in ${projectName} are available to chats in this project. Source bodies and local paths stay private.`;
  panel.appendChild(intro);

  const graph = knowledge.graph || {};
  const stats = document.createElement("div");
  stats.className = "chat-knowledge-stats";
  for (const [label, value] of [
    ["Sources", Number(knowledge.source_count) || 0],
    ["Nodes", Array.isArray(graph.nodes) ? graph.nodes.length : 0],
    ["Links", Number(graph.edge_count) || 0],
  ]) {
    const stat = document.createElement("span");
    stat.textContent = `${label} ${value}`;
    stats.appendChild(stat);
  }
  panel.appendChild(stats);

  const folderImports = Array.isArray(knowledge.folder_imports) ? knowledge.folder_imports : [];
  if (folderImports.length) {
    const heading = document.createElement("strong");
    heading.className = "chat-shelf-section-label";
    heading.textContent = "Attached folders";
    panel.appendChild(heading);
    const folders = document.createElement("div");
    folders.className = "chat-history-list";
    for (const folder of folderImports) {
      const item = document.createElement("div");
      item.className = "chat-history-item";
      const row = document.createElement("div");
      row.className = "chat-history-row";
      const label = document.createElement("strong");
      label.className = "chat-shelf-item-title";
      label.textContent = folder.root || "Imported folder";
      const remove = historyButton("Remove", "Remove this folder from the project", async () => {
        const count = Number(folder.source_count) || 0;
        if (!await confirmDialog({
          title: "Remove this folder from the project?",
          message: `Kairo will stop using ${count} file${count === 1 ? "" : "s"} from ${folder.root}. The source records stay in the local audit trail, and you can attach another folder afterwards.`,
          confirmLabel: "Remove folder", tone: "attention",
        })) return;
        const result = await api.post("/api/chat/knowledge/detach", { root: folder.root });
        if (result.ok) {
          const sources = Number(result.data.detached_sources || 0);
          const chunks = Number(result.data.cleared_chunks || 0);
          const message = sources
            ? `Removed ${sources} project source${sources === 1 ? "" : "s"}${chunks ? ` and cleared ${chunks} indexed section${chunks === 1 ? "" : "s"}` : ""}.`
            : `Cleared ${chunks} indexed section${chunks === 1 ? "" : "s"} from this detached folder.`;
          showToast(message);
          await refresh();
        } else showToast("Kairo couldn't remove that folder from this project.", "error");
      }, "chat-history-action danger");
      row.append(label, remove);
      const meta = document.createElement("span");
      meta.className = "chat-shelf-item-meta";
      const count = Number(folder.source_count) || 0;
      meta.textContent = `${count} file${count === 1 ? "" : "s"} · available to this project`;
      item.append(row, meta);
      folders.appendChild(item);
    }
    panel.appendChild(folders);
  }

  const sources = Array.isArray(knowledge.sources) ? knowledge.sources : [];
  const sourcesHeading = document.createElement("strong");
  sourcesHeading.className = "chat-shelf-section-label";
  sourcesHeading.textContent = "Project files";
  panel.appendChild(sourcesHeading);
  const sourceList = document.createElement("div");
  sourceList.className = "chat-source-tree";
  if (!sources.length) {
    sourceList.appendChild(emptyShelf("Add a document or code file with the + button to start this project's knowledge."));
  } else {
    sourceList.appendChild(renderSourceTree(sources));
  }
  if (knowledge.sources_truncated) sourceList.appendChild(
    emptyShelf("Showing the first 300 project sources. Narrow the import to explore a smaller tree."),
  );
  panel.appendChild(sourceList);

  const graphHeading = document.createElement("strong");
  graphHeading.className = "chat-shelf-section-label";
  graphHeading.textContent = "Knowledge connections";
  panel.appendChild(graphHeading);
  const nodes = Array.isArray(graph.nodes) ? graph.nodes : [];
  const preview = document.createElement("div");
  preview.className = "chat-knowledge-preview";
  if (!nodes.length) {
    preview.appendChild(emptyShelf(
      graph.available
        ? "Sources are ready. Build the derived graph to show their connections."
        : "The project graph is unavailable in this Kairo session.",
    ));
  } else {
    const root = document.createElement("div");
    root.className = "chat-knowledge-root";
    root.textContent = projectName;
    preview.appendChild(root);
    for (const node of nodes.filter((node) => node.kind !== "project").slice(0, 6)) {
      const row = document.createElement("div");
      row.className = "chat-knowledge-node";
      const link = document.createElement("span");
      link.setAttribute("aria-hidden", "true");
      link.textContent = "↳";
      const label = document.createElement("span");
      label.textContent = node.label || node.kind || "Knowledge node";
      const type = document.createElement("small");
      type.textContent = node.kind || "node";
      row.append(link, label, type);
      preview.appendChild(row);
    }
  }
  panel.appendChild(preview);

  const actions = document.createElement("div");
  actions.className = "chat-knowledge-actions";
  const openGraph = historyButton("Open full graph", "Open this project's Knowledge Graph", () => {
    window.location.hash = `#workspace/${projectId}/graph`;
    closeChatHistory(container);
  }, "chat-history-new");
  const buildGraph = historyButton("Copy: build graph", "Copy the project graph rebuild command", async () => {
    try {
      await navigator.clipboard.writeText("uv run jarvis graph rebuild");
      showToast("Graph rebuild command copied.");
    } catch {
      showToast("Kairo couldn't copy the graph command.", "error");
    }
  }, "chat-history-action");
  actions.append(openGraph, buildGraph);
  panel.appendChild(actions);
}

function saveLabel(state) {
  return {
    new: "New chat · unsaved", saving: "Saving…", saved: "Saved", failed: "Save failed",
  }[state] || "New chat · unsaved";
}

function formatTime(value) {
  if (!value) return "";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "" : date.toLocaleString([], { dateStyle: "medium", timeStyle: "short" });
}

function renderLifecycle(container, api) {
  const runner = api.state.runner || {};
  const state = runner.session_save_state || "new";
  const status = container.querySelector("#chat-lifecycle-status");
  if (status) {
    if (state === "new") {
      status.textContent = "";
      status.classList.remove("error");
      return;
    }
    const time = formatTime(runner.session_updated_at || runner.session_created_at);
    status.textContent = state === "failed"
      ? "Save failed — this chat is still open in this tab. Keep it open and copy anything important before switching."
      : [saveLabel(state), time ? `Updated ${time}` : ""].filter(Boolean).join(" · ");
    status.classList.toggle("error", state === "failed");
  }
}
