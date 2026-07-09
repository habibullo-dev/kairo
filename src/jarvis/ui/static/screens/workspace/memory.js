// Workspace › Memory tab (Phase 11 T10; Phase 15 adds review). What Kairo knows about this project
// (project + global live memories; each can be forgotten — a status flip, never a hard delete), plus
// a "Suggested" review queue: QUARANTINED proposals a human must approve before they become durable
// memory. Approve/reject reuse the existing gated review routes; untrusted-evidence proposals are
// flagged so the human sees why they need scrutiny. All text is textContent (el()/_util helpers).
import { el } from "../../ui/dom.js";
import { emptyState, row, section, actionButton } from "./_util.js";
import { relTime } from "../../ui/format.js";

export async function render(container, api, ctx) {
  container.textContent = "";
  const pid = encodeURIComponent(ctx.projectId);
  const data = await api.get("/api/memory?project_id=" + pid);
  if (!data) {
    container.appendChild(emptyState("Unavailable", "Couldn't load this tab — it'll refresh shortly."));
    return;
  }
  const rerender = () => render(container, api, ctx);

  // --- Suggested: the QUARANTINED review queue (approve/reject before anything is durable) ---
  const sugg = await api.get("/api/graph/suggestions?project_id=" + pid);
  const suggestions = (sugg && sugg.suggestions) || [];
  if (suggestions.length) {
    const built = suggestions.map((s) => {
      const untrusted = s.trust_class === "untrusted_external";
      const actions = el("div", { class: "row-actions" }, [
        actionButton("Approve", () =>
          api.post(`/api/graph/suggestions/${s.id}/approve`, {}).then(rerender)),
        actionButton("Reject", () =>
          api.post(`/api/graph/suggestions/${s.id}/reject`, {}).then(rerender), "ghost"),
      ]);
      return row(
        untrusted ? "⚠️" : "💡",
        (untrusted ? "Untrusted — review carefully: " : "") + (s.preview || "(proposal)"),
        `${s.kind} · ${s.trust_class} · ${relTime(s.created_at)}`,
        { trailing: actions },
      );
    });
    container.appendChild(section("Suggested · pending review", built));
  }

  // --- Memory: durable, live memories ---
  const memories = Array.isArray(data) ? data : data.memories || [];
  if (!memories.length) {
    container.appendChild(section("Memory", [
      emptyState("Nothing remembered yet", "Facts Kairo learns about this project will collect here."),
    ]));
    return;
  }
  const rows = memories.map((m) =>
    row("🧠", m.content, m.type + " · " + relTime(m.created_at), {
      trailing: el("div", { class: "row-actions" }, [
        graphLink(ctx.projectId, `memory:${m.id}`),
        actionButton("Forget",
          () => api.post(`/api/memory/${m.id}/forget`, {}).then(rerender), "ghost"),
      ]),
    })
  );
  container.appendChild(section("Memory", rows));
}

// A quiet "View in graph" deep-link: focus the graph tab on this node (graph.js reads its focus
// from localStorage) then navigate. GET/navigate-only — it sets a display preference + a hash,
// never a server write, so the Memory panel stays within its enumerated routes.
function graphLink(projectId, ref) {
  const a = el("a", { class: "plain-button ghost", href: `#workspace/${projectId}/graph` },
    ["In graph"]);
  a.addEventListener("click", () => {
    try {
      localStorage.setItem(`kairo:graph:${projectId}`, JSON.stringify({ focus: ref, kinds: [] }));
    } catch { /* storage disabled — the graph opens unfocused */ }
  });
  return a;
}
