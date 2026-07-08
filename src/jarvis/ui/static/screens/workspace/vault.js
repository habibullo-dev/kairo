// Workspace Vault tab (Phase 11 T10): the per-project KB review queue — approve/reject the
// sources quarantined for this project, with a capped markdown preview so the call is informed.
import { el } from "../../ui/dom.js";
import { emptyState, chip, row, actionButton, section } from "./_util.js";

export async function render(container, api, ctx) {
  container.textContent = "";
  const data = await api.get("/api/vault?project_id=" + encodeURIComponent(ctx.projectId));
  if (!data) {
    container.appendChild(emptyState("Unavailable", "Couldn't load this tab — it'll refresh shortly."));
    return;
  }

  // Stats line — values are counts (numbers), coerced to strings for text-safe chips.
  const stats = data.stats || {};
  const chips = Object.entries(stats).map(([k, v]) => chip(k + " " + v));
  container.appendChild(el("div", { class: "ws-stats" }, chips.length ? chips : [chip("empty")]));

  const rerender = () => render(container, api, ctx);
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
