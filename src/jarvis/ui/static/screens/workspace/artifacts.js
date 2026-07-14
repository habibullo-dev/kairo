// Artifacts tab for the Project Workspace (Phase 11 T10): the project's artifact library,
// pinned-first, opening only the same-origin /content route (never an external_uri).
import { emptyState, row, actionButton, section } from "./_util.js";
import { relTime } from "../../ui/format.js";

const KIND_ICON = {
  document: "📄", doc: "📄", report: "📄", note: "📝", markdown: "📝",
  image: "🖼️", chart: "📊", dataset: "🗂️", data: "🗂️", table: "🧮",
  code: "💻", link: "🔗", web: "🌐", email: "✉️", pdf: "📕", slide: "📽️",
};

function iconFor(kind) {
  return KIND_ICON[kind] || "📎";
}

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
  const data = await api.get(`/api/artifacts?project_id=${ctx.projectId}`);
  if (!data) {
    container.appendChild(emptyState("Unavailable", "Couldn't load this tab — it'll refresh shortly."));
    return;
  }

  const artifacts = data.artifacts || [];
  if (!artifacts.length) {
    container.appendChild(
      emptyState("No artifacts yet", "Documents, charts, and files a run produces will collect here.")
    );
    return;
  }

  const rows = artifacts.map((a) => {
    const sub = a.kind + " · " + relTime(a.created_at) + (a.pinned ? " · pinned" : "");
    const onClick = (a.has_content || a.origin_type === "orchestration")
      ? () => openArtifact(a) : undefined;
    const trailing = actionButton(a.pinned ? "Unpin" : "Pin", async () => {
      await api.post(`/api/artifacts/${a.id}/pin`, { pinned: !a.pinned });
      await api.refreshRoute();
    }, "ghost");
    return row(iconFor(a.kind), a.title, sub, { onClick, trailing });
  });

  container.appendChild(section("Artifacts", rows));
}
