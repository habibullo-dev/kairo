// Command palette v2 (Phase 15.5). Ctrl/Cmd-K. Three sections:
//   1. ACTIONS — New Chat, Switch Project/Model/Mode, Open Active Workspace, Open Graph, Run
//      Workflow. Writes go ONLY to the four reversible UI-state routes (sessions/new, mode, model,
//      projects/select) — never the agent-turn or approval routes, and never a Gate-reaching
//      action (Run Workflow only NAVIGATES to Studio). A deliberate amendment of the Phase-11
//      "GET-only" rule, pinned to an exact allowlist by test_ui_palette.
//   2. GO TO — jump to any screen (hash).
//   3. RESULTS — unified search (/api/graph/search: chats/artifacts/memory/vault/tasks/runs +
//      graph entities, quarantine-aware). A chat result RESUMES; an entity opens the focused graph
//      tab; an artifact opens its hardened content GET; the rest navigate.
// Every row renders via el()/textContent, so a snippet can never inject markup.
import { el } from "./dom.js";
import { pushEscape, setPaletteToggle } from "./keys.js";

const NAV = [
  ["daily", "Daily", "Home & conversation"],
  ["projects", "Projects", "Workspaces"],
  ["artifacts", "Artifacts", "Outputs library"],
  ["studio", "Studio", "Orchestration"],
  ["costs", "Costs", "Spend & budgets"],
  ["settings", "Settings", "Appearance & status"],
  ["gate", "Gate", "Pending approvals"],
  ["hub", "Hub", "Connectors & capabilities"],
  ["trace", "Trace", "Event log"],
  ["lab", "Lab", "Evals"],
  ["meetings", "Meetings", "Capture"],
  ["vault", "Vault", "Files & knowledge"],
  ["tasks", "Tasks", "Scheduled work"],
  ["memory", "Memory", "Long-term memory"],
];

// kind (from the unified search) -> how to open it. "resume"/"artifact"/"entity"/"project" are
// special-cased; everything else navigates to a screen hash.
const KIND_ROUTE = {
  chat: "resume", artifact: "artifact", project: "project", digest: "daily",
  memory: "memory", source: "vault", wiki: "vault", task: "tasks", run: "studio",
  team: "studio", service: "hub", member: "studio",
  person: "entity", decision: "entity", topic: "entity", external_ref: "entity", custom: "entity",
};
const KIND_LABEL = {
  chat: "Chat", artifact: "Artifact", memory: "Memory", source: "Source", wiki: "Wiki",
  task: "Task", run: "Run", project: "Project", digest: "Digest", team: "Team", service: "Service",
  member: "Member", person: "Entity", decision: "Entity", topic: "Entity",
  external_ref: "Entity", custom: "Entity",
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

function go(hash) { close(); location.hash = hash; }

// Perform a UI-state write (the allowlist), then close. New/scope also clear the local transcript
// (a fresh/scoped conversation started server-side); app.js's WS echoes keep the chips in sync.
async function act(path, body, { resetChat = false, then = null } = {}) {
  const res = await _api.post(path, body || {});
  if (res.ok) {
    if (resetChat && _api.state) _api.state.chat = [];
    close();
    if (then) then();
  }
}

function computeActions(q) {
  const ql = q.toLowerCase();
  const out = [];
  const add = (title, snip, run) => out.push({ kind: "action", chip: "Do", title, snip, run });
  const pid = _ctx.runner.project && _ctx.runner.project.id;
  add("New Chat", "Start a fresh conversation",
    () => act("/api/sessions/new", {}, { resetChat: true, then: () => go("#daily") }));
  if (pid) {
    add("Open Active Workspace", "This project's workspace", () => go(`#workspace/${pid}`));
    add("Open Graph", "This project's knowledge graph", () => go(`#workspace/${pid}/graph`));
  }
  add("Run Workflow", "Assemble a team in Studio", () => go("#studio"));
  for (const [v, label] of [["plan", "Planning"], ["approval", "Approval"], ["auto", "Auto"]]) {
    if (_ctx.runner.mode !== v) add(`Switch mode: ${label}`, "Run mode", () => act("/api/mode", { mode: v }));
  }
  for (const m of _ctx.models) {
    if (m.selectable && !m.current) {
      add(`Switch model: ${m.label}`, "Interactive model", () => act("/api/model", { model: m.id }));
    }
  }
  if (pid) add("Switch to Global", "Chat outside any project",
    () => act("/api/projects/select", { project_id: null }, { resetChat: true, then: () => go("#daily") }));
  for (const p of _ctx.projects) {
    if (p.id !== pid) {
      add(`Switch to project: ${p.name || "Project " + p.id}`, "Project scope",
        () => act("/api/projects/select", { project_id: p.id }, { resetChat: true, then: () => go("#daily") }));
    }
  }
  return out.filter((a) => !ql || a.title.toLowerCase().includes(ql) || a.snip.toLowerCase().includes(ql));
}

// Recent chats, matched by TITLE (the unified search is word/content-based + project-scoped, so it
// can miss chats — this makes them always findable + jumpable). Selecting one resumes it.
function computeChats(q) {
  const ql = q.toLowerCase();
  return (_ctx.sessions || [])
    .filter((s) => !ql || (s.title || "").toLowerCase().includes(ql))
    .slice(0, 8)
    .map((s) => ({
      kind: "chat", chip: "Chat", title: s.title || `Chat ${s.id}`,
      snip: "Resume this conversation",
      run: () => { _api.resumeChat(s.id).then(() => go("#daily")); },
    }));
}

function computeNav(q) {
  const ql = q.toLowerCase();
  return NAV.filter(([key, label, sub]) =>
    !ql || label.toLowerCase().includes(ql) || key.includes(ql) || sub.toLowerCase().includes(ql),
  ).map(([key, label, sub]) => ({ kind: "nav", chip: "Go", title: label, snip: sub, run: () => go(`#${key}`) }));
}

function resultItems(results) {
  return (results || []).map((r) => ({
    kind: "result", chip: KIND_LABEL[r.kind] || r.kind, title: r.label || r.title || "(untitled)",
    snip: (r.badges || []).join(" · ") || r.snippet || "", run: () => openResult(r),
  }));
}

function openResult(r) {
  const dest = KIND_ROUTE[r.kind] || "daily";
  if (dest === "resume") { _api.resumeChat(Number(r.ref_id)).then(() => go("#daily")); return; }
  if (dest === "artifact") {
    window.open(`/api/artifacts/${encodeURIComponent(r.ref_id)}/content`, "_blank", "noopener");
    close();
    return;
  }
  if (dest === "project") { go(`#workspace/${r.ref_id}`); return; }
  if (dest === "entity") { openEntity(r); return; }
  go(`#${dest}`);
}

// Open the graph tab focused on an entity. The graph consumes this session-only focus once, so a
// database reset cannot turn an old project-id reuse into a stale graph view. Navigate-only.
function openEntity(r) {
  const pid = _ctx.runner.project && _ctx.runner.project.id;
  if (!pid) { go("#projects"); return; }
  try {
    sessionStorage.setItem(`kairo:graph:focus:${pid}`, `${r.kind}:${r.ref_id}`);
  } catch { /* storage disabled — the graph just opens unfocused */ }
  go(`#workspace/${pid}/graph`);
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
  if (!q) return;
  const my = ++_seq;
  _timer = setTimeout(async () => {
    if (!_api) return;
    const pid = _ctx.runner.project && _ctx.runner.project.id;
    const scope = pid ? `&project_id=${pid}` : "";
    const data = await _api.get(`/api/graph/search?q=${encodeURIComponent(q)}&limit=25${scope}`);
    if (my !== _seq) return;  // a newer keystroke superseded this one
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
  _input.value = "";
  _sel = 0;
  rebuild();       // instant: actions + nav (context from the last open)
  _input.focus();
  _escOff = pushEscape(close);
  // Refresh the action context (active project/model/mode + the pickers + recent chats), re-render.
  const [runner, projects, models, sessions] = await Promise.all([
    _api.get("/api/runner"), _api.get("/api/projects"), _api.get("/api/models"),
    _api.get("/api/sessions?limit=20"),
  ]);
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
