// Settings (Phase 11 T6 + T14; Phase 13 T7 maturity). Appearance controls (client-side only, via
// ui/theme.js — localStorage, NO server route) + a debug/trace toggle (presentation-only body
// class) + READ-ONLY policy surfaces (providers w/ authority/private-ok, services w/ availability
// + egress/context-policy/output-trust badges + credential env NAMES, connectors w/ granted scope
// names + expiry, configured Skill Forge pins, budgets + per-service caps) sourced from
// /api/settings (Phase 13) plus the /api/hub, /api/costs, /api/runner reads. Presence/state/NAMES
// only — never a key value or a token. Global service flags stay YAML-only (the panel shows the
// exact line). Nothing here grants authority or mutates.
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
let _statusAuthorityToken = null;

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

function badge(text) {
  return el("span", { class: "chip settings-badge" }, [text]);
}

// One service row: name + state pill + policy badges (egress / context-policy / output-trust) +
// the credential env-var NAMES (never a value). Read-only.
function serviceRow(x) {
  const ok = x.state === "available";
  const head = el("div", { class: "settings-service-head" }, [
    el("span", { class: "status-pill" + (ok ? " good" : "") }, [
      el("span", { class: "dot" + (ok ? "" : " off") }, []), el("span", {}, [`${x.name} · ${x.state}`]),
    ]),
    x.egress ? badge("egress") : null,
    badge(x.context_policy),
    badge(x.output_trust),
    (x.credential_env || []).length ? badge("key: " + x.credential_env.join(", ")) : null,
  ].filter(Boolean));
  const note = x.note ? el("div", { class: "set-s settings-service-note" }, [x.note]) : null;
  return el("div", { class: "settings-service-row" }, [head, note].filter(Boolean));
}

// One capability line: name · state (+ "not in chat" when connected-but-unexposed) + plain reason.
// Renders the SHARED capability_truth, so Settings agrees with Daily and the Hub.
function capLine(r) {
  const usable = ["connected", "available", "on"].includes(r.state) && r.exposed_to_chat;
  const notInChat = ["connected", "available", "on"].includes(r.state) && !r.exposed_to_chat;
  const pill = el("span", { class: "status-pill" + (usable ? " good" : "") }, [
    el("span", { class: "dot" + (usable ? "" : " off") }, []),
    el("span", {}, [`${r.name} · ${r.state}${notInChat ? " · not in chat" : ""}`]),
  ]);
  const reason = r.reason
    ? el("span", { class: "set-s settings-reason" }, [r.reason]) : null;
  return el("div", { class: "cap-line" }, [pill, reason].filter(Boolean));
}

function capabilitySection(setData) {
  const capd = setData.capabilities || {};
  const nodes = [];
  for (const [title, rows] of [["Connectors", capd.connectors], ["Model providers", capd.providers],
    ["Services & tools", capd.services]]) {
    if (!(rows || []).length) continue;
    nodes.push(el("div", { class: "cap-group" }, [title]));
    for (const r of rows) nodes.push(capLine(r));
  }
  const voice = capd.voice || {};
  const mcp = capd.mcp || {};
  nodes.push(el("div", { class: "cap-group" }, ["Voice & MCP"]));
  nodes.push(capLine({ name: "Voice", state: voice.state || "off",
    exposed_to_chat: voice.exposed_to_chat, reason: voice.reason }));
  nodes.push(capLine({ name: "MCP", state: mcp.state || "not_configured",
    exposed_to_chat: false, reason: mcp.reason }));
  return statusSection("Capabilities — what chat can use", nodes);
}

function renderStatus(region) {
  region.textContent = "";
  const s = _status;
  if (!s) { region.appendChild(el("div", { class: "dim" }, ["Loading status…"])); return; }
  const unavailable = Object.entries(s)
    .filter(([, data]) => data == null)
    .map(([name]) => name);
  if (unavailable.length) {
    region.appendChild(el("div", { class: "empty-state" }, [
      el("h4", {}, ["Some status is unavailable"]),
      el("div", {}, [`Couldn't load ${unavailable.join(", ")}. The remaining status is shown below.`]),
    ]));
  }
  const hub = s.hub || {};
  const costs = s.costs || {};
  const runner = s.runner || {};
  const set = s.settings || {};

  // The shared availability truth FIRST — the calm, plain-language answer to "what can chat use?".
  region.appendChild(capabilitySection(set));

  // Providers (10C availability: state + authority + private_ok) & model routes
  const provRows = (set.providers || []).map((p) => {
    const tags = [p.trusted_authority ? "authority" : null, p.private_ok ? "private-ok" : null,
      p.tool_capable ? "tools" : null].filter(Boolean).join(" · ");
    return metaRow(p.name, `${p.state}${tags ? " · " + tags : ""}`);
  });
  const routeRows = (set.model_routes || hub.model_routes || []).slice(0, 8).map((r) =>
    metaRow(r.role, `${r.model || "?"}${r.provider ? " · " + r.provider : ""}${r.configured ? "" : " · no key"}`));
  region.appendChild(statusSection("Providers & model routes", [
    el("div", { class: "art-meta" }, provRows.length ? provRows : [el("div", { class: "dim" }, ["No providers."])]),
    el("div", { class: "set-s settings-section-label" }, ["Model routes"]),
    el("div", { class: "art-meta" }, routeRows),
  ], "hub"));

  // The configured policy is read-only: it exposes only the global decision and explicit
  // overrides, so a tool such as web_search cannot quietly be more permissive than the default.
  // It is deliberately NOT presented as an effective permission: tool defaults and per-call
  // safety floors (paths, shell, sensitive data, taint) remain in the Gate's decision path.
  const posture = set.configured_policy || { state: "unavailable", overrides: [] };
  const postureNodes = posture.state === "available"
    ? [
      el("div", { class: "art-meta" }, [metaRow("Configured default", posture.global_default || "?")]),
      el("div", { class: "set-s settings-section-label" }, [
        "Explicit decisions that differ from the global default",
      ]),
      (posture.overrides || []).length
        ? el("div", { class: "art-meta" }, (posture.overrides || []).map((row) =>
          metaRow(row.tool || "?", row.decision || "?")))
        : el("div", { class: "dim" }, ["No explicit tool decision differs from the global default."]),
      el("div", { class: "set-s settings-section-note" }, [
        "Configured policy only — tool defaults and path, shell, sensitive-data, and taint safety rules still apply per call. Changing a decision still requires the existing Gate policy workflow.",
      ]),
    ]
    : [el("div", { class: "dim" }, ["Configured policy overrides are unavailable."])];
  region.appendChild(statusSection("Configured policy overrides", postureNodes, "gate"));

  // Services catalog — the raw availability + policy badges + credential NAMES + how-to-enable is
  // DEV detail, so it's demoted into a collapsed <details> (the plain truth is up in Capabilities).
  const services = set.services || hub.services || [];
  const svcNodes = services.length
    ? services.map(serviceRow)
    : [el("div", { class: "dim" }, ["No services in the catalog."])];
  if (set.enable_hint) {
    svcNodes.push(el("pre", { class: "set-s settings-enable-hint" }, [set.enable_hint]));
  }
  const details = el("details", { class: "advanced" }, [
    el("summary", {}, ["Advanced: full service catalog + policies"]), ...svcNodes,
  ]);
  region.appendChild(statusSection("Services", [details]));

  // Connectors — presence + granted scope NAMES + expiry (never a token)
  const c = set.connectors || hub.connectors || {};
  const connNodes = [];
  const connChips = [];
  if (hub.demo || c.demo) connChips.push(presencePill("Demo mode", true));
  const g = c.google;
  connChips.push(presencePill("Google", g != null && g.connected));
  for (const [name, cfg] of Object.entries(c.notifiers || {})) {
    const on = !!(cfg && (cfg.connected ?? cfg.configured) && !cfg.needs_reconnect);
    connChips.push(presencePill(name, on));
  }
  connNodes.push(el("div", { class: "conn-strip" }, connChips));
  if (g && g.connected && (g.scopes || g.expires_at)) {
    connNodes.push(el("div", { class: "art-meta settings-meta" }, [
      g.scopes ? metaRow("Google scopes", g.scopes.map((x) => x.split("/").pop()).join(", ")) : null,
      g.expires_at ? metaRow("Token expires", g.expires_at) : null,
    ].filter(Boolean)));
  }
  region.appendChild(statusSection("Connectors", connNodes, "hub"));

  // Budgets & cost ledger (limits + per-service caps)
  const lim = costs.limits || {};
  const bud = set.budgets || {};
  const ledger = hub.cost_ledger || set.cost_ledger || {};
  const cap = (v) => (v == null ? "not set" : money(v));
  region.appendChild(statusSection("Budgets & cost ledger", [
    el("div", { class: "art-meta" }, [
      metaRow("Project / month", lim.project_monthly_usd == null ? "no cap" : money(lim.project_monthly_usd)),
      metaRow("Confirm above", money(lim.confirm_above_usd)),
      metaRow("Soft / hard per run", `${money(lim.soft_warn_usd_per_run)} · ${money(lim.hard_stop_usd_per_run)}`),
      metaRow("Service cap / run · day", `${cap(bud.service_max_usd_per_run)} · ${cap(bud.service_max_usd_per_day)}`),
      metaRow("Context reuse", (set.context_reuse && set.context_reuse.enabled) ? "on" : "off"),
      metaRow("Cost tracking", ledger.degraded ? `degraded (${ledger.unrecorded || 0} unrecorded)` : "healthy"),
    ]),
  ], "costs"));

  // Skill Forge: configuration projection ONLY.  It intentionally does not load a pack directory
  // or claim a configured pin is installed, verified, or injected into a prompt.
  const skills = set.skills || { mode: "off", configured_packs: [] };
  const skillMode = skills.mode || "off";
  const skillModeNote = skillMode === "off"
    ? "Off — runtime inactive; no skill packs are loaded."
    : skillMode === "shadow"
      ? "Shadow — configured pins may be observed, but guidance is not injected into prompts."
      : "Active mode is configured. Pack files are not loaded or verified by this status screen.";
  const pins = skills.configured_packs || [];
  const skillNodes = [
    el("div", { class: "art-meta" }, [
      metaRow("Configured mode", skillMode),
      metaRow("Runtime status", skillModeNote),
    ]),
    el("div", { class: "set-s settings-section-label" }, ["Configured pins"]),
    pins.length
      ? el("div", { class: "art-meta" }, pins.map((pin) =>
        metaRow(pin.pack || "?", `${pin.version || "?"} · hash ${pin.sha256_prefix || "?"}`)))
      : el("div", { class: "dim" }, ["No skill packs are configured."]),
    el("div", { class: "set-s settings-section-note" }, [
      "Configuration only — this screen never loads pack files, injects skill text, or changes runtime mode.",
    ]),
  ];
  region.appendChild(statusSection("Skill Forge", skillNodes));

  // Privacy & safety
  const eg = hub.egress || {};
  const attentionRouting = set.attention_routing || {};
  region.appendChild(statusSection("Privacy & safety", [
    el("div", { class: "art-meta" }, [
      metaRow("Run mode", runner.mode || "approval"),
      metaRow("Unattended", "read-only by default — risky actions require the on-screen Gate"),
      metaRow("Attention routing", attentionRouting.reason ||
        "No attention-routing status available."),
      metaRow("Egress this session", `${eg.text_chars || 0} chars · ${eg.audio_bytes || 0} audio bytes`),
      metaRow("MCP", (hub.mcp && hub.mcp.note) || "not connected"),
    ]),
  ]));
}

async function fetchStatus(api) {
  const authorityToken = typeof api.authorityToken === "function" ? api.authorityToken() : null;
  const [hub, costs, runner, settings] = await Promise.all([
    api.get("/api/hub"), api.get("/api/costs"), api.runnerStatus(), api.get("/api/settings"),
  ]);
  if (typeof api.renderIsCurrent === "function" && !api.renderIsCurrent()) return false;
  if (authorityToken !== null && typeof api.authorityIsCurrent === "function"
      && !api.authorityIsCurrent(authorityToken)) return false;
  _status = { hub, costs, runner, settings };
  _statusAuthorityToken = authorityToken;
  return true;
}

export function render(container, api) {
  container.textContent = "";
  const authorityToken = typeof api.authorityToken === "function" ? api.authorityToken() : null;
  if (_statusAuthorityToken !== authorityToken) {
    _status = null;
    _statusAuthorityToken = authorityToken;
  }
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
  fetchStatus(api).then((current) => {   // refresh in the background, then repaint the status region
    if (!current) return;
    const region = container.querySelector(".set-status");
    if (region) renderStatus(region);
  }).catch(() => {
    if (typeof api.renderIsCurrent === "function" && !api.renderIsCurrent()) return;
    if (authorityToken !== null && typeof api.authorityIsCurrent === "function"
        && !api.authorityIsCurrent(authorityToken)) return;
    _status = { hub: null, costs: null, runner: null, settings: null };
    _statusAuthorityToken = authorityToken;
    const region = container.querySelector(".set-status");
    if (region) renderStatus(region);
  });

  if (_refocus) {
    container.querySelector(`button[data-set-key="${_refocus.key}"][data-set-val="${_refocus.val}"]`)?.focus();
    _refocus = null;
  }
}
