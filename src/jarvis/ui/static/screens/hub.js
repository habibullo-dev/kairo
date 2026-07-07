// Hub — connectors/providers status. Presence booleans only (never a key value). MCP is an
// honest "not connected — future phase" placeholder. Egress detail is Debug-only.
export async function render(container, api) {
  const h = await api.get("/api/hub");
  if (!h) { container.innerHTML = `<div class="rise"><h1>Hub</h1><div class="sub">Unavailable.</div></div>`; return; }
  const providers = Object.entries(h.providers || {})
    .map(([k, on]) => `<tr><td>${esc(k)}</td>
      <td style="text-align:right"><span class="tag ${on ? "ok" : ""}">${on ? "connected" : "not set"}</span></td></tr>`)
    .join("");
  const v = h.voice || {};
  const eg = h.egress || {};
  container.innerHTML = `
    <div class="rise"><h1>Hub</h1>
      <div class="sub">Connectors & providers. Keys are shown as present/absent only — never a value.</div></div>
    <div class="card rise"><div class="card-label">Providers</div><table>${providers}</table></div>
    <div class="card rise"><div class="card-label">Voice</div>
      <div class="mono dim">cloud providers ${v.cloud_providers} · STT ${esc(v.stt_provider)} · TTS ${esc(v.tts_provider)}</div>
      <div class="mono dim debug-only" style="margin-top:6px">egress — audio ${eg.audio_bytes || 0}B · text ${eg.text_chars || 0} chars</div></div>
    <div class="card rise"><div class="card-label">MCP</div>
      <div class="dim">${h.mcp ? esc(h.mcp.note) : "—"}</div></div>`;
}
function esc(s) { const d = document.createElement("div"); d.textContent = s ?? ""; return d.innerHTML; }
