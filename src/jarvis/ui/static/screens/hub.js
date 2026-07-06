// Hub — connectors/providers status. Presence booleans only (never a key value). MCP is an
// honest "not connected — future phase" placeholder.
export async function render(container, api) {
  const h = await api.get("/api/hub");
  if (!h) { container.innerHTML = `<h1>Hub</h1><div class="sub">Unavailable.</div>`; return; }
  const providers = Object.entries(h.providers || {})
    .map(([k, on]) => `<tr><td>${k}</td><td><span class="tag ${on ? "ok" : ""}">${on ? "connected" : "not set"}</span></td></tr>`)
    .join("");
  const v = h.voice || {};
  const eg = h.egress || {};
  container.innerHTML = `
    <h1>Hub</h1><div class="sub">Connectors & providers. Keys are shown as present/absent only.</div>
    <div class="card"><div class="label">Providers</div><table>${providers}</table></div>
    <div class="card"><div class="label">Voice</div>
      <div class="dim mono">cloud: ${v.cloud_providers} · stt: ${v.stt_provider} · tts: ${v.tts_provider}</div>
      <div class="dim mono debug-only">egress — audio ${eg.audio_bytes || 0}B · text ${eg.text_chars || 0} chars</div>
    </div>
    <div class="card"><div class="label">MCP</div><div class="dim">${h.mcp ? h.mcp.note : "—"}</div></div>`;
}
