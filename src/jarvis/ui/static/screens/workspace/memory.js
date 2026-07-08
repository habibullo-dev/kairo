// Workspace › Memory tab (Phase 11 T10). What Kairo knows about this project (project + global
// live memories); each can be forgotten (a status flip, never a hard delete).
import { emptyState, row, section, actionButton } from "./_util.js";
import { relTime } from "../../ui/format.js";

export async function render(container, api, ctx) {
  container.textContent = "";
  const data = await api.get("/api/memory?project_id=" + encodeURIComponent(ctx.projectId));
  if (!data) {
    container.appendChild(emptyState("Unavailable", "Couldn't load this tab — it'll refresh shortly."));
    return;
  }
  const memories = Array.isArray(data) ? data : data.memories || [];
  if (!memories.length) {
    container.appendChild(
      section("Memory", [emptyState("Nothing remembered yet", "Facts Kairo learns about this project will collect here.")])
    );
    return;
  }
  const rows = memories.map((m) =>
    row("🧠", m.content, m.type + " · " + relTime(m.created_at), {
      trailing: actionButton("Forget", () => api.post(`/api/memory/${m.id}/forget`, {}).then(() => render(container, api, ctx)), "ghost"),
    })
  );
  container.appendChild(section("Memory", rows));
}
