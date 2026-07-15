// Central keyboard dispatcher (Phase 11 T6). ONE document keydown listener owns the shortcut
// surface so screens/overlays don't fight over the keyboard:
//   - a registrable global hotkey (the command palette binds Ctrl/Cmd-K in T7);
//   - Escape closes the top-most open overlay (approval modal, palette) — a stack, so nested
//     overlays unwind one at a time;
//   - a per-screen scope map (single keys) that navigate() clears, so a screen's local keys
//     never bleed into the next screen.
// This adds NO authority — it only routes keystrokes to handlers the app already owns.

let _paletteToggle = null; // set by the palette (T7)
let _escapeStack = []; // [{ close, trapRoot }] — most-recently-opened overlay is last
let _scope = new Map(); // key -> fn, for the active screen only

export function setPaletteToggle(fn) {
  _paletteToggle = fn;
}

// Register an overlay so Escape closes the top-most open one. Returns an unregister fn the
// overlay calls when it closes by any other means (button, backdrop, navigate).
export function pushEscape(closeFn, trapRoot = null) {
  const entry = { close: closeFn, trapRoot };
  _escapeStack.push(entry);
  return () => {
    _escapeStack = _escapeStack.filter((e) => e !== entry);
  };
}

function trapTab(ev, root) {
  const selector = [
    "a[href]", "button:not([disabled])", "input:not([disabled])",
    "select:not([disabled])", "textarea:not([disabled])", "summary",
    '[contenteditable="true"]', '[tabindex]:not([tabindex="-1"])',
  ].join(",");
  const focusable = [...root.querySelectorAll(selector)]
    .filter((node) => !node.hidden && node.getClientRects().length > 0);
  if (!focusable.length) {
    ev.preventDefault();
    root.focus();
    return;
  }
  const first = focusable[0];
  const last = focusable[focusable.length - 1];
  const active = document.activeElement;
  if (!root.contains(active) || active === root) {
    ev.preventDefault();
    (ev.shiftKey ? last : first).focus();
  } else if (ev.shiftKey && active === first) {
    ev.preventDefault();
    last.focus();
  } else if (!ev.shiftKey && active === last) {
    ev.preventDefault();
    first.focus();
  }
}

export function bindScope(map) {
  _scope = new Map(Object.entries(map || {}));
}

export function clearScope() {
  _scope = new Map();
}

function typingInField(ev) {
  const t = ev.target;
  if (!t) return false;
  const tag = (t.tagName || "").toLowerCase();
  return tag === "input" || tag === "textarea" || tag === "select" || t.isContentEditable;
}

function onKeydown(ev) {
  const topOverlay = _escapeStack[_escapeStack.length - 1];
  if (topOverlay?.trapRoot) {
    ev.stopImmediatePropagation();
    if (ev.key === "Tab") {
      trapTab(ev, topOverlay.trapRoot);
      return;
    }
    if (ev.key === "Escape") {
      ev.preventDefault();
      try {
        topOverlay.close();
      } catch {
        /* closing must never throw into the dispatcher */
      }
      return;
    }
    // A modal owns the active keyboard scope. Native keys still reach its controls, while
    // global palette/screen shortcuts stay dormant until the modal closes.
    return;
  }
  const mod = ev.ctrlKey || ev.metaKey;
  if (mod && (ev.key === "k" || ev.key === "K")) {
    if (_paletteToggle) {
      ev.preventDefault();
      _paletteToggle();
    }
    return;
  }
  if (ev.key === "Escape" && _escapeStack.length) {
    const top = _escapeStack[_escapeStack.length - 1];
    try {
      top.close();
    } catch {
      /* closing must never throw into the dispatcher */
    }
    return;
  }
  // Per-screen single-key scope — never while typing, never with a modifier.
  if (!mod && !ev.altKey && !typingInField(ev)) {
    const fn = _scope.get(ev.key);
    if (fn) fn(ev);
  }
}

let _installed = false;
export function initKeys() {
  if (_installed) return;
  document.addEventListener("keydown", onKeydown);
  _installed = true;
}
