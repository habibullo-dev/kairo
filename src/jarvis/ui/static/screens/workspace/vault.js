// Workspace Vault tab (Phase 11 T10): the per-project KB review queue — approve/reject the
// sources quarantined for this project, with a capped markdown preview so the call is informed.
import { el } from "../../ui/dom.js";
import { emptyState, chip, row, actionButton, section } from "./_util.js";
import { renderSourceTree } from "../../ui/source-tree.js";
import {
  beginVaultReview, bindVaultReview, pendingVaultReview, settleVaultReview,
} from "../../ui/vault-review.js";

function setReviewBusy(item, busy) {
  for (const control of item.querySelectorAll(".ws-rowacts button")) control.disabled = busy;
  item.setAttribute("aria-busy", busy ? "true" : "false");
}

export async function render(container, api, ctx) {
  container.textContent = "";
  const [data, knowledge] = await Promise.all([
    api.get("/api/vault?project_id=" + encodeURIComponent(ctx.projectId)),
    // The tab is a project workspace, not the ambient chat shelf. The server accepts this value
    // only when it matches the authenticated workspace, so it cannot select another project.
    api.get("/api/chat/knowledge?project_id=" + encodeURIComponent(ctx.projectId)),
  ]);
  if (!data) {
    container.appendChild(emptyState("Unavailable", "Couldn't load this tab — it'll refresh shortly."));
    return;
  }

  // Stats line — values are counts (numbers), coerced to strings for text-safe chips.
  const stats = data.stats || {};
  const chips = Object.entries(stats).map(([k, v]) => chip(k + " " + v));
  container.appendChild(el("div", { class: "ws-stats" }, chips.length ? chips : [chip("empty")]));
  const readiness = data.project_readiness || null;
  if (readiness) {
    container.appendChild(section("Project knowledge readiness", [
      el("div", { class: "ws-stats" }, [
        chip(`${readiness.ready ? "Ready" : "Needs files"}`),
        chip(`Files ${Number(readiness.sources) || 0}`),
        chip(`Sections ${Number(readiness.indexed_chunks) || 0}`),
        chip(`Imports ${Number(readiness.import_links) || 0}`),
      ]),
      el("div", { class: "dim" }, [readiness.detail || "Project knowledge is unavailable."]),
      el("div", { class: "dim" }, [
        "Chat retrieves relevant sections and direct verified dependencies for each question; it does not load the whole project into every prompt.",
      ]),
    ]));
  }

  const reviewFeedback = el("div", {
    class: "dim vault-review-feedback",
    role: "status",
    "aria-live": "polite",
  });
  const reviewSource = async (sourceId, action, item) => {
    const operation = beginVaultReview(api, ctx.projectId, sourceId, action);
    if (!operation) return;
    bindOperation(operation, item);
    const path = action === "approve"
      ? `/api/vault/sources/${sourceId}/approve`
      : `/api/vault/sources/${sourceId}/reject`;
    let result;
    try {
      result = await api.post(path, {});
    } catch {
      result = { ok: false, data: { message: "This source review could not reach Kairo." } };
    }
    await settleVaultReview(operation, result);
  };
  const bindOperation = (operation, item) => bindVaultReview(
    operation,
    ({ pending, result }) => {
      if (pending) {
        setReviewBusy(item, true);
        return true;
      }
      if (!item.isConnected) return false;
      if (result?.ok) {
        void api.refreshRoute();
        return true;
      }
      setReviewBusy(item, false);
      reviewFeedback.textContent = result?.data?.message
        || "This review action could not be completed.";
      return true;
    },
  );
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
  const rows = (data.unreviewed || []).map((s) => {
    const controls = [];
    const item = row("📄", s.title || s.origin, s.preview, {
      trailing: el("div", { class: "ws-rowacts" }, controls),
    });
    controls.push(
      actionButton("Approve", () => reviewSource(s.id, "approve", item)),
      actionButton("Reject", () => reviewSource(s.id, "reject", item), "ghost"),
    );
    item.querySelector(".ws-rowacts")?.append(...controls);
    const operation = pendingVaultReview(api, ctx.projectId, s.id);
    if (operation) bindOperation(operation, item);
    return item;
  });

  container.appendChild(
    section("Awaiting review", rows.length
      ? [...rows, reviewFeedback]
      : [emptyState("Nothing awaiting review", "New sources land here for a quick approve or reject before they can be cited.")])
  );
}
