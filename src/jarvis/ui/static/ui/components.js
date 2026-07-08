// Reusable UI component builders (Phase 11 T5). Token-driven; return DOM nodes (never HTML
// strings), so untrusted values go through textContent by construction. Screens (T6-T14) extend
// this set. Everything uses the V2 component classes in kairo.css.

import { el } from "./dom.js";

// A glass panel. `title`/`subtitle` render an optional .panel-title header; `children` fill it.
export function surface({ title, subtitle, actions, children, cls } = {}) {
  const kids = [];
  if (title || actions) {
    const head = el("div", { class: "panel-title" }, [
      el("div", null, [
        el("h3", { text: title || "" }),
        subtitle ? el("p", { text: subtitle }) : null,
      ]),
      actions || null,
    ]);
    kids.push(head);
  }
  for (const c of [].concat(children || [])) if (c) kids.push(c);
  return el("div", { class: "surface" + (cls ? " " + cls : "") }, kids);
}

// A rounded status pill: an optional leading dot + optional <strong> label + trailing text.
export function statusPill({ label, text, tone } = {}) {
  const cls = "status-pill" + (tone ? " " + tone : "");
  const kids = [el("span", { class: "dot" })];
  if (label) kids.push(el("strong", { text: label }));
  if (text) kids.push(el("span", { text: text }));
  return el("span", { class: cls }, kids);
}

// A polished empty state that teaches the next action (calm; every screen has one).
export function emptyState(title, body, action) {
  return el("div", { class: "empty-state" }, [
    el("h4", { text: title }),
    body ? el("p", { class: "dim", text: body }) : null,
    action || null,
  ]);
}

// A list row: leading icon slot, a main body (nodes/string), an optional trailing node.
// `tone` = "attention" | "cost-tone" for the amber/teal variants.
export function listRow({ icon, body, trailing, tone, onClick } = {}) {
  const cls = "list-row" + (tone ? " " + tone : "");
  const node = el("div", { class: cls }, [
    el("div", { class: "list-icon" }, icon != null ? [icon] : []),
    el("div", { class: "grow", style: "min-width:0" }, [].concat(body || [])),
    trailing || null,
  ]);
  if (onClick) {
    node.style.cursor = "pointer";
    node.addEventListener("click", onClick);
  }
  return node;
}

// A horizontal tab bar. tabs: [{id, label, count}]; onSelect(id) fires on click.
export function tabBar(tabs, activeId, onSelect) {
  return el(
    "div",
    { class: "tabbar" },
    tabs.map((t) =>
      el(
        "button",
        { class: "tab-button" + (t.id === activeId ? " active" : ""), onclick: () => onSelect(t.id) },
        [el("span", { text: t.label }), t.count != null ? el("span", { class: "badge", text: String(t.count) }) : null],
      ),
    ),
  );
}
