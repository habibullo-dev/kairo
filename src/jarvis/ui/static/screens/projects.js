// Projects — the workspace switcher + list (Phase 10). Create a project, activate one (which
// starts a fresh conversation, since a chat is bound to one project for life), or archive it.
// Renders no secrets; all state changes go through the enumerated /api/projects mutations.
import { esc, escAttr } from "../ui/dom.js";

export async function render(container, api) {
  const data = await api.get("/api/projects");
  if (!data) {
    container.innerHTML = `<div class="rise"><h1>Projects</h1>
      <div class="sub">Unavailable.</div></div>`;
    return;
  }
  const active = data.active_project_id;
  const rows = (data.projects || [])
    .map((p) => {
      const on = p.id === active;
      const dot = p.color ? `<span class="pj-dot" style="background:${escAttr(p.color)}"></span>` : "";
      return `<tr>
        <td>${dot}<b>${esc(p.name)}</b> <span class="mono dim">${esc(p.slug)}</span>
          ${on ? '<span class="tag ok">active</span>' : ""}
          <div class="dim">${esc(p.description || "")}</div></td>
        <td style="text-align:right">
          ${on ? "" : `<button data-use="${p.id}">Open</button>`}
          <button data-archive="${p.id}" class="ghost">Archive</button></td></tr>`;
    })
    .join("");
  const activeLabel = active == null ? "global scope" : "a project";
  container.innerHTML = `
    <div class="rise"><h1>Projects</h1>
      <div class="sub">Each project owns its chats, memory, tasks and files. Currently working in
        ${activeLabel}. Switching starts a fresh conversation.</div></div>
    <div class="card rise">
      <div class="card-label">New project</div>
      <div class="pj-new">
        <input id="pj-name" placeholder="Project name" maxlength="120">
        <button id="pj-create">Create</button>
      </div>
    </div>
    <div class="card rise">
      <div class="card-label">Your projects</div>
      <table>${rows || '<tr><td class="dim">No projects yet.</td></tr>'}</table>
      ${active == null ? "" : '<div style="margin-top:8px"><button id="pj-global" class="ghost">Return to global scope</button></div>'}
    </div>`;

  container.querySelector("#pj-create")?.addEventListener("click", async () => {
    const name = container.querySelector("#pj-name").value.trim();
    if (!name) return;
    await api.post("/api/projects", { name });
    render(container, api);
  });
  container.querySelector("#pj-global")?.addEventListener("click", async () => {
    await api.post("/api/projects/select", { project_id: null });
    render(container, api);
  });
  for (const btn of container.querySelectorAll("[data-use]")) {
    btn.addEventListener("click", async () => {
      await api.post("/api/projects/select", { project_id: Number(btn.dataset.use) });
      render(container, api);
    });
  }
  for (const btn of container.querySelectorAll("[data-archive]")) {
    btn.addEventListener("click", async () => {
      await api.post(`/api/projects/${btn.dataset.archive}/archive`, {});
      render(container, api);
    });
  }
}
