// Workspace › Chats — this project's conversations, searchable, resumable (Phase 11 T10).
// Read-only list + two existing writes: resume a chat, pin/unpin it.
import { el } from "../../ui/dom.js";
import { emptyState, row, actionButton } from "./_util.js";
import { relTime } from "../../ui/format.js";

export async function render(container, api, ctx) {
  container.textContent = "";
  const base = "/api/sessions?project_id=" + ctx.projectId;

  const listRegion = el("div", { class: "ws-list" }, []);
  const search = el("input", {
    class: "ws-search",
    type: "search",
    placeholder: "Search chats…",
    "aria-label": "Search chats",
    oninput: (ev) => loadList(ev.target.value),
  });
  container.appendChild(search);
  container.appendChild(listRegion);

  let seq = 0;
  async function loadList(query) {
    const url = query ? base + "&query=" + encodeURIComponent(query) : base;
    const my = ++seq;
    const data = await api.get(url);
    if (my !== seq) return; // a newer keystroke superseded this fetch — drop the stale result
    listRegion.textContent = "";
    if (!data) {
      listRegion.appendChild(emptyState("Unavailable", "Couldn't load this tab — it'll refresh shortly."));
      return;
    }
    const sessions = data.sessions || [];
    if (!sessions.length) {
      listRegion.appendChild(
        query
          ? emptyState("No matches", "No chats match that search in this project.")
          : emptyState("No chats yet", "Resume a conversation from the Daily tab to start one here.")
      );
      return;
    }
    for (const s of sessions) {
      const acts = el("div", { class: "ws-rowacts" }, [
        actionButton("Resume", async () => {
          // Loads the chat + its transcript into the Daily view (via the shared helper).
          if (await api.resumeChat(s.id)) location.hash = "daily";
        }),
        actionButton(s.pinned ? "Unpin" : "Pin", async () => {
          await api.post(`/api/sessions/${s.id}/pin`, { pinned: !s.pinned });
          loadList(search.value);
        }, "ghost"),
      ]);
      const sub = relTime(s.updated_at) + (s.pinned ? " · pinned" : "");
      listRegion.appendChild(row("💬", s.title, sub, { trailing: acts }));
    }
  }

  await loadList("");
}
