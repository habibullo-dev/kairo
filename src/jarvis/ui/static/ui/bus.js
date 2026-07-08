// Tiny event bus (Phase 11 T6). app.js emits WS/lifecycle events; screens subscribe so they can
// react to live updates without app.js hard-coding each screen's refresh. Not a safety boundary
// (no data/authority here) — purely a UI fan-out. A subscriber error never breaks the socket.

const _handlers = new Map(); // kind -> Set<fn>

export function on(kind, fn) {
  if (!_handlers.has(kind)) _handlers.set(kind, new Set());
  _handlers.get(kind).add(fn);
  return () => off(kind, fn);
}

export function off(kind, fn) {
  _handlers.get(kind)?.delete(fn);
}

export function emit(kind, payload) {
  for (const fn of _handlers.get(kind) || []) {
    try {
      fn(payload);
    } catch {
      /* a subscriber hiccup must not break the WS fan-out */
    }
  }
}
