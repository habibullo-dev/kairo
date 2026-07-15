// Workspace › Chats — this project's conversations, searchable, resumable (Phase 11 T10).
// Read-only list + two existing writes: resume a chat, pin/unpin it.
import { el } from "../../ui/dom.js";
import { emptyState, row, actionButton } from "./_util.js";
import { relTime } from "../../ui/format.js";

function captureAuthority(api) {
  const context = api.state?.context;
  return {
    authority: typeof api.authorityToken === "function" ? api.authorityToken() : null,
    navigation: typeof api.navigationToken === "function" ? api.navigationToken() : null,
    workspace: typeof api.workspaceToken === "function" ? api.workspaceToken() : null,
    context: context ? { ...context } : null,
  };
}

function isExactChatSuccessor(api, before, projectId) {
  const current = api.state?.context;
  if (!before.context || !current) return false;
  if (typeof api.workspaceToken === "function" && api.workspaceToken() !== before.workspace) {
    return false;
  }
  if (Number.isInteger(before.authority) && typeof api.authorityToken === "function"
      && api.authorityToken() !== before.authority + 1) return false;
  if (before.navigation !== null && typeof api.navigationIsCurrent === "function"
      && !api.navigationIsCurrent(before.navigation)) return false;
  return current.project_id === projectId
    && current.session_id !== before.context.session_id
    && current.context_revision === before.context.context_revision + 1;
}

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
  const activeProjectId = api.state.runner && api.state.runner.project && api.state.runner.project.id;
  const newChat = actionButton(
    activeProjectId === ctx.projectId ? "New chat in this project" : "Work in this project",
    async () => {
      const before = captureAuthority(api);
      if (activeProjectId !== ctx.projectId) {
        const selected = await api.post("/api/projects/select", { project_id: ctx.projectId });
        if (!selected.ok) return;
      } else {
        const created = await api.post("/api/sessions/new", {});
        if (!created.ok) return;
      }
      if (typeof api.runnerStatus === "function") await api.runnerStatus({ refresh: true });
      if (!isExactChatSuccessor(api, before, ctx.projectId)) return;
      location.hash = "chat";
    },
    "primary",
  );
  container.append(el("div", { class: "ws-chats-head" }, [newChat]), search);
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
      const active = s.id === api.state.runner?.session_id;
      const acts = el("div", { class: "ws-rowacts" }, [
        actionButton("Resume", async () => {
          // Loads the chat + its transcript into the Daily view (via the shared helper).
          const navigation = typeof api.navigationToken === "function"
            ? api.navigationToken() : null;
          const resumed = await api.resumeChat(s.id);
          const navigationCurrent = navigation === null
            || typeof api.navigationIsCurrent !== "function"
            || api.navigationIsCurrent(navigation);
          if (resumed && navigationCurrent) location.hash = "chat";
        }),
        actionButton(s.pinned ? "Unpin" : "Pin", async () => {
          await api.post(`/api/sessions/${s.id}/pin`, { pinned: !s.pinned });
          loadList(search.value);
        }, "ghost"),
      ]);
      const sub = [relTime(s.updated_at), active ? "active" : "", s.pinned ? "pinned" : ""]
        .filter(Boolean).join(" · ");
      const item = row("💬", s.title, sub, { trailing: acts });
      item.classList.toggle("active-chat", active);
      listRegion.appendChild(item);
    }
  }

  await loadList("");
}
