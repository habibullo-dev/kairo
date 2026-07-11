// Shared attended-conversation rendering. Both Chat and Daily consume the same state reducer and
// POST /api/turn path; this module deliberately owns no authority. The small Markdown subset
// below creates every node itself and places all dynamic text with textContent: raw HTML, SVG,
// images, event handlers, and unsupported syntax remain inert text rather than markup.

const FENCE = /^```\s*([A-Za-z0-9_+-]*)\s*$/;
const HEADING = /^(#{1,3})\s+(.+?)\s*#*\s*$/;
const BULLET = /^[-*+]\s+(.+)$/;
const NUMBERED = /^\d+[.)]\s+(.+)$/;
const QUOTE = /^>\s?(.*)$/;
const INLINE = /`([^`\n]+)`|(?<!\!)\[([^\]\n]+)\]\(([^()\s]+)\)|\*\*([^*\n]+)\*\*|__([^_\n]+)__|(?<![\w*])\*([^\s*\n](?:[^*\n]*[^\s*\n])?)\*(?![\w*])|(?<![\w_])_([^\s_\n](?:[^_\n]*[^\s_\n])?)_(?![\w_])/g;

function safeHref(value) {
  try {
    const url = new URL(value);
    return ["https:", "http:"].includes(url.protocol) ? url.href : null;
  } catch {
    return null;
  }
}

function appendInline(parent, text) {
  const source = String(text || "");
  let cursor = 0;
  INLINE.lastIndex = 0;
  for (const match of source.matchAll(INLINE)) {
    const index = match.index || 0;
    if (index > cursor) parent.appendChild(document.createTextNode(source.slice(cursor, index)));
    if (match[1] != null) {
      const code = document.createElement("code");
      code.className = "message-inline-code";
      code.textContent = match[1];
      parent.appendChild(code);
    } else if (match[2] != null) {
      const href = safeHref(match[3]);
      if (!href) {
        parent.appendChild(document.createTextNode(match[0]));
      } else {
        const link = document.createElement("a");
        link.className = "message-link";
        link.href = href;
        link.target = "_blank";
        link.rel = "noopener noreferrer";
        link.textContent = match[2];
        parent.appendChild(link);
      }
    } else if (match[4] != null || match[5] != null) {
      const strong = document.createElement("strong");
      strong.className = "message-strong";
      strong.textContent = match[4] || match[5];
      parent.appendChild(strong);
    } else {
      const emph = document.createElement("em");
      emph.className = "message-emphasis";
      emph.textContent = match[6] || match[7] || "";
      parent.appendChild(emph);
    }
    cursor = index + match[0].length;
  }
  if (cursor < source.length) parent.appendChild(document.createTextNode(source.slice(cursor)));
}

function copyText(text) {
  navigator.clipboard?.writeText(text);
}

function codeBlock(language, text) {
  const wrap = document.createElement("section");
  wrap.className = "message-code-block";
  const head = document.createElement("div");
  head.className = "message-code-head";
  const label = document.createElement("span");
  label.className = "message-code-language";
  label.textContent = language || "text";
  const copy = document.createElement("button");
  copy.className = "message-code-copy";
  copy.type = "button";
  copy.textContent = "Copy code";
  copy.addEventListener("click", () => copyText(text));
  head.append(label, copy);
  const pre = document.createElement("pre");
  pre.className = "message-code";
  const code = document.createElement("code");
  code.textContent = text;
  pre.appendChild(code);
  wrap.append(head, pre);
  return wrap;
}

function paragraph(lines) {
  const node = document.createElement("p");
  node.className = "message-paragraph";
  appendInline(node, lines.join("\n"));
  return node;
}

function heading(level, text) {
  const node = document.createElement(level === 1 ? "h2" : level === 2 ? "h3" : "h4");
  node.className = `message-heading h${level}`;
  appendInline(node, text);
  return node;
}

// This is intentionally a narrow, line-oriented subset. Tables, images, HTML, and extensions are
// plain text. Keeping the grammar small makes each allowed output explicit.
export function renderMarkdown(text) {
  const fragment = document.createDocumentFragment();
  const lines = String(text || "").replace(/\r\n?/g, "\n").split("\n");
  const root = document.createElement("div");
  root.className = "message-markdown";
  let i = 0;
  while (i < lines.length) {
    if (!lines[i].trim()) { i += 1; continue; }
    const fence = lines[i].match(FENCE);
    if (fence) {
      const start = i;
      const code = [];
      i += 1;
      while (i < lines.length && !FENCE.test(lines[i])) { code.push(lines[i]); i += 1; }
      if (i < lines.length) {
        root.appendChild(codeBlock(fence[1], code.join("\n")));
        i += 1;
        continue;
      }
      // An unfinished streamed fence is ordinary text until the closing fence arrives.
      root.appendChild(paragraph(lines.slice(start)));
      break;
    }
    const head = lines[i].match(HEADING);
    if (head) {
      root.appendChild(heading(head[1].length, head[2]));
      i += 1;
      continue;
    }
    const bullet = lines[i].match(BULLET);
    const numbered = lines[i].match(NUMBERED);
    if (bullet || numbered) {
      const list = document.createElement(bullet ? "ul" : "ol");
      list.className = "message-list";
      const pattern = bullet ? BULLET : NUMBERED;
      while (i < lines.length) {
        const item = lines[i].match(pattern);
        if (!item) break;
        const li = document.createElement("li");
        appendInline(li, item[1]);
        list.appendChild(li);
        i += 1;
      }
      root.appendChild(list);
      continue;
    }
    const quote = lines[i].match(QUOTE);
    if (quote) {
      const block = document.createElement("blockquote");
      block.className = "message-quote";
      const quoteLines = [];
      while (i < lines.length) {
        const line = lines[i].match(QUOTE);
        if (!line) break;
        quoteLines.push(line[1]);
        i += 1;
      }
      appendInline(block, quoteLines.join("\n"));
      root.appendChild(block);
      continue;
    }
    const plain = [];
    while (i < lines.length && lines[i].trim() && !FENCE.test(lines[i])
      && !HEADING.test(lines[i])
      && !BULLET.test(lines[i]) && !NUMBERED.test(lines[i]) && !QUOTE.test(lines[i])) {
      plain.push(lines[i]);
      i += 1;
    }
    root.appendChild(paragraph(plain));
  }
  fragment.appendChild(root);
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
    if (item.role === "assistant") message.appendChild(renderMarkdown(item.text));
    else {
      const body = document.createElement("div");
      body.className = "message-text";
      body.textContent = item.text || "";
      message.appendChild(body);
    }
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
