// Settings (Phase 11 T6). Appearance controls now — client-side only, backed by ui/theme.js
// (localStorage; NO server theme route, so nothing here grants authority or touches the backend).
// T14 expands this screen with read-only status sections (providers, budgets, modes, safety).
import { el } from "../ui/dom.js";
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

// The re-render replaces the clicked button, so record which control changed and refocus its
// equivalent after the rebuild (keyboard users keep their place). Cleared once consumed.
let _refocus = null;

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
        dot.style.background = val; // CSSOM assignment — not subject to CSP style-src
        dot.style.boxShadow = "none"; // the .dot status glow (green) would tint every swatch
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

export function render(container, api) {
  container.textContent = "";
  // set() publishes an "appearance" event; app.js re-renders this screen (and re-syncs the
  // status-bar toggle) from that single path — so we don't self-render here (no double render).
  const onchange = (key, val) => {
    _refocus = { key, val };
    set({ [key]: val });
  };
  const st = get();

  const head = el("div", { class: "rise" }, [
    el("h1", {}, ["Settings"]),
    el("div", { class: "sub" }, [
      "Personalise the workstation. Appearance is saved only in this browser.",
    ]),
  ]);

  const rows = GROUPS.map((g) => segRow(g, st[g.key], onchange));
  rows.push(segRow({ key: "accent", h: "Accent", s: "Highlight colour across the workstation.",
    options: ACCENTS }, st.accent || "", onchange));

  const appearance = el("div", { class: "surface rise" }, [
    el("div", { class: "panel-title" }, [el("h3", {}, ["Appearance"])]),
    ...rows,
  ]);

  const note = el("div", { class: "empty-state rise" }, [
    el("h4", {}, ["More settings coming"]),
    el("div", {}, [
      "Providers, budgets, modes and safety status appear here in a later pass.",
    ]),
  ]);

  container.append(head, appearance, note);

  if (_refocus) {
    const sel = `button[data-set-key="${_refocus.key}"][data-set-val="${_refocus.val}"]`;
    container.querySelector(sel)?.focus();
    _refocus = null;
  }
}
