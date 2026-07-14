// Projects — the workspace grid (Phase 11 T9). Each project is a card: color tile, name, editable
// category label, pinned star, status, and health chips (open tasks · chats this week · last run ·
// month spend). A collections row surfaces built-in navigation presets. The card
// opens the project Workspace (#workspace/{id}, T10). Every write is an existing/enumerated
// metadata mutation (create/select/archive/pin/label) — no new authority. Built entirely with
// el() so a project name/label can never inject markup.
import { el } from "../ui/dom.js";
import { showToast } from "../ui/feedback.js";
import { money } from "../ui/format.js";
import { pushEscape } from "../ui/keys.js";

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

// Metadata edits are attended: this form is populated from the current project card and only
// submits after the person explicitly saves it.  It does not switch project scope, change the
// stable slug, or touch repositories, agents, schedules, or any run state.
let activeProjectEdit = null;
let projectEditSequence = 0;
let activeProjectReset = null;

function captureAuthority(api) {
  const context = api.state?.context;
  return {
    authority: typeof api.authorityToken === "function" ? api.authorityToken() : null,
    navigation: typeof api.navigationToken === "function" ? api.navigationToken() : null,
    workspace: typeof api.workspaceToken === "function" ? api.workspaceToken() : null,
    context: context ? {
      session_id: context.session_id,
      project_id: context.project_id,
      context_revision: context.context_revision,
    } : null,
  };
}

function isExactProjectSuccessor(api, before, projectId) {
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

function closeProjectReset(value, owner = null) {
  if (!activeProjectReset || (owner !== null && activeProjectReset !== owner)) return;
  const current = activeProjectReset;
  activeProjectReset = null;
  current.unregisterEscape();
  current.password.value = "";
  current.overlay.remove();
  current.restoreFocus?.focus?.();
  current.resolve(value);
}

function detachProjectReset(current) {
  if (!current || current.detached) return;
  current.detached = true;
  current.unregisterEscape();
  current.password.value = "";
  current.overlay.remove();
}

function openProjectReset(project, api) {
  if (activeProjectReset?.operationPending) {
    showToast("A project reset is already in progress.", "error");
    return Promise.resolve(false);
  }
  return new Promise((resolve) => {
    if (activeProjectReset) closeProjectReset(false);
    const overlay = el("div", { class: "dialog-overlay", role: "presentation" });
    const card = el("section", {
      class: "dialog-card danger", role: "dialog", "aria-modal": "true",
      "aria-label": `Start ${project.name || "project"} fresh`,
    });
    const confirmation = el("input", {
      class: "dialog-input", type: "text", maxlength: "120", autocomplete: "off",
      "aria-label": "Type the exact project name",
    });
    const password = el("input", {
      class: "dialog-input", type: "password", maxlength: "1024",
      autocomplete: "current-password", "aria-label": "Owner password",
    });
    const retain = el("input", { type: "checkbox", checked: true });
    const error = el("div", { class: "project-edit-error", role: "alert", hidden: true });
    const cancel = el("button", { class: "dialog-button secondary", type: "button", text: "Cancel" });
    const submit = el("button", { class: "dialog-button primary danger", type: "submit", text: "Archive & start fresh" });
    const form = el("form", { class: "project-edit-form" }, [
      el("h2", { class: "dialog-title", text: "Start this project fresh" }),
      el("p", {
        class: "dialog-message",
        text: "Kairo will archive the current workspace and keep its history for audit. The new workspace starts without its chats, memory, tasks, reports, or pending actions.",
      }),
      projectField("Type the exact project name", confirmation, project.name),
      projectField("Owner password", password, "Required again for this destructive action."),
      el("label", { class: "project-edit-field" }, [
        el("span", { class: "project-edit-label" }, [retain, " Keep repository links so Kairo can relearn the project"]),
      ]),
      error,
      el("div", { class: "dialog-actions" }, [cancel, submit]),
    ]);
    card.append(form);
    overlay.append(card);
    const setBusy = (busy) => {
      submit.disabled = busy; cancel.disabled = busy; confirmation.disabled = busy;
      password.disabled = busy; retain.disabled = busy;
    };
    const showError = (message) => {
      error.hidden = false;
      error.textContent = message || "Project reset failed. Stop active work and try again.";
    };
    const closeIfIdle = () => {
      if (activeProjectReset?.operationPending) return;
      closeProjectReset(false);
    };
    cancel.addEventListener("click", closeIfIdle);
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      if (confirmation.value !== project.name) { showError("The project name does not match."); return; }
      if (!password.value) { showError("Enter the owner password."); return; }
      const owner = activeProjectReset;
      const resetRequest = {
        confirmation: confirmation.value,
        retain_repositories: retain.checked,
      };
      owner.operationPending = true;
      setBusy(true); error.hidden = true;
      let steppedUp;
      try {
        steppedUp = await api.stepUp(password.value);
      } catch {
        steppedUp = {
          ok: false, data: { message: "Password verification could not reach Kairo." },
        };
      }
      password.value = "";
      if (activeProjectReset !== owner) return;
      if (!steppedUp.ok) {
        owner.operationPending = false;
        if (owner.detached) {
          closeProjectReset(false, owner);
          showToast(steppedUp.data?.message || "Password verification failed.", "error");
          return;
        }
        setBusy(false); showError(steppedUp.data?.message || "Password verification failed."); return;
      }
      const beforeReset = captureAuthority(api);
      let result;
      try {
        result = await api.post(
          `/api/projects/${encodeURIComponent(project.id)}/reset`, resetRequest,
        );
      } catch {
        result = { ok: false, data: { message: "Project reset could not reach Kairo." } };
      }
      if (activeProjectReset !== owner) return;
      owner.operationPending = false;
      if (!result.ok) {
        if (owner.detached) {
          closeProjectReset(false, owner);
          showToast(result.data?.message || "Project reset failed.", "error");
          return;
        }
        setBusy(false); showError(result.data?.message); return;
      }
      if (typeof api.runnerStatus === "function") {
        await api.runnerStatus({ refresh: true });
      }
      if (!isExactProjectSuccessor(api, beforeReset, result.data?.successor_project_id)) {
        closeProjectReset(false, owner);
        showToast("The reset completed, but the current workspace changed.");
        return;
      }
      closeProjectReset(result.data, owner);
    });
    overlay.addEventListener("click", (event) => { if (event.target === overlay) closeIfIdle(); });
    const restoreFocus = document.activeElement;
    const unregisterEscape = pushEscape(closeIfIdle, card);
    activeProjectReset = {
      overlay, password, resolve, unregisterEscape, restoreFocus,
      operationPending: false, detached: false,
    };
    document.body.append(overlay);
    confirmation.focus();
  });
}

export function dismissProjectDialogs() {
  // Password step-up intentionally rotates the owner workspace, and a successful reset then
  // rotates project/session authority again. Once the user has submitted this destructive,
  // exact-project transaction, detach its stale DOM but let the immutable in-flight request
  // finish. Before submission, an authority change still dismisses it immediately.
  if (activeProjectReset?.operationPending) detachProjectReset(activeProjectReset);
  else closeProjectReset(false);
  closeProjectEdit(false);
}

function closeProjectEdit(value, owner = null) {
  const current = activeProjectEdit;
  if (!current || (owner !== null && current.owner !== owner)) return false;
  activeProjectEdit = null;
  current.unregisterEscape();
  current.overlay.remove();
  current.restoreFocus?.focus?.();
  current.resolve(value);
  return true;
}

function projectField(labelText, control, hint = null) {
  const field = el("label", { class: "project-edit-field" }, [
    el("span", { class: "project-edit-label", text: labelText }), control,
  ]);
  if (hint) field.append(el("span", { class: "project-edit-hint", text: hint }));
  return field;
}

function openProjectEdit(project, api) {
  return new Promise((resolve) => {
    if (activeProjectEdit) {
      // Never let a late response from dialog A close dialog B. A saving form owns the modal
      // until its request settles, so a second card click simply leaves the current review open.
      if (activeProjectEdit.saving) { resolve(false); return; }
      closeProjectEdit(false);
    }
    const owner = ++projectEditSequence;
    const overlay = el("div", { class: "dialog-overlay", role: "presentation" });
    const card = el("section", {
      class: "dialog-card project-edit-dialog", role: "dialog", "aria-modal": "true",
      "aria-label": `Edit project details for ${project.name || "project"}`,
    });
    const name = el("input", {
      class: "dialog-input", type: "text", required: true, maxlength: "120",
      "aria-label": "Project name",
    });
    name.value = String(project.name || "");
    const description = el("textarea", {
      class: "dialog-input project-edit-description", rows: "5", maxlength: "2000",
      "aria-label": "Project description",
    });
    description.value = String(project.description || "");
    const error = el("div", { class: "project-edit-error", role: "alert", hidden: true });
    const cancel = el("button", { class: "dialog-button secondary", type: "button", text: "Cancel" });
    const submit = el("button", { class: "dialog-button primary", type: "submit", text: "Save details" });
    const form = el("form", { class: "project-edit-form" }, [
      el("h2", { class: "dialog-title", text: "Edit project details" }),
      el("p", {
        class: "dialog-message",
        text: "Review the metadata before saving. This does not switch project scope or change project files, agents, schedules, or run settings.",
      }),
      projectField("Project name", name, "The stable project slug stays unchanged."),
      projectField("Description", description, "Optional; shown in the project card and workspace header."),
      error,
      el("div", { class: "dialog-actions" }, [cancel, submit]),
    ]);
    card.append(form);
    overlay.append(card);

    const showError = (message) => {
      error.hidden = false;
      error.textContent = message || "Project details could not be saved. Please try again.";
    };
    const closeIfIdle = () => {
      if (activeProjectEdit?.owner !== owner || activeProjectEdit.saving) return;
      closeProjectEdit(false, owner);
    };
    const setSaving = (saving) => {
      const current = activeProjectEdit;
      if (!current || current.owner !== owner) return false;
      current.saving = saving;
      submit.disabled = saving;
      cancel.disabled = saving;
      return true;
    };
    cancel.addEventListener("click", closeIfIdle);
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      if (activeProjectEdit?.owner !== owner || activeProjectEdit.saving) return;
      const nextName = name.value.trim();
      if (!nextName) {
        showError("A project name is required.");
        name.focus();
        return;
      }
      setSaving(true);
      error.hidden = true;
      let result;
      try {
        result = await api.post(`/api/projects/${encodeURIComponent(project.id)}/update`, {
          name: nextName,
          description: description.value.trim(),
        });
      } catch {
        if (setSaving(false)) showError("Project details could not be saved. Please try again.");
        return;
      }
      if (activeProjectEdit?.owner !== owner) return;
      if (!result.ok || !result.data?.ok) {
        setSaving(false);
        showError(result.data?.message);
        return;
      }
      closeProjectEdit(true, owner);
    });
    overlay.addEventListener("click", (event) => {
      if (event.target === overlay) closeIfIdle();
    });
    const restoreFocus = document.activeElement;
    const unregisterEscape = pushEscape(closeIfIdle, card);
    activeProjectEdit = {
      owner, saving: false, overlay, resolve, unregisterEscape, restoreFocus,
    };
    document.body.append(overlay);
    name.focus();
  });
}

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
      const beforeSelect = captureAuthority(api);
      const selected = await api.post("/api/projects/select", { project_id: p.id });
      if (!selected.ok) { refresh(); return; }
      if (typeof api.runnerStatus === "function") {
        await api.runnerStatus({ refresh: true });
      }
      if (!isExactProjectSuccessor(api, beforeSelect, p.id)) return;
    } else if (api.state?.context?.project_id !== p.id) {
      return;
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
    plainButton("Edit details", async () => {
      const saved = await openProjectEdit(p, api);
      if (!saved) return;
      showToast("Project details saved.");
      refresh();
    }, "ghost"),
    plainButton("Archive", async () => {
      await api.post(`/api/projects/${p.id}/archive`, {});
      refresh();
    }, "ghost"),
    plainButton("Start fresh", async () => {
      const result = await openProjectReset(p, api);
      if (!result) return;
      showToast("Fresh project workspace created.");
      location.hash = `workspace/${result.successor_project_id}`;
    }, "ghost"),
  ]);

  card.append(head, meta, ...(desc ? [desc] : []), health, actions);
  return card;
}

function collectionsRow() {
  const chips = [];
  for (const [key, label] of BUILTIN_VIEWS) {
    const c = el("button", { class: "collection-chip" }, [label]);
    c.addEventListener("click", () => { location.hash = `artifacts/${key}`; });
    chips.push(c);
  }
  return el("div", { class: "surface rise" }, [
    el("div", { class: "panel-title" }, [el("h3", {}, ["Collections"])]),
    el("div", { class: "collections" }, chips),
  ]);
}

export async function render(container, api) {
  const refresh = () => api.refreshRoute();
  const data = await api.getRequired("/api/projects/overview");
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
  container.appendChild(collectionsRow());

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
    container.appendChild(el("div", { class: "rise project-scope-return" }, [back]));
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
      el("div", { class: "daily-rows archived-project-rows" }, rows),
    ]);
    container.appendChild(details);
  }
}
