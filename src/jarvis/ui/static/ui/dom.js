// Shared DOM helpers — the ONE place escaping lives (Phase 11 T5). Screens import from here
// instead of each defining a local esc(). No inline event handlers (CSP forbids them); el()
// wires listeners via addEventListener.

// Element-CONTENT safe: escapes & < > (via textContent round-trip). NOT quote-safe — use only
// where the value lands in text content, never inside an attribute value.
export function esc(s) {
  const d = document.createElement("div");
  d.textContent = s ?? "";
  return d.innerHTML;
}

const _ATTR = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&#34;", "'": "&#39;", "`": "&#96;" };

// Attribute-value safe: escapes & < > AND " ' ` so an interpolated value can never break out of a
// quoted attribute (closes the projects/studio interpolated-attribute injection smell). & is in the
// class so it is replaced once, no double-encoding.
export function escAttr(s) {
  return String(s ?? "").replace(/[&<>"'`]/g, (c) => _ATTR[c]);
}

// Tiny element builder. attrs: {class, text, html, dataset:{}, on<event>: fn, <attr>: value}.
// null/false attr values are skipped; children may be nodes or strings (or an array).
export function el(tag, attrs, children) {
  const node = document.createElement(tag);
  if (attrs) {
    for (const [k, v] of Object.entries(attrs)) {
      if (v == null || v === false) continue;
      if (k === "class") node.className = v;
      else if (k === "text") node.textContent = v;
      else if (k === "html") node.innerHTML = v;
      else if (k === "dataset") for (const [dk, dv] of Object.entries(v)) node.dataset[dk] = dv;
      else if (k.startsWith("on") && typeof v === "function") node.addEventListener(k.slice(2), v);
      else if (k === "style") throw new TypeError("Inline styles are blocked by Kairo's CSP; use a CSS class");
      else node.setAttribute(k, v);
    }
  }
  if (children != null) {
    for (const c of [].concat(children)) {
      if (c == null) continue;
      node.appendChild(typeof c === "string" ? document.createTextNode(c) : c);
    }
  }
  return node;
}
