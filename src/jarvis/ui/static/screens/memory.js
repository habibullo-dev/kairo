// Memory — stored facts/preferences with provenance. Forget flips status (never DELETE).
import { esc, unavailable } from "./vault.js";

export async function render(container, api) {
  const rows = await api.get("/api/memory");
  if (rows === null) { container.innerHTML = unavailable("Memory", "long-term memory off"); return; }
  container.innerHTML = `
    <h1>Memory</h1><div class="sub">What Kairo remembers about you — with where it came from.</div>
    <div class="card"><table id="mem-tbl"><tr><th>Fact</th><th>Type</th><th>Source</th><th></th></tr></table></div>`;
  const tbl = container.querySelector("#mem-tbl");
  if (!rows.length) { tbl.insertAdjacentHTML("beforeend", `<tr><td colspan="4" class="dim">Nothing stored.</td></tr>`); return; }
  for (const m of rows) {
    const tr = document.createElement("tr");
    tr.innerHTML = `<td>${esc(m.content)}</td><td class="dim">${m.type}</td><td class="dim">${esc(m.source)}</td><td></td>`;
    const b = document.createElement("button");
    b.className = "rowbtn"; b.textContent = "Forget";
    b.addEventListener("click", async () => { await api.post(`/api/memory/${m.id}/forget`); render(container, api); });
    tr.lastElementChild.appendChild(b);
    tbl.appendChild(tr);
  }
}
