// Workspace › Tasks tab (Phase 11 T10). Scheduled reminders and jobs for this project —
// each row shows kind/status/next-run; active tasks can be cancelled in place.
import { emptyState, chip, row, actionButton, section } from "./_util.js";

export async function render(container, api, ctx) {
  container.textContent = "";
  const data = await api.get("/api/tasks?project_id=" + ctx.projectId);
  if (!data) {
    container.appendChild(emptyState("Unavailable", "Couldn't load this tab — it'll refresh shortly."));
    return;
  }
  const tasks = Array.isArray(data) ? data : data.tasks || [];
  if (!tasks.length) {
    container.appendChild(
      section("Tasks", [
        emptyState("No tasks scheduled", "Reminders and jobs you schedule for this project will appear here."),
      ])
    );
    return;
  }
  const rerender = () => render(container, api, ctx);
  const rows = tasks.map((t) => {
    const when = t.next_run_at ? t.next_run_at.slice(0, 16).replace("T", " ") : "";
    const sub = t.kind + " · " + t.status + (when ? " · " + when : "");
    const trailing =
      t.status === "active"
        ? actionButton("Cancel", () => api.post("/api/tasks/" + t.id + "/cancel", {}).then(rerender), "ghost")
        : chip(t.status);
    return row("⏰", t.title, sub, { trailing });
  });
  container.appendChild(section("Tasks", rows));
}
