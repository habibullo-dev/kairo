// Vault — KB stats + the unreviewed review queue (kb review) + lint. Amber tags flag
// anything awaiting review (quarantined untrusted content).

export async function render(container, api) {
  const data = await api.get("/api/vault");
  if (!data) { container.innerHTML = unavailable("Vault", "knowledge base off"); return; }
  const stats = data.stats || {};
  const statLine = Object.entries(stats).map(([k, v]) => `${k}: ${v}`).join(" · ");
  container.innerHTML = `
    <h1>Vault</h1>
    <div class="sub">Knowledge base — searchable, cited, provenance-tracked.</div>
    <div class="card"><div class="mono dim">${statLine || "empty"}</div></div>
    <div class="card">
      <div class="label" style="color:var(--amber)">Review queue (unreviewed)</div>
      <div id="vault-queue"></div>
    </div>
    <div class="card"><button class="rowbtn" id="vault-lint">Run lint</button> <span id="vault-lint-out" class="dim mono"></span></div>`;

  const q = container.querySelector("#vault-queue");
  const items = data.unreviewed || [];
  if (!items.length) { q.innerHTML = `<div class="dim">Nothing to review.</div>`; }
  else {
    const table = document.createElement("table");
    table.innerHTML = `<tr><th>Source</th><th>Origin</th><th></th></tr>`;
    for (const s of items) {
      const tr = document.createElement("tr");
      tr.innerHTML = `<td>${esc(s.title || "(untitled)")} <span class="tag amber">${s.review_status}</span></td>
        <td class="mono dim">${esc(s.origin)}</td><td></td>`;
      const td = tr.lastElementChild;
      td.append(btn("Approve", async () => { await api.post(`/api/vault/sources/${s.id}/approve`); render(container, api); }));
      td.append(btn("Reject", async () => { await api.post(`/api/vault/sources/${s.id}/reject`); render(container, api); }));
      table.appendChild(tr);
    }
    q.appendChild(table);
  }
  container.querySelector("#vault-lint").addEventListener("click", async () => {
    const r = await api.get("/api/vault/lint");
    container.querySelector("#vault-lint-out").textContent = r ? JSON.stringify(r) : "—";
  });
}

function btn(label, fn) {
  const b = document.createElement("button");
  b.className = "rowbtn"; b.textContent = label; b.style.marginLeft = "6px";
  b.addEventListener("click", fn); return b;
}
function esc(s) { const d = document.createElement("div"); d.textContent = s ?? ""; return d.innerHTML; }
function unavailable(name, why) { return `<h1>${name}</h1><div class="sub">Unavailable — ${why}.</div>`; }
export { esc, unavailable };
