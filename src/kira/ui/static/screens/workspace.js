// Project Workspace (Phase 11 T10) — the per-project home. Route: #workspace/{id}/{tab}. This
// shell fetches the shared project context once, renders the header + tab bar, and lazy-loads the
// active tab's panel (screens/workspace/{tab}.js) with a per-tab error boundary so one panel can
// never take down the whole workspace. The tab name is validated against a fixed allowlist before
// the dynamic import (the hash is attacker-influenceable). Panels read project-scoped data and
// navigate; every write goes through the existing enumerated routes. No new authority.
import { el } from "../ui/dom.js";

const TABS = [
  ["overview", "Overview"], ["chats", "Chats"], ["artifacts", "Artifacts"],
  ["memory", "Memory"], ["tasks", "Tasks"], ["vault", "Vault"],
  ["studio", "Studio"], ["office", "Office"], ["graph", "Graph"],
  ["costs", "Costs"], ["activity", "Activity"],
];
const TAB_KEYS = TABS.map(([k]) => k);

export async function render(container, api, args) {
  const projectId = Number(args && args[0]);
  const tab = TAB_KEYS.includes(args && args[1]) ? args[1] : "overview";
  container.textContent = "";

  if (!Number.isInteger(projectId) || projectId <= 0) {
    container.appendChild(el("div", { class: "empty-state rise" }, [
      el("h4", {}, ["No project selected"]),
      el("div", {}, ["Open a project from the Projects grid to see its workspace."]),
    ]));
    return;
  }

  const ctx = await api.getRequired(`/api/workspace/${projectId}`);
  const project = (ctx && ctx.project) || null;
  if (!project) {
    const currentProjectId = api.state.context?.project_id;
    const actions = [el("a", { href: "#projects", class: "plain-button" }, ["Open Projects"])];
    if (Number.isInteger(currentProjectId) && currentProjectId > 0) {
      actions.push(el("a", {
        href: `#workspace/${currentProjectId}`, class: "plain-button ghost",
      }, ["Open current workspace"]));
    }
    container.appendChild(el("div", { class: "empty-state rise" }, [
      el("h4", {}, ["Workspace unavailable"]),
      el("div", {}, ["This project is not the current workspace, or it is no longer available."]),
      el("div", { class: "chip-row" }, actions),
    ]));
    return;
  }

  const tile = el("div", { class: "card-tile ws-tile" }, [
    project ? (project.icon || (project.name || "?").slice(0, 1).toUpperCase()) : "?",
  ]);
  if (project && project.color) {
    tile.style.setProperty("--p1", project.color);
    tile.style.setProperty("--p2", project.color);
  }
  const header = el("div", { class: "ws-header rise" }, [
    tile,
    el("div", { class: "ws-head-text" }, [
      el("h1", {}, [project ? project.name : `Project ${projectId}`]),
      el("div", { class: "sub" }, [
        project && project.description ? project.description : "Project workspace",
      ]),
    ]),
    el("a", { href: "#projects", class: "plain-button ghost" }, ["All projects"]),
  ]);

  const tabbar = el("div", { class: "tabbar ws-tabs rise" }, TABS.map(([key, label]) => {
    const b = el("button", { class: "tab-button" + (key === tab ? " active" : "") }, [label]);
    b.addEventListener("click", () => { location.hash = `workspace/${projectId}/${key}`; });
    return b;
  }));

  const panel = el("div", { class: "ws-panel rise" }, [el("div", { class: "dim" }, ["Loading…"])]);
  container.append(header, tabbar, panel);

  try {
    const mod = await import(`./workspace/${tab}.js`);
    if (!api.renderIsCurrent()) return;
    panel.textContent = "";
    await mod.render(panel, api, { projectId, project });
  } catch {
    if (!api.renderIsCurrent()) return;
    panel.textContent = "";
    panel.appendChild(el("div", { class: "empty-state" }, [
      el("h4", {}, ["Panel unavailable"]),
      el("div", {}, ["This tab couldn't load — try another, or refresh."]),
    ]));
  }
}
