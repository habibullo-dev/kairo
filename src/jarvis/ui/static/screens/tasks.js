// Tasks — reminders & jobs with next-fire times. Cancel is the only mutation (creation stays
// in chat via the gated schedule_task — one creation path, one approval).
import { esc, unavailable } from "./vault.js";

export async function render(container, api) {
  const rows = await api.get("/api/tasks");
  if (rows === null) { container.innerHTML = unavailable("Tasks", "scheduler off"); return; }
  container.innerHTML = `
    <div class="rise"><h1>Tasks</h1><div class="sub">Reminders and unattended jobs.</div></div>
    <div class="card rise"><table id="tasks-tbl">
      <tr><th>Title</th><th>Kind</th><th>Next</th><th>Status</th><th></th></tr></table></div>`;
  const tbl = container.querySelector("#tasks-tbl");
  if (!rows.length) {
    tbl.insertAdjacentHTML("beforeend", `<tr><td colspan="5" class="dim">No tasks scheduled.</td></tr>`);
    return;
  }
  for (const t of rows) {
    const tr = document.createElement("tr");
    const active = t.status === "active";
    tr.innerHTML = `<td>${esc(t.title)}</td><td class="dim">${esc(t.kind)}</td>
      <td class="mono dim">${esc(t.next_run_at || "—")}</td>
      <td><span class="tag ${active ? "ok" : ""}">${esc(t.status)}</span></td>
      <td style="text-align:right"></td>`;
    if (active) {
      const b = document.createElement("button");
      b.className = "rowbtn";
      b.textContent = "Cancel";
      b.addEventListener("click", async () => { await api.post(`/api/tasks/${t.id}/cancel`); render(container, api); });
      tr.lastElementChild.appendChild(b);
    }
    tbl.appendChild(tr);
  }
}
