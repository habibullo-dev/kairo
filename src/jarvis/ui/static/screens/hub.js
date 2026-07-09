// Hub — the honest availability grid. It renders the SHARED capability truth (h.capabilities: the
// same rows Daily and Settings show, so the three can never disagree): connectors / providers /
// services / voice / MCP, each with a state, whether it's actually usable IN CHAT, and a plain
// reason when it isn't. Presence/state/reason only — never a key value. Egress detail is Debug-only.
export async function render(container, api) {
  const h = await api.get("/api/hub");
  if (!h) {
    container.innerHTML = `<div class="rise"><h1>Hub</h1><div class="sub">Unavailable.</div></div>`;
    return;
  }
  const cap = h.capabilities || {};
  const voiceRow = { name: "Voice", state: (cap.voice || {}).state || "off",
    exposed_to_chat: (cap.voice || {}).exposed_to_chat, reason: (cap.voice || {}).reason };
  const mcpRow = { name: "MCP", state: (cap.mcp || {}).state || "not_configured",
    exposed_to_chat: false, reason: (cap.mcp || {}).reason };
  const keys = Object.entries(h.providers || {})
    .map(([k, on]) => `<tr><td>${esc(k)}</td>
      <td style="text-align:right"><span class="tag ${on ? "ok" : ""}">${on ? "present" : "absent"}</span></td></tr>`)
    .join("");
  const eg = h.egress || {};
  container.innerHTML = `
    <div class="rise"><h1>Hub</h1>
      <div class="sub">What's connected and what Kairo can actually use in chat — presence & state
      only, never a key value.</div></div>
    ${card("Connectors", cap.connectors || [])}
    ${card("Model providers", cap.providers || [])}
    ${card("Services & tools", cap.services || [])}
    ${card("Voice & MCP", [voiceRow, mcpRow])}
    <div class="card rise"><div class="card-label">API keys</div><table>${keys}</table>
      <div class="mono dim debug-only" style="margin-top:8px">egress — audio ${eg.audio_bytes || 0}B · text ${eg.text_chars || 0} chars</div></div>`;
}

// One capability group as a card + table: name (+ "not in chat" when connected-but-unexposed),
// a plain reason, and a state tag toned by whether it's usable.
function card(title, rows) {
  if (!rows.length) {
    return `<div class="card rise"><div class="card-label">${esc(title)}</div>
      <div class="dim">Nothing here yet.</div></div>`;
  }
  const body = rows.map((r) => {
    const usable = ["connected", "available", "on"].includes(r.state) && r.exposed_to_chat;
    const tone = usable ? "ok" : r.state === "needs_reconnect" ? "warn" : "";
    const notInChat = ["connected", "available", "on"].includes(r.state) && !r.exposed_to_chat;
    return `<tr>
      <td>${esc(r.name)}${notInChat ? ' <span class="dim">· not in chat</span>' : ""}</td>
      <td class="dim" style="font-size:12px">${esc(r.reason || "")}</td>
      <td style="text-align:right"><span class="tag ${tone}">${esc(r.state)}</span></td></tr>`;
  }).join("");
  return `<div class="card rise"><div class="card-label">${esc(title)}</div>
    <table>${body}</table></div>`;
}

function esc(s) { const d = document.createElement("div"); d.textContent = s ?? ""; return d.innerHTML; }
