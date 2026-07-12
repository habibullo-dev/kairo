// Workspace › Tasks tab. Team follow-ups are inert head-synthesis planning items, deliberately
// distinct from scheduled reminders/jobs: model findings never schedule or execute work.
import { emptyState, chip, row, actionButton, section } from "./_util.js";
import { el } from "../../ui/dom.js";
import { openTaskDraft, openTaskHistory } from "../../ui/task-draft.js";

export async function render(container, api, ctx) {
  container.textContent = "";
  const [data, runsData] = await Promise.all([
    api.get("/api/tasks?project_id=" + ctx.projectId),
    api.get("/api/orchestration?project_id=" + ctx.projectId),
  ]);
  if (!data && !runsData) {
    container.appendChild(emptyState("Unavailable", "Couldn't load this tab — it'll refresh shortly."));
    return;
  }
  const tasks = Array.isArray(data) ? data : (data && data.tasks) || [];
  const runs = (runsData && runsData.runs) || [];
  const followUps = runs.flatMap((run) => (Array.isArray(run.action_items) ? run.action_items : [])
    .map((item) => ({ ...item, runId: run.id, runTitle: run.title || run.workflow || "Team run" })));
  const rerender = () => render(container, api, ctx);
  const rows = tasks.map((t) => {
    const when = t.next_run_at ? t.next_run_at.slice(0, 16).replace("T", " ") : "";
    const sub = t.kind + " · " + t.status + (when ? " · " + when : "");
    const trailing = el("div", { class: "action-row" }, [
      actionButton("History", () => { void openTaskHistory(t, api); }, "ghost"),
      t.status === "active"
        ? actionButton("Cancel", () => api.post("/api/tasks/" + t.id + "/cancel", {}).then(rerender), "ghost")
        : chip(t.status),
    ]);
    return row("⏰", t.title, sub, { trailing });
  });
  const followRows = followUps.map((item) => row("→", item.title, `${item.runTitle} · ${item.goal}`, {
    trailing: el("div", { class: "action-row" }, [
      chip(item.priority || "medium"),
      actionButton("Review & schedule", async () => {
        if (await openTaskDraft(item, api)) rerender();
      }, "ghost"),
    ]),
    onClick: () => { location.hash = `studio/${item.runId}`; },
  }));
  container.appendChild(section("Team follow-ups", followRows.length ? followRows : [
    emptyState("No follow-ups yet", "Completed team reviews will place their recommended next steps here. They never schedule work automatically."),
  ]));
  container.appendChild(section("Scheduled tasks", rows.length ? rows : [
    emptyState("No tasks scheduled", "Reminders and jobs you confirm for this project will appear here."),
  ]));
}
