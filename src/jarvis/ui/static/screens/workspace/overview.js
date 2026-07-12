// Project Workspace — Overview tab (Phase 11 T10). Read-only: month spend, recent artifacts
// & runs, and quick-nav actions. All untrusted text lands via el()/_util helpers.
import { el } from "../../ui/dom.js";
import { emptyState, row, actionButton, section, runTone, statusPill } from "./_util.js";
import { money, relTime } from "../../ui/format.js";

const KIND_EMOJI = {
  document: "📄", report: "📑", note: "📝", image: "🖼️", chart: "📊",
  dataset: "🗂️", spreadsheet: "📈", code: "💻", link: "🔗", audio: "🎧",
};
const kindEmoji = (kind) => KIND_EMOJI[kind] || "📦";

function openArtifact(a) {
  if (a.origin_type === "orchestration" && /^\d+$/.test(String(a.origin_id || ""))) {
    location.hash = `studio/${a.origin_id}`;
    return;
  }
  if (a.has_content) {
    window.open("/api/artifacts/" + encodeURIComponent(a.id) + "/content", "_blank", "noopener");
  }
}

export async function render(container, api, ctx) {
  container.textContent = "";
  const data = await api.get(`/api/workspace/${ctx.projectId}`);
  if (!data) {
    container.appendChild(emptyState("Unavailable", "Couldn't load this tab — it'll refresh shortly."));
    return;
  }

  const tile = el("div", { class: "metric cost-metric" }, [
    el("div", { class: "n" }, [money(data.health && data.health.month_spend_usd)]),
    el("div", { class: "l" }, ["spent this month"]),
  ]);

  const arts = data.recent_artifacts || [];
  const artRows = arts.length
    ? arts.map((a) =>
        row(kindEmoji(a.kind), a.title, a.kind + " · " + relTime(a.created_at), {
          onClick: (a.has_content || a.origin_type === "orchestration") ? () => openArtifact(a) : undefined,
        }),
      )
    : [emptyState("No artifacts yet", "Outputs Kairo files for this project appear here.")];

  const runs = data.recent_runs || [];
  const runRows = runs.length
    ? runs.map((r) =>
        row("🧩", r.title || r.workflow, r.status, {
          trailing: statusPill(r.status, runTone(r.status)),
          onClick: () => { location.hash = `studio/${r.id}`; },
        }),
      )
    : [emptyState("No runs yet", "Launch a team from Studio to see orchestration runs here.")];

  const quick = section("Quick actions", [
    el("div", { class: "action-row" }, [
      actionButton("Chats", () => { location.hash = `workspace/${ctx.projectId}/chats`; }),
      actionButton("Artifacts", () => { location.hash = `workspace/${ctx.projectId}/artifacts`; }),
      actionButton("Open Studio", () => { location.hash = "studio"; }, "ghost"),
    ]),
  ]);

  container.appendChild(tile);
  container.appendChild(section("Recent artifacts", artRows));
  container.appendChild(section("Recent runs", runRows));
  container.appendChild(quick);
}
