// Shared builders for the Workspace panels (Phase 11 T10). Keeps the tabs visually consistent
// and every panel textContent-safe. Panels import from here + ../../ui/dom.js|format.js.
import { el } from "../../ui/dom.js";

export function emptyState(heading, hint) {
  return el("div", { class: "empty-state" }, [el("h4", {}, [heading]), el("div", {}, [hint])]);
}

export function chip(text, cls) {
  return el("span", { class: "p-chip" + (cls ? " " + cls : "") }, [text]);
}

// A .list-row: [icon] [title / sub] [trailing]. onClick makes the whole row navigable. All text
// is set via el() text children (createTextNode) — untrusted values can never inject markup.
export function row(icon, title, sub, opts = {}) {
  const mid = el("div", { class: "ws-rowmid" }, [
    el("div", { class: "lr-t" }, [title || "(untitled)"]),
    sub ? el("div", { class: "lr-s" }, [sub]) : null,
  ]);
  const r = el("div", { class: "list-row" }, [
    el("span", { class: "list-icon" }, [icon || "•"]),
    mid,
    opts.trailing || el("span", {}, []),
  ]);
  if (opts.onClick) {
    r.style.cursor = "pointer";
    r.addEventListener("click", opts.onClick);
  }
  return r;
}

// A small action button that stops propagation (so it works inside a clickable row).
export function actionButton(label, onClick, variant) {
  const b = el("button", { class: "plain-button" + (variant ? " " + variant : "") }, [label]);
  b.addEventListener("click", (ev) => { ev.stopPropagation(); onClick(); });
  return b;
}

// Map the real OrchestrationRun status vocabulary to a status-pill tone.
export function runTone(status) {
  if (status === "ok") return "good";
  if (status === "running" || status === "revise") return "busy";
  if (["error", "rejected", "aborted", "budget_stopped", "cancelled"].includes(status)) return "danger";
  return "";
}

export function statusPill(text, tone) {
  return el("span", { class: "status-pill" + (tone ? " " + tone : "") }, [text || "—"]);
}

// A titled surface section wrapping a list of nodes.
export function section(title, nodes, actions) {
  const head = [el("h3", {}, [title])];
  if (actions) head.push(actions);
  return el("div", { class: "surface rise" }, [
    el("div", { class: "panel-title" }, head),
    ...nodes,
  ]);
}
