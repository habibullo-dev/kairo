// Shared attended-conversation rendering. Both Chat and Daily consume the same state reducer and
// POST /api/turn path; this module deliberately owns no authority and never renders model text as
// HTML.

function messageBody(text) {
  const fragment = document.createDocumentFragment();
  const parts = String(text || "").split(/(```[\s\S]*?```)/g);
  for (const part of parts) {
    if (!part) continue;
    if (part.startsWith("```") && part.endsWith("```")) {
      const code = document.createElement("pre");
      code.className = "message-code";
      code.textContent = part.slice(3, -3).replace(/^\w*\n/, "");
      fragment.appendChild(code);
    } else {
      const block = document.createElement("div");
      block.className = "message-text";
      block.textContent = part;
      fragment.appendChild(block);
    }
  }
  return fragment;
}

export function renderConversation(host, state, { emptyHeading, emptyHint } = {}) {
  if (!host) return;
  host.textContent = "";
  if (!state.chat.length) {
    const empty = document.createElement("div");
    empty.className = "empty-state";
    const heading = document.createElement("h4");
    heading.textContent = emptyHeading || "No messages yet";
    const hint = document.createElement("div");
    hint.textContent = emptyHint || "Start a conversation with Kairo.";
    empty.append(heading, hint);
    host.appendChild(empty);
    return;
  }
  for (const item of state.chat) {
    if (item.tool) {
      const line = document.createElement("div");
      line.className = "toolline" + (item.resolution === "denied" ? " deny" : "");
      line.textContent = `${item.tool} · ${item.resolution}`;
      host.appendChild(line);
      continue;
    }
    const message = document.createElement("article");
    message.className = "msg " + item.role;
    message.appendChild(messageBody(item.text));
    if (item.role === "assistant" && item.text) {
      const copy = document.createElement("button");
      copy.className = "message-copy";
      copy.type = "button";
      copy.textContent = "Copy";
      copy.addEventListener("click", () => navigator.clipboard?.writeText(item.text));
      message.appendChild(copy);
    }
    host.appendChild(message);
  }
  host.scrollTop = host.scrollHeight;
}

export async function submitConversationTurn(api, input, redraw) {
  const text = input.value.trim();
  if (!text) return;
  input.value = "";
  api.state.chat.push({ role: "user", text });
  redraw();
  const result = await api.post("/api/turn", { text });
  if (!result.ok) {
    api.state.chat.push({ role: "assistant", text: `— ${result.data.message || "busy"} —` });
    redraw();
  }
}

export function onConversationEvent(state, evt) {
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
