// Settings (Phase 11 T6 + T14). Appearance controls (client-side only, via ui/theme.js —
// localStorage, NO server route) + a debug/trace toggle (presentation-only body class) + READ-ONLY
// status sections (providers/routes, services, connectors, budgets, privacy/safety) sourced from
// the existing /api/hub, /api/costs and /api/runner reads. Presence booleans only — never a key
// value. Nothing here grants authority or mutates.
import { el } from "../ui/dom.js";
import { money } from "../ui/format.js";
import { get, set, THEMES } from "../ui/theme.js";

function cap(s) {
  return s.charAt(0).toUpperCase() + s.slice(1);
}

const GROUPS = [
  { key: "theme", h: "Theme", s: "Overall look of the workstation.",
    options: THEMES.map((t) => [t, cap(t)]) },
  { key: "density", h: "Density", s: "Spacing of lists and cards.",
    options: [["comfortable", "Comfortable"], ["compact", "Compact"]] },
  { key: "layout", h: "Layout", s: "Reading width of the main column.",
    options: [["focused", "Focused"], ["expanded", "Expanded"]] },
  { key: "motion", h: "Motion", s: "Transitions and animated accents.",
    options: [["on", "Full"], ["off", "Reduced"]] },
];

const ACCENTS = [
  ["", "Default"], ["#7cc4ff", "Ice"], ["#8b7cff", "Violet"],
  ["#4fd1c5", "Teal"], ["#ffb26b", "Amber"], ["#ff7eb6", "Rose"],
];

let _refocus = null;   // keyboard focus to restore after an appearance re-render
let _status = null;    // cached {hub, costs, runner} so an appearance change doesn't reflash it

function segRow(cfg, current, onchange) {
  const seg = el(
    "div",
    { class: "seg" },
    cfg.options.map(([val, lbl]) => {
      const btn = el(
        "button",
        {
          class: val === current ? "active" : "",
          dataset: { setKey: cfg.key, setVal: val },
          onclick: () => onchange(cfg.key, val),
        },
        [lbl],
      );
      if (cfg.key === "accent" && val) {
        const dot = el("span", { class: "dot" }, []);
        dot.style.background = val;
        dot.style.boxShadow = "none";
        btn.prepend(dot);
      }
      return btn;
    }),
  );
  return el("div", { class: "set-row" }, [
    el("div", { class: "set-h" }, [cfg.h]),
    el("div", { class: "set-s" }, [cfg.s]),
    seg,
  ]);
}

// Debug/trace: a client-side body class that only REVEALS telemetry — never a capability.
function debugRow() {
  const isOn = () => document.body.classList.contains("debug");
  const off = el("button", {}, ["Off"]);
  const on = el("button", {}, ["On"]);
  const sync = () => { off.classList.toggle("active", !isOn()); on.classList.toggle("active", isOn()); };
  off.addEventListener("click", () => { document.body.classList.remove("debug"); sync(); });
  on.addEventListener("click", () => { document.body.classList.add("debug"); sync(); });
  sync();
  return el("div", { class: "set-row" }, [
    el("div", { class: "set-h" }, ["Debug / trace"]),
    el("div", { class: "set-s" }, ["Reveal telemetry (Trace, context, raw events). Presentation-only — changes no capability."]),
    el("div", { class: "seg" }, [off, on]),
  ]);
}

function presencePill(label, on) {
  const pill = el("span", { class: "status-pill" + (on ? " good" : "") }, []);
  const dot = el("span", { class: "dot" + (on ? "" : " off") }, []);
  pill.append(dot, el("span", {}, [label]));
  return pill;
}

function statusSection(title, nodes, link) {
  const head = [el("h3", {}, [title])];
  if (link) head.push(el("a", { href: `#${link}` }, [`${cap(link)} →`]));
  return el("div", { class: "surface rise" }, [el("div", { class: "panel-title" }, head), ...nodes]);
}

function metaRow(k, v) {
  return el("div", { class: "art-meta-row" }, [
    el("span", { class: "art-meta-k" }, [k]),
    el("span", { class: "art-meta-v" }, [String(v)]),
  ]);
}

function renderStatus(region) {
  region.textContent = "";
  const s = _status;
  if (!s) { region.appendChild(el("div", { class: "dim" }, ["Loading status…"])); return; }
  const hub = s.hub || {};
  const costs = s.costs || {};
  const runner = s.runner || {};

  // Providers & model routes
  const provChips = el("div", { class: "conn-strip" },
    Object.entries(hub.providers || {}).map(([name, on]) => presencePill(name, !!on)));
  const routeRows = (hub.model_routes || []).slice(0, 8).map((r) =>
    metaRow(r.role, `${r.model || "?"}${r.provider ? " · " + r.provider : ""}${r.configured ? "" : " · no key"}`));
  region.appendChild(statusSection("Providers & model routes", [provChips, el("div", { class: "art-meta", style: "margin-top:10px" }, routeRows)], "hub"));

  // Services catalog
  const svc = el("div", { class: "conn-strip" },
    (hub.services || []).map((x) => {
      const okState = x.state === "available";
      const pill = el("span", { class: "status-pill" + (okState ? " good" : "") }, []);
      pill.append(el("span", { class: "dot" + (okState ? "" : " off") }, []), el("span", {}, [`${x.name} · ${x.state}`]));
      return pill;
    }));
  region.appendChild(statusSection("Services", [(hub.services || []).length ? svc : el("div", { class: "dim" }, ["No services in the catalog."])]));

  // Connectors
  const c = hub.connectors || {};
  const connChips = [];
  if (hub.demo || c.demo) connChips.push(presencePill("Demo mode", true));
  connChips.push(presencePill("Google", c.google != null));
  for (const [name, cfg] of Object.entries(c.notifiers || {})) {
    const on = !!(cfg && (cfg.connected ?? cfg.configured) && !cfg.needs_reconnect);
    connChips.push(presencePill(name, on));
  }
  region.appendChild(statusSection("Connectors", [el("div", { class: "conn-strip" }, connChips)], "hub"));

  // Budgets & cost ledger
  const lim = costs.limits || {};
  const ledger = hub.cost_ledger || {};
  region.appendChild(statusSection("Budgets & cost ledger", [
    el("div", { class: "art-meta" }, [
      metaRow("Project / month", lim.project_monthly_usd == null ? "no cap" : money(lim.project_monthly_usd)),
      metaRow("Confirm above", money(lim.confirm_above_usd)),
      metaRow("Soft / hard per run", `${money(lim.soft_warn_usd_per_run)} · ${money(lim.hard_stop_usd_per_run)}`),
      metaRow("Cost tracking", ledger.degraded ? `degraded (${ledger.unrecorded || 0} unrecorded)` : "healthy"),
    ]),
  ], "costs"));

  // Privacy & safety
  const eg = hub.egress || {};
  region.appendChild(statusSection("Privacy & safety", [
    el("div", { class: "art-meta" }, [
      metaRow("Run mode", runner.mode || "approval"),
      metaRow("Unattended", "read-only by default — risky actions require the on-screen Gate"),
      metaRow("Egress this session", `${eg.text_chars || 0} chars · ${eg.audio_bytes || 0} audio bytes`),
      metaRow("MCP", (hub.mcp && hub.mcp.note) || "not connected"),
    ]),
  ]));
}

async function fetchStatus(api) {
  const [hub, costs, runner] = await Promise.all([
    api.get("/api/hub"), api.get("/api/costs"), api.get("/api/runner"),
  ]);
  _status = { hub, costs, runner };
}

export function render(container, api) {
  container.textContent = "";
  const onchange = (key, val) => { _refocus = { key, val }; set({ [key]: val }); };
  const st = get();

  const head = el("div", { class: "rise" }, [
    el("h1", {}, ["Settings"]),
    el("div", { class: "sub" }, ["Personalise the workstation, and review its status. Appearance is saved only in this browser."]),
  ]);

  const rows = GROUPS.map((g) => segRow(g, st[g.key], onchange));
  rows.push(segRow({ key: "accent", h: "Accent", s: "Highlight colour across the workstation.",
    options: ACCENTS }, st.accent || "", onchange));
  rows.push(debugRow());
  const appearance = el("div", { class: "surface rise" }, [
    el("div", { class: "panel-title" }, [el("h3", {}, ["Appearance & display"])]),
    ...rows,
  ]);

  const statusRegion = el("div", { class: "set-status" }, []);
  container.append(head, appearance, statusRegion);
  renderStatus(statusRegion);           // cached (instant) or a Loading line
  fetchStatus(api).then(() => {          // refresh in the background, then repaint the status region
    const region = container.querySelector(".set-status");
    if (region) renderStatus(region);
  });

  if (_refocus) {
    container.querySelector(`button[data-set-key="${_refocus.key}"][data-set-val="${_refocus.val}"]`)?.focus();
    _refocus = null;
  }
}
