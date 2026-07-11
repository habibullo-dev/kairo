// Workspace Vault tab (Phase 11 T10): the per-project KB review queue — approve/reject the
// sources quarantined for this project, with a capped markdown preview so the call is informed.
import { el } from "../../ui/dom.js";
import { emptyState, chip, row, actionButton, section } from "./_util.js";
import { renderSourceTree } from "../../ui/source-tree.js";

export async function render(container, api, ctx) {
  container.textContent = "";
  const [data, knowledge] = await Promise.all([
    api.get("/api/vault?project_id=" + encodeURIComponent(ctx.projectId)),
    api.get("/api/chat/knowledge"),
  ]);
  if (!data) {
    container.appendChild(emptyState("Unavailable", "Couldn't load this tab — it'll refresh shortly."));
    return;
  }

  // Stats line — values are counts (numbers), coerced to strings for text-safe chips.
  const stats = data.stats || {};
  const chips = Object.entries(stats).map(([k, v]) => chip(k + " " + v));
  container.appendChild(el("div", { class: "ws-stats" }, chips.length ? chips : [chip("empty")]));

  const rerender = () => render(container, api, ctx);
  const projectSources = knowledge && knowledge.project_id === ctx.projectId
    ? (knowledge.sources || []) : [];
  const tree = projectSources.length
    ? renderSourceTree(projectSources)
    : emptyState("No project files yet", "Use Chat’s + button to add files or an entire project folder.");
  const graph = knowledge && knowledge.graph ? knowledge.graph : {};
  const explorerActions = el("div", { class: "action-row" }, [
    actionButton("Open knowledge graph", () => { location.hash = `workspace/${ctx.projectId}/graph`; }),
  ]);
  const explorerNote = el("div", { class: "dim" }, [
    "Folders are derived from the relative names you selected. They are local project structure, not model-guessed dependencies.",
  ]);
  container.appendChild(section(
    `Project files · ${Number(knowledge && knowledge.source_count) || 0}`,
    [tree, explorerActions, explorerNote, knowledge && knowledge.sources_truncated
      ? el("div", { class: "dim" }, ["Showing the first 300 sources."]) : null].filter(Boolean),
  ));
  if (graph.available) {
    container.appendChild(el("div", { class: "ws-stats" }, [
      chip(`Graph nodes ${(graph.nodes || []).length}`), chip(`Graph links ${Number(graph.edge_count) || 0}`),
    ]));
  }
  const rows = (data.unreviewed || []).map((s) =>
    row("📄", s.title || s.origin, s.preview, {
      trailing: el("div", { class: "ws-rowacts" }, [
        actionButton("Approve", () => api.post(`/api/vault/sources/${s.id}/approve`, {}).then(rerender)),
        actionButton("Reject", () => api.post(`/api/vault/sources/${s.id}/reject`, {}).then(rerender), "ghost"),
      ]),
    })
  );

  container.appendChild(
    section("Awaiting review", rows.length ? rows : [emptyState("Nothing awaiting review", "New sources land here for a quick approve or reject before they can be cited.")])
  );
}
