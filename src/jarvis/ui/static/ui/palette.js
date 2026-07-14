// Command palette v2 (Phase 15.5). Ctrl/Cmd-K. Three sections:
//   1. ACTIONS — New Chat, Switch Project/Model/Mode, Open Active Workspace, Open Graph, Run
//      Workflow. Writes go ONLY to the four reversible UI-state routes (sessions/new, mode, model,
//      projects/select) — never the agent-turn or approval routes, and never a Gate-reaching
//      action (Run Workflow only NAVIGATES to Studio). A deliberate amendment of the Phase-11
//      "GET-only" rule, pinned to an exact allowlist by test_ui_palette.
//   2. GO TO — jump to any screen (hash).
//   3. RESULTS — federated search (/api/search: chats, memory, knowledge, tasks, workflows,
//      digests, and artifacts; project-scoped and quarantine-aware). A chat result RESUMES; an
//      artifact opens its hardened content GET; the rest navigate to their owning screen.
// Every row renders via el()/textContent, so a snippet can never inject markup.
import { el } from "./dom.js";
import { refreshHeader } from "./header.js";
import { pushEscape, setPaletteToggle } from "./keys.js";

const NAV = [
  ["daily", "Daily", "Home & conversation"],
  ["projects", "Projects", "Workspaces"],
  ["artifacts", "Artifacts", "Outputs library"],
  ["studio", "Studio", "Orchestration"],
  ["costs", "Costs", "Spend & budgets"],
  ["settings", "Settings", "Appearance & status"],
  ["gate", "Notifications", "Approvals and background activity"],
  ["hub", "Hub", "Connectors & capabilities"],
  ["trace", "Trace", "Event log"],
  ["lab", "Lab", "Evals"],
  ["meetings", "Meetings", "Capture"],
  ["vault", "Vault", "Files & knowledge"],
  ["tasks", "Tasks", "Scheduled work"],
  ["memory", "Memory", "Long-term memory"],
];

// Federated-search domains are a closed server contract.  Do not use returned titles/snippets as
// routes: they remain display-only text, and the route is selected exclusively from this map.
const DOMAIN_ROUTE = {
  chats: "resume", memories: "memory", knowledge: "vault", tasks: "tasks",
  orchestration: "studio", digests: "daily", artifacts: "artifact",
};
const DOMAIN_LABEL = {
  chats: "Chat", memories: "Memory", knowledge: "Knowledge", tasks: "Task",
  orchestration: "Workflow", digests: "Digest", artifacts: "Artifact",
};

let _api = null;
let _overlay = null;
let _input = null;
let _list = null;
let _items = [];
let _actions = [];
let _navItems = [];
let _sel = 0;
let _escOff = null;
let _seq = 0;
let _timer = null;
let _ctx = { runner: {}, projects: [], models: [], sessions: [] };
let _ctxAuthorityToken = null;
let _actionOperation = null;

function authorityToken(api = _api) {
  return typeof api?.authorityToken === "function" ? api.authorityToken() : null;
}

function authorityIsCurrent(token, api = _api) {
  return token === null || typeof api?.authorityIsCurrent !== "function"
    || api.authorityIsCurrent(token);
}

function guarded(token, run) {
  return () => { if (authorityIsCurrent(token)) run(); };
}

function go(hash) { close(); location.hash = hash; }

// Perform a UI-state write (the allowlist), then close. Authoritative lifecycle frames own
// conversation clearing, so a delayed HTTP callback can never erase a newer chat.
async function act(path, body, { then = null } = {}) {
  const api = _api;
  const token = authorityToken(api);
  const startingContext = api?.state?.context
    ? {
        session_id: api.state.context.session_id,
        project_id: api.state.context.project_id,
        context_revision: api.state.context.context_revision,
      }
    : null;
  const startingWorkspace = typeof api?.workspaceToken === "function" ? api.workspaceToken() : null;
  if (_actionOperation && authorityIsCurrent(_actionOperation.token, _actionOperation.api)) return;
  const operation = { api, token, path, body: body || {}, startingContext, startingWorkspace };
  operation.navigationToken = typeof api?.navigationToken === "function"
    ? api.navigationToken() : null;
  _actionOperation = operation;
  const openRevision = _seq;
  try {
    const res = await api.post(path, body || {});
    if (_actionOperation !== operation) return;
    // A lifecycle frame or replacement workspace may retire this operation while its POST is in
    // flight. Never let the old callback refresh chrome, close a newly opened palette, or navigate
    // the replacement authority. If this write caused the lifecycle change, that frame already
    // owns the rerender; when the frame is missing, the still-current token permits recovery below.
    if (!actionResultOwnsCurrentContext(operation)) return;
    if (openRevision !== _seq && _overlay?.classList.contains("open")) return;
    if (res.ok) {
      // A WebSocket echo is helpful but not the authority for UI state. Refresh the shared cache
      // before the next consumer opens, so palette/header/status agree even if that echo is late.
      await api.runnerStatus({ refresh: true });
      await refreshHeader();
      if (_actionOperation !== operation) return;
      if (!actionResultOwnsCurrentContext(operation)) return;
      if (openRevision !== _seq && _overlay?.classList.contains("open")) return;
      close();
      const navigationCurrent = operation.navigationToken === null
        || typeof api.navigationIsCurrent !== "function"
        || api.navigationIsCurrent(operation.navigationToken);
      if (then && navigationCurrent) then();
    }
  } finally {
    if (_actionOperation === operation) _actionOperation = null;
  }
}

function resumeAndGo(sessionId, hash) {
  const api = _api;
  const navigationToken = typeof api?.navigationToken === "function"
    ? api.navigationToken() : null;
  api.resumeChat(sessionId).then((ok) => {
    const navigationCurrent = navigationToken === null
      || typeof api.navigationIsCurrent !== "function"
      || api.navigationIsCurrent(navigationToken);
    if (ok && navigationCurrent) go(hash);
  });
}

function actionResultOwnsCurrentContext(operation) {
  const { api, token, path, body, startingContext, startingWorkspace } = operation;
  if (typeof api?.workspaceToken === "function" && api.workspaceToken() !== startingWorkspace) {
    return false;
  }
  if (authorityIsCurrent(token, api)) return true;
  const current = api?.state?.context;
  if (!current || !startingContext) return false;
  // Context-changing actions are expected to retire their starting authority when the forced
  // runner read is the first observer. Permit only their exact successor scope; all other writes
  // must remain under the original authority.
  if (path === "/api/sessions/new") {
    return current.project_id === startingContext.project_id
      && current.session_id !== startingContext.session_id
      && current.context_revision === startingContext.context_revision + 1
      && (!Number.isInteger(token) || api.authorityToken() === token + 1);
  }
  if (path === "/api/projects/select") {
    const expectedProject = body.project_id == null ? null : Number(body.project_id);
    return current.project_id === expectedProject
      && current.session_id !== startingContext.session_id
      && current.context_revision === startingContext.context_revision + 1
      && (!Number.isInteger(token) || api.authorityToken() === token + 1);
  }
  return false;
}

function computeActions(q) {
  const ql = q.toLowerCase();
  const out = [];
  const add = (title, snip, run) => out.push({ kind: "action", chip: "Do", title, snip, run });
  const pid = _ctx.runner.project && _ctx.runner.project.id;
  const token = _ctxAuthorityToken;
  add("New Chat", "Start a fresh conversation",
    guarded(token, () => act("/api/sessions/new", {}, { then: () => go("#daily") })));
  if (pid) {
    add("Open Active Workspace", "This project's workspace", guarded(token, () => go(`#workspace/${pid}`)));
    add("Open Graph", "This project's knowledge graph", guarded(token, () => go(`#workspace/${pid}/graph`)));
  }
  add("Run Workflow", "Assemble a team in Studio", guarded(token, () => go("#studio")));
  for (const [v, label] of [["plan", "Planning"], ["approval", "Approval"], ["auto", "Auto"]]) {
    if (_ctx.runner.mode !== v) add(`Switch mode: ${label}`, "Run mode",
      guarded(token, () => act("/api/mode", { mode: v })));
  }
  for (const m of _ctx.models) {
    if (m.selectable && !m.current) {
      add(`Switch model: ${m.label}`, "Interactive model",
        guarded(token, () => act("/api/model", { model: m.id })));
    }
  }
  if (pid) add("Switch to Global", "Chat outside any project",
    guarded(token, () => act("/api/projects/select", { project_id: null }, { then: () => go("#daily") })));
  for (const p of _ctx.projects) {
    if (p.id !== pid) {
      add(`Switch to project: ${p.name || "Project " + p.id}`, "Project scope",
        guarded(token, () => act("/api/projects/select", { project_id: p.id }, { then: () => go("#daily") })));
    }
  }
  return out.filter((a) => !ql || a.title.toLowerCase().includes(ql) || a.snip.toLowerCase().includes(ql));
}

// Recent chats, matched by TITLE (the unified search is word/content-based + project-scoped, so it
// can miss chats — this makes them always findable + jumpable). Selecting one resumes it.
function computeChats(q) {
  const ql = q.toLowerCase();
  const token = _ctxAuthorityToken;
  return (_ctx.sessions || [])
    .filter((s) => !ql || (s.title || "").toLowerCase().includes(ql))
    .slice(0, 8)
    .map((s) => ({
      kind: "chat", chip: "Chat", title: s.title || `Chat ${s.id}`,
      snip: "Resume this conversation",
      run: guarded(token, () => resumeAndGo(s.id, "#chat")),
    }));
}

function computeNav(q) {
  const ql = q.toLowerCase();
  return NAV.filter(([key, label, sub]) =>
    !ql || label.toLowerCase().includes(ql) || key.includes(ql) || sub.toLowerCase().includes(ql),
  ).map(([key, label, sub]) => ({ kind: "nav", chip: "Go", title: label, snip: sub, run: () => go(`#${key}`) }));
}

function resultItems(results) {
  const token = _ctxAuthorityToken;
  return (results || []).filter((r) => r && DOMAIN_ROUTE[r.domain]).map((r) => ({
    kind: "result", chip: DOMAIN_LABEL[r.domain],
    title: typeof r.title === "string" && r.title.trim() ? r.title : "(untitled)",
    snip: typeof r.snippet === "string" ? r.snippet : "", run: guarded(token, () => openResult(r)),
  }));
}

function openResult(r) {
  const dest = DOMAIN_ROUTE[r.domain];
  const refId = Number(r.ref_id);
  if (!Number.isSafeInteger(refId) || refId < 1) return;
  if (dest === "resume") {
    resumeAndGo(refId, "#chat");
    return;
  }
  if (dest === "artifact") {
    window.open(`/api/artifacts/${encodeURIComponent(refId)}/content`, "_blank", "noopener");
    close();
    return;
  }
  if (dest) go(`#${dest}`);
}

function markSel() {
  for (const row of _list.querySelectorAll(".list-row")) {
    row.classList.toggle("sel", Number(row.dataset.i) === _sel);
  }
}

const _CAP = { action: "Actions", chat: "Chats", nav: "Go to", result: "Results" };

function render() {
  _list.textContent = "";
  if (!_items.length) {
    _list.appendChild(el("div", { class: "palette-empty" }, [_input.value.trim() ? "No matches." : "Type to search."]));
    return;
  }
  let lastKind = null;
  _items.forEach((it, i) => {
    if (it.kind !== lastKind) {
      _list.appendChild(el("div", { class: "palette-cap" }, [_CAP[it.kind] || ""]));
      lastKind = it.kind;
    }
    const row = el("div", { class: `list-row${i === _sel ? " sel" : ""}`, dataset: { i: String(i) } }, [
      el("span", { class: "p-chip" }, [it.chip]),
      el("div", { class: "p-body" }, [
        el("div", { class: "p-title" }, [it.title]),
        it.snip ? el("div", { class: "p-snip" }, [it.snip]) : null,
      ]),
      el("span", {}, []),
    ]);
    row.addEventListener("click", () => { _sel = i; activate(); });
    row.addEventListener("mousemove", () => { if (_sel !== i) { _sel = i; markSel(); } });
    _list.appendChild(row);
  });
}

let _chats = [];

function rebuild() {
  const q = _input.value.trim();
  _actions = computeActions(q);
  _chats = computeChats(q);
  _navItems = computeNav(q);
  _items = [..._actions, ..._chats, ..._navItems];
  if (_sel >= _items.length) _sel = 0;
  render();
}

function scheduleSearch() {
  rebuild();  // actions + nav show instantly; results arrive after the debounce
  const q = _input.value.trim();
  if (_timer) clearTimeout(_timer);
  const my = ++_seq;
  if (!q) return;
  const token = authorityToken();
  const api = _api;
  _timer = setTimeout(async () => {
    if (!api || my !== _seq || !authorityIsCurrent(token, api)) return;
    const pid = _ctx.runner.project && _ctx.runner.project.id;
    const scope = pid ? `&project_id=${pid}` : "";
    const data = await api.get(`/api/search?q=${encodeURIComponent(q)}&limit=25${scope}`);
    if (my !== _seq || !authorityIsCurrent(token, api)
        || !_overlay?.classList.contains("open")) return;
    _items = [..._actions, ..._chats, ..._navItems, ...resultItems((data && data.results) || [])];
    if (_sel >= _items.length) _sel = Math.max(0, _items.length - 1);
    render();
  }, 160);
}

function move(d) {
  if (!_items.length) return;
  _sel = (_sel + d + _items.length) % _items.length;
  markSel();
  _list.querySelector(`.list-row[data-i="${_sel}"]`)?.scrollIntoView({ block: "nearest" });
}

function activate() { _items[_sel]?.run(); }

function onInputKey(ev) {
  if (ev.key === "ArrowDown") { ev.preventDefault(); move(1); }
  else if (ev.key === "ArrowUp") { ev.preventDefault(); move(-1); }
  else if (ev.key === "Enter") { ev.preventDefault(); activate(); }
}

function ensureDom() {
  if (_overlay) return;
  _input = el("input", {
    type: "text", placeholder: "Search, or run an action…",
    autocomplete: "off", spellcheck: "false", "aria-label": "Command palette",
  });
  const bar = el("div", { class: "search-input" }, [el("span", { class: "icon" }, ["⌕"]), _input, el("span", { class: "kbd" }, ["Esc"])]);
  _list = el("div", { class: "palette-results" }, []);
  _overlay = el("div", { class: "command-overlay" }, [el("div", { class: "command-palette" }, [bar, _list])]);
  _overlay.addEventListener("click", (ev) => { if (ev.target === _overlay) close(); });
  _input.addEventListener("input", scheduleSearch);
  _input.addEventListener("keydown", onInputKey);
  document.body.appendChild(_overlay);
}

async function open() {
  ensureDom();
  if (_overlay.classList.contains("open")) return;
  _overlay.classList.add("open");
  const openRevision = ++_seq;
  const token = authorityToken();
  const api = _api;
  if (_ctxAuthorityToken !== token) {
    _ctx = { runner: {}, projects: [], models: [], sessions: [] };
  }
  _ctxAuthorityToken = token;
  _input.value = "";
  _sel = 0;
  rebuild();       // instant: current-authority actions + nav; prior authority data is cleared
  _input.focus();
  _escOff = pushEscape(close);
  // Refresh the action context (active project/model/mode + the pickers + recent chats), re-render.
  const [runner, projects, models, sessions] = await Promise.all([
    api.runnerStatus({ refresh: true }), api.get("/api/projects"), api.get("/api/models"),
    api.get("/api/sessions?limit=20"),
  ]);
  if (openRevision !== _seq || !authorityIsCurrent(token, api)
      || !_overlay.classList.contains("open")) return;
  _ctx = {
    runner: runner || {}, projects: (projects && projects.projects) || [],
    models: (models && models.models) || [], sessions: (sessions && sessions.sessions) || [],
  };
  if (_overlay.classList.contains("open")) rebuild();
}

function close() {
  if (!_overlay || !_overlay.classList.contains("open")) return;
  _overlay.classList.remove("open");
  if (_escOff) { _escOff(); _escOff = null; }
  if (_timer) { clearTimeout(_timer); _timer = null; }
  _seq++;
}

function toggle() {
  if (_overlay && _overlay.classList.contains("open")) close();
  else open();
}

export function init(api) {
  _api = api;
  setPaletteToggle(toggle);
}

export function openPalette() { open(); }
export function closePalette() { close(); }
