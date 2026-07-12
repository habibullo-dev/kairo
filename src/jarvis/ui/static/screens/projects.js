// Projects — the workspace grid (Phase 11 T9). Each project is a card: color tile, name, editable
// category label, pinned star, status, and health chips (open tasks · chats this week · last run ·
// month spend). A collections row surfaces built-in smart collections + user saved views. The card
// opens the project Workspace (#workspace/{id}, T10). Every write is an existing/enumerated
// metadata mutation (create/select/archive/pin/label) — no new authority. Built entirely with
// el() so a project name/label can never inject markup.
import { el } from "../ui/dom.js";
import { money } from "../ui/format.js";

const LABELS = ["Coding", "Creativity", "Business", "Personal", "Learning", "Finance"];

// Built-in smart collections (artifact-centric). Selecting one opens the Artifacts Library
// filtered (T11 consumes the hash arg); until then it lands on the safe unknown-route state.
const BUILTIN_VIEWS = [
  ["recent-artifacts", "Recent artifacts"],
  ["needs-review", "Needs review"],
  ["generated-week", "Generated this week"],
  ["by-team-model", "By team / model"],
  ["pinned-work", "Pinned project work"],
];

function runTone(status) {
  if (status === "ok") return "good";
  if (status === "running" || status === "revise") return "busy";
  if (["error", "rejected", "aborted", "budget_stopped", "cancelled"].includes(status)) return "danger";
  return "";
}

function chip(text, cls) {
  return el("span", { class: "p-chip" + (cls ? " " + cls : "") }, [text]);
}

function plainButton(label, onClick, variant) {
  const b = el("button", { class: "plain-button" + (variant ? " " + variant : "") }, [label]);
  b.addEventListener("click", (ev) => { ev.stopPropagation(); onClick(); });
  return b;
}

function healthChips(h) {
  const out = [chip(`${h.open_tasks ?? 0} tasks`), chip(`${h.sessions_week ?? 0} chats/wk`)];
  out.push(h.last_run ? chip(h.last_run.verdict || h.last_run.status, runTone(h.last_run.status)) : chip("no runs"));
  out.push(chip(`${money(h.month_spend_usd)}/mo`, "cost-tone"));
  return out;
}

function pinStar(p, api, refresh) {
  const b = el(
    "button",
    { class: "pin-star" + (p.pinned ? " on" : ""), title: p.pinned ? "Unpin" : "Pin", "aria-label": "Pin project" },
    [p.pinned ? "★" : "☆"],
  );
  b.addEventListener("click", async (ev) => {
    ev.stopPropagation();
    await api.post(`/api/projects/${p.id}/pin`, { pinned: !p.pinned });
    refresh();
  });
  return b;
}

function labelSelect(p, api, refresh) {
  const sel = el("select", { class: "label-select", "aria-label": "Project label" });
  const values = ["", ...LABELS];
  if (p.label && !LABELS.includes(p.label)) values.push(p.label); // preserve a custom label
  for (const v of values) {
    const o = el("option", { value: v }, [v || "No label"]);
    if (v === (p.label || "")) o.selected = true;
    sel.appendChild(o);
  }
  sel.addEventListener("click", (ev) => ev.stopPropagation()); // don't open the workspace
  sel.addEventListener("change", async () => {
    await api.post(`/api/projects/${p.id}/label`, { label: sel.value || null });
    refresh();
  });
  return sel;
}

function projectCard(p, activeId, api, refresh) {
  const active = p.id === activeId;
  const card = el("div", { class: "project-card" + (p.pinned ? " pinned" : "") });

  // A Workspace is not merely a dashboard: its Graph, Vault, and Chat data are deliberately
  // read through the live project workspace. Navigate there only after the existing project
  // selection transition succeeds, otherwise an inactive project's scoped panels would correctly
  // refuse to show its private data and look empty. This adds no route or authority.
  const openWorkspace = async () => {
    if (!active) {
      const selected = await api.post("/api/projects/select", { project_id: p.id });
      if (!selected.ok) { refresh(); return; }
    }
    location.hash = `workspace/${p.id}`;
  };

  const tile = el("div", { class: "card-tile" }, [p.icon || (p.name || "?").slice(0, 1).toUpperCase()]);
  if (p.color) { tile.style.setProperty("--p1", p.color); tile.style.setProperty("--p2", p.color); }
  const head = el("div", { class: "pc-head" }, [
    tile,
    el("div", { class: "pc-title" }, [
      el("div", { class: "pc-name" }, [p.name]),
      el("div", { class: "pc-slug mono dim" }, [p.slug]),
    ]),
    pinStar(p, api, refresh),
  ]);
  head.style.cursor = "pointer";
  head.addEventListener("click", () => { void openWorkspace(); });

  const meta = el("div", { class: "pc-meta" }, [
    labelSelect(p, api, refresh),
    active ? el("span", { class: "status-pill good" }, ["active"]) : chip(p.status),
  ]);
  const desc = p.description
    ? el("div", { class: "pc-desc dim" }, [p.description])
    : null;
  const health = el("div", { class: "pc-health" }, healthChips(p.health || {}));
  const actions = el("div", { class: "pc-actions" }, [
    active ? el("span", { class: "dim" }, ["Working here"]) : plainButton("Set active", async () => {
      await api.post("/api/projects/select", { project_id: p.id });
      refresh();
    }),
    plainButton(active ? "Open →" : "Open & switch", () => { void openWorkspace(); }, "ghost"),
    plainButton("Archive", async () => {
      await api.post(`/api/projects/${p.id}/archive`, {});
      refresh();
    }, "ghost"),
  ]);

  card.append(head, meta, ...(desc ? [desc] : []), health, actions);
  return card;
}

async function collectionsRow(container, api) {
  const views = await api.get("/api/views");
  const chips = [];
  for (const [key, label] of BUILTIN_VIEWS) {
    const c = el("button", { class: "collection-chip" }, [label]);
    c.addEventListener("click", () => { location.hash = `artifacts/${key}`; });
    chips.push(c);
  }
  for (const v of (views && views.views) || []) {
    const c = el("button", { class: "collection-chip user" }, [v.name || "View"]);
    c.addEventListener("click", () => { location.hash = `artifacts/view-${v.id}`; });
    chips.push(c);
  }
  return el("div", { class: "surface rise" }, [
    el("div", { class: "panel-title" }, [el("h3", {}, ["Collections"])]),
    el("div", { class: "collections" }, chips),
  ]);
}

export async function render(container, api) {
  const refresh = () => render(container, api);
  const data = await api.get("/api/projects/overview");
  container.textContent = "";

  const head = el("div", { class: "rise" }, [
    el("h1", {}, ["Projects"]),
    el("div", { class: "sub" }, [
      "Each project owns its chats, memory, tasks and files. Open one to work in its Workspace; " +
        "switching starts a fresh conversation.",
    ]),
  ]);
  container.appendChild(head);

  if (!data) {
    container.appendChild(el("div", { class: "empty-state rise" }, [
      el("h4", {}, ["Projects unavailable"]),
      el("div", {}, ["Couldn't load your projects — it'll refresh shortly."]),
    ]));
    return;
  }

  // New project
  const nameInput = el("input", { id: "pj-name", placeholder: "Project name", maxlength: "120" });
  const createBtn = plainButton("Create", async () => {
    const name = nameInput.value.trim();
    if (!name) return;
    await api.post("/api/projects", { name });
    refresh();
  }, "primary");
  container.appendChild(el("div", { class: "surface rise" }, [
    el("div", { class: "panel-title" }, [el("h3", {}, ["New project"])]),
    el("div", { class: "pj-new" }, [nameInput, createBtn]),
  ]));

  // Collections row
  container.appendChild(await collectionsRow(container, api));

  const active = data.projects || [];
  if (!active.length) {
    container.appendChild(el("div", { class: "empty-state rise" }, [
      el("h4", {}, ["No projects yet"]),
      el("div", {}, ["Create your first project above to give your work a home."]),
    ]));
  } else {
    const grid = el("div", { class: "projects-grid rise" }, active.map((p) =>
      projectCard(p, data.active_project_id, api, refresh)));
    container.appendChild(grid);
  }

  // Global scope + archived
  if (data.active_project_id != null) {
    const back = plainButton("Return to global scope", async () => {
      await api.post("/api/projects/select", { project_id: null });
      refresh();
    }, "ghost");
    container.appendChild(el("div", { class: "rise", style: "margin-top:12px" }, [back]));
  }

  const archived = data.archived || [];
  if (archived.length) {
    const rows = archived.map((p) => el("div", { class: "list-row" }, [
      el("span", { class: "list-icon" }, [(p.name || "?").slice(0, 1).toUpperCase()]),
      el("div", { class: "pc-title" }, [
        el("div", { class: "lr-t" }, [p.name]),
        el("div", { class: "lr-s" }, [p.slug]),
      ]),
      el("span", {}, []),
    ]));
    const details = el("details", { class: "surface rise archived" }, [
      el("summary", {}, [`Archived (${archived.length})`]),
      el("div", { class: "daily-rows", style: "margin-top:10px" }, rows),
    ]);
    container.appendChild(details);
  }
}
