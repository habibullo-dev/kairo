// Command palette (Phase 11 T7). Ctrl/Cmd-K opens it. It does exactly two things and both are
// READ/navigate: (1) federated search over /api/search (a GET), (2) jump to a screen (hash) or
// open an artifact's read-only content (a GET, new tab). It NEVER mutates: it calls only the
// shell's GET helper, never the mutation helper.
// All rows render via el()/textContent, so a search snippet can never inject markup.
import { el } from "./dom.js";
import { setPaletteToggle, pushEscape } from "./keys.js";

// Static "Go to" commands — every reachable screen (primary rail + utility + the hash-only
// Vault/Tasks/Memory). Selecting one navigates; it grants nothing the rail links don't already.
const NAV = [
  ["daily", "Daily", "Home & conversation"],
  ["projects", "Projects", "Workspaces"],
  ["studio", "Studio", "Orchestration"],
  ["costs", "Costs", "Spend & budgets"],
  ["settings", "Settings", "Appearance"],
  ["gate", "Gate", "Pending approvals"],
  ["trace", "Trace", "Event log"],
  ["hub", "Hub", "Connectors"],
  ["lab", "Lab", "Evals"],
  ["meetings", "Meetings", "Capture"],
  ["vault", "Vault", "Files & knowledge"],
  ["tasks", "Tasks", "Scheduled work"],
  ["memory", "Memory", "Long-term memory"],
];

// A search result's destination (navigate-only). Artifacts have no screen yet (T11 adds one),
// so an artifact opens its hardened, read-only content route in a new tab.
const DOMAIN_ROUTE = {
  chats: "daily", digests: "daily", memories: "memory",
  knowledge: "vault", tasks: "tasks", orchestration: "studio",
};
const DOMAIN_LABEL = {
  chats: "Chat", digests: "Digest", memories: "Memory", knowledge: "Knowledge",
  tasks: "Task", orchestration: "Run", artifacts: "Artifact",
};

let _api = null;
let _overlay = null;
let _input = null;
let _list = null;
let _navItems = [];
let _items = [];
let _sel = 0;
let _escOff = null;
let _seq = 0; // search race guard
let _timer = null;

function computeNav(q) {
  const ql = q.toLowerCase();
  return NAV.filter(
    ([key, label, sub]) =>
      !ql || label.toLowerCase().includes(ql) || key.includes(ql) || sub.toLowerCase().includes(ql),
  ).map(([key, label, sub]) => ({
    kind: "nav", chip: "Go", title: label, snip: sub, run: () => go(`#${key}`),
  }));
}

function resultItems(results) {
  return (results || []).map((r) => ({
    kind: "result",
    chip: DOMAIN_LABEL[r.domain] || r.domain,
    title: r.title || "(untitled)",
    snip: r.snippet || "",
    run: () => openResult(r),
  }));
}

function go(hash) {
  close();
  location.hash = hash;
}

function openResult(r) {
  if (r.domain === "artifacts") {
    // Read-only GET; the content route is registered-id-only, quarantine-refusing, media-allow-
    // listed, size-capped and never leaks a local path. noopener: the new tab gets no handle back.
    window.open(`/api/artifacts/${encodeURIComponent(r.ref_id)}/content`, "_blank", "noopener");
    close();
    return;
  }
  go(`#${DOMAIN_ROUTE[r.domain] || "daily"}`);
}

function markSel() {
  for (const row of _list.querySelectorAll(".list-row")) {
    row.classList.toggle("sel", Number(row.dataset.i) === _sel);
  }
}

function render() {
  _list.textContent = "";
  if (!_items.length) {
    _list.appendChild(
      el("div", { class: "palette-empty" }, [_input.value.trim() ? "No matches." : "Type to search."]),
    );
    return;
  }
  let lastKind = null;
  _items.forEach((it, i) => {
    if (it.kind !== lastKind) {
      _list.appendChild(el("div", { class: "palette-cap" }, [it.kind === "nav" ? "Go to" : "Results"]));
      lastKind = it.kind;
    }
    const row = el(
      "div",
      { class: `list-row${i === _sel ? " sel" : ""}`, dataset: { i: String(i) } },
      [
        el("span", { class: "p-chip" }, [it.chip]),
        el("div", { class: "p-body" }, [
          el("div", { class: "p-title" }, [it.title]),
          it.snip ? el("div", { class: "p-snip" }, [it.snip]) : null,
        ]),
        el("span", {}, []),
      ],
    );
    row.addEventListener("click", () => { _sel = i; activate(); });
    row.addEventListener("mousemove", () => { if (_sel !== i) { _sel = i; markSel(); } });
    _list.appendChild(row);
  });
}

function scheduleSearch() {
  const q = _input.value.trim();
  _navItems = computeNav(q);
  _items = _navItems.slice(); // show nav commands instantly; results arrive after the debounce
  _sel = 0;
  render();
  if (_timer) clearTimeout(_timer);
  if (!q) return;
  const my = ++_seq;
  _timer = setTimeout(async () => {
    if (!_api) return;
    const data = await _api.get(`/api/search?q=${encodeURIComponent(q)}&limit=30`);
    if (my !== _seq) return; // a newer keystroke already superseded this one
    _items = _navItems.concat(resultItems((data && data.results) || []));
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

function activate() {
  _items[_sel]?.run();
}

function onInputKey(ev) {
  if (ev.key === "ArrowDown") { ev.preventDefault(); move(1); }
  else if (ev.key === "ArrowUp") { ev.preventDefault(); move(-1); }
  else if (ev.key === "Enter") { ev.preventDefault(); activate(); }
  // Escape is handled by the global keys.js dispatcher (the pushEscape stack), so nested
  // overlays unwind consistently.
}

function ensureDom() {
  if (_overlay) return;
  _input = el("input", {
    type: "text", placeholder: "Search or jump to a screen…",
    autocomplete: "off", spellcheck: "false", "aria-label": "Command palette",
  });
  const bar = el("div", { class: "search-input" }, [
    el("span", { class: "icon" }, ["⌕"]),
    _input,
    el("span", { class: "kbd" }, ["Esc"]),
  ]);
  _list = el("div", { class: "palette-results" }, []);
  const panel = el("div", { class: "command-palette" }, [bar, _list]);
  _overlay = el("div", { class: "command-overlay" }, [panel]);
  _overlay.addEventListener("click", (ev) => { if (ev.target === _overlay) close(); });
  _input.addEventListener("input", scheduleSearch);
  _input.addEventListener("keydown", onInputKey);
  document.body.appendChild(_overlay);
}

function open() {
  ensureDom();
  if (_overlay.classList.contains("open")) return;
  _overlay.classList.add("open");
  _input.value = "";
  _navItems = computeNav("");
  _items = _navItems.slice();
  _sel = 0;
  render();
  _input.focus();
  _escOff = pushEscape(close);
}

function close() {
  if (!_overlay || !_overlay.classList.contains("open")) return;
  _overlay.classList.remove("open");
  if (_escOff) { _escOff(); _escOff = null; }
  if (_timer) { clearTimeout(_timer); _timer = null; } // don't let a debounced GET fire post-close
  _seq++; // and invalidate any already in-flight search
}

function toggle() {
  if (_overlay && _overlay.classList.contains("open")) close();
  else open();
}

// Wire the palette to the keyboard dispatcher. api is the shell's GET/POST helper — the palette
// uses ONLY api.get.
export function init(api) {
  _api = api;
  setPaletteToggle(toggle);
}
