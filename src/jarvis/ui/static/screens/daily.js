// Daily Mode — the calm default. One chat stream + a composer. Tool activity is one quiet
// line per call (expandable detail lives in Trace/Debug). No simultaneous panels.

export function render(container, api) {
  if (!container.querySelector("#composer-input")) {
    container.innerHTML = `
      <h1>Daily</h1>
      <div class="sub">Ask Kairo anything. Risky actions confirm on screen.</div>
      <div class="chat" id="chat"></div>
      <div class="composer"><div class="box">
        <input id="composer-input" placeholder="Message Kairo…" autocomplete="off">
        <button id="composer-send">Send</button>
      </div></div>`;
    const input = container.querySelector("#composer-input");
    const send = async () => {
      const text = input.value.trim();
      if (!text) return;
      input.value = "";
      api.state.chat.push({ role: "user", text });
      renderChat(container, api);
      const res = await api.post("/api/turn", { text });
      if (!res.ok) {
        api.state.chat.push({ role: "assistant", text: `— ${res.data.message || "busy"} —` });
        renderChat(container, api);
      }
    };
    container.querySelector("#composer-send").addEventListener("click", send);
    input.addEventListener("keydown", (e) => { if (e.key === "Enter") send(); });
  }
  renderChat(container, api);
}

function renderChat(container, api) {
  const chat = container.querySelector("#chat");
  if (!chat) return;
  chat.innerHTML = "";
  for (const item of api.state.chat) {
    const div = document.createElement("div");
    if (item.tool) {
      div.className = "toolline" + (item.resolution === "deny" ? " deny" : "");
      div.textContent = `${item.tool} · ${item.resolution}`;
    } else {
      div.className = "msg " + item.role;
      div.textContent = item.text;
    }
    chat.appendChild(div);
  }
  chat.scrollTop = chat.scrollHeight;
}

// Called by app.js for every streamed loop event; keeps Daily quiet (summary lines only).
export function onEvent(state, evt) {
  if (evt.type === "text_delta") {
    let last = state.chat[state.chat.length - 1];
    if (!last || last.role !== "assistant" || !last.live) {
      last = { role: "assistant", text: "", live: true };
      state.chat.push(last);
    }
    last.text += evt.text;
  } else if (evt.type === "tool_started") {
    state.chat.push({ tool: evt.name, resolution: "allow" });
  } else if (evt.type === "tool_decision" && evt.resolution === "deny") {
    state.chat.push({ tool: evt.name, resolution: "denied" });
  } else if (evt.type === "turn_completed") {
    const last = state.chat[state.chat.length - 1];
    if (last && last.role === "assistant") last.live = false;
  }
}
