// Central keyboard dispatcher (Phase 11 T6). ONE document keydown listener owns the shortcut
// surface so screens/overlays don't fight over the keyboard:
//   - a registrable global hotkey (the command palette binds Ctrl/Cmd-K in T7);
//   - Escape closes the top-most open overlay (approval modal, palette) — a stack, so nested
//     overlays unwind one at a time;
//   - a per-screen scope map (single keys) that navigate() clears, so a screen's local keys
//     never bleed into the next screen.
// This adds NO authority — it only routes keystrokes to handlers the app already owns.

let _paletteToggle = null; // set by the palette (T7)
let _escapeStack = []; // [{ close }] — most-recently-opened overlay is last
let _scope = new Map(); // key -> fn, for the active screen only

export function setPaletteToggle(fn) {
  _paletteToggle = fn;
}

// Register an overlay so Escape closes the top-most open one. Returns an unregister fn the
// overlay calls when it closes by any other means (button, backdrop, navigate).
export function pushEscape(closeFn) {
  const entry = { close: closeFn };
  _escapeStack.push(entry);
  return () => {
    _escapeStack = _escapeStack.filter((e) => e !== entry);
  };
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
