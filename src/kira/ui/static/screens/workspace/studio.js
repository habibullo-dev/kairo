// Studio panel: recent orchestration runs for this project, with a shortcut to
// launch a new run on the Studio screen. Read-only; navigation only.
import { el } from "../../ui/dom.js";
import { emptyState, chip, row, actionButton, section, runTone, statusPill } from "./_util.js";
import { money } from "../../ui/format.js";

export async function render(container, api, ctx) {
  container.textContent = "";
  const data = await api.get("/api/orchestration?project_id=" + encodeURIComponent(ctx.projectId));
  if (!data) {
    container.append(emptyState("Unavailable", "Couldn't load this tab — it'll refresh shortly."));
    return;
  }

  const runs = data.runs || [];
  const launch = actionButton("Launch in Studio", () => { location.hash = "studio"; });

  if (!runs.length) {
    container.append(
      section("Runs", [emptyState("No runs yet", "Launch a team workflow in Studio to see it here.")], launch)
    );
    return;
  }

  const rows = runs.map((run) =>
    row(
      "🧩",
      run.title || run.workflow,
      (run.team ? run.team + " · " : "") + run.workflow,
      {
        trailing: el("div", { class: "ws-rowacts" }, [
          statusPill(run.status, runTone(run.status)),
          chip(money(run.actual_cost_usd != null ? run.actual_cost_usd : run.estimated_cost_usd)),
        ]),
        onClick: () => { location.hash = "studio"; },
      }
    )
  );

  container.append(section("Runs", rows, launch));
}
