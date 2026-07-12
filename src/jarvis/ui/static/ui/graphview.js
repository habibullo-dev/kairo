// Calm graph canvas (Phase 15) — a self-contained Canvas 2D force layout for the Memory Graph.
// No dependencies, no CDN, no image assets: nodes are token-colored circles with a trust RING
// (solid = trusted/reviewed, dashed = model-generated, hazard-dashed = untrusted-external — plus a
// text badge in the side panel, never color-only), edges are thin (derived) / solid (asserted)
// lines. Layout is DETERMINISTIC (seeded by a hash of each node id, no Math.random) so the same
// graph lays out the same way every time (stable screenshots); the simulation settles and STOPS.
// Under reduced-motion it runs to rest synchronously and draws once (no animation). Read/inspect
// only — clicking a node calls onNode(); the canvas never mutates anything.
import { el } from "./dom.js";

const KIND_TOKEN = {
  project: "--accent", run: "--accent-2", member: "--subtle", source: "--cost",
  artifact: "--accent-3", memory: "--good", task: "--muted", team: "--attention",
  wiki: "--muted", folder: "--accent", digest: "--muted", person: "--accent-2", decision: "--attention",
  topic: "--accent-3", external_ref: "--cost",
};
const NODE_CAP = 300; // bounded by the read model; enough room for a real project branch

function tok(name, fallback) {
  const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  return v || fallback;
}

// A stable [0,1) hash of a string → deterministic seed positions (no Math.random).
function hash01(str) {
  let h = 2166136261;
  for (let i = 0; i < str.length; i++) {
    h ^= str.charCodeAt(i);
    h = Math.imul(h, 16777619);
  }
  return ((h >>> 0) % 100000) / 100000;
}

function reducedMotion() {
  return document.documentElement.classList.contains("reduce-motion") ||
    (window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches);
}

// Run the deterministic force simulation to rest, mutating each node's {x,y}.
function layout(nodes, edges, w, h, iterations) {
  const cx = w / 2, cy = h / 2;
  const dense = nodes.length > 80;
  const R = Math.min(w, h) * (dense ? 0.47 : 0.36);
  const repel = dense ? 1060 : 1400;
  const byId = new Map(nodes.map((n) => [n.id, n]));
  nodes.forEach((n, i) => {
    const a = hash01(n.id) * Math.PI * 2;
    const r = R * (0.35 + 0.6 * hash01(n.id + ":r"));
    n.x = cx + Math.cos(a) * r + (i % 2 ? 6 : -6);
    n.y = cy + Math.sin(a) * r;
    n.vx = n.vy = 0;
  });
  const links = edges
    .map((e) => [byId.get(e.src), byId.get(e.dst)])
    .filter(([a, b]) => a && b);
  for (let it = 0; it < iterations; it++) {
    for (let i = 0; i < nodes.length; i++) {
      for (let j = i + 1; j < nodes.length; j++) {
        const a = nodes[i], b = nodes[j];
        let dx = a.x - b.x, dy = a.y - b.y;
        let d2 = dx * dx + dy * dy || 0.01;
        const f = repel / d2;
        const d = Math.sqrt(d2);
        dx /= d; dy /= d;
        a.vx += dx * f; a.vy += dy * f; b.vx -= dx * f; b.vy -= dy * f;
      }
    }
    for (const [a, b] of links) {  // spring toward a rest length
      const dx = b.x - a.x, dy = b.y - a.y;
      const d = Math.sqrt(dx * dx + dy * dy) || 0.01;
      const f = (d - 90) * 0.02;
      const ux = dx / d, uy = dy / d;
      a.vx += ux * f; a.vy += uy * f; b.vx -= ux * f; b.vy -= uy * f;
    }
    for (const n of nodes) {
      n.vx += (cx - n.x) * 0.008; n.vy += (cy - n.y) * 0.008;  // gentle centering
      n.x += Math.max(-12, Math.min(12, n.vx)); n.y += Math.max(-12, Math.min(12, n.vy));
      n.vx *= 0.82; n.vy *= 0.82;
      n.x = Math.max(24, Math.min(w - 24, n.x)); n.y = Math.max(24, Math.min(h - 24, n.y));
    }
  }
}

function ringStroke(ctx, trust, fallback) {
  // never color-only: the ring STYLE also encodes trust (the side panel adds the text badge).
  if (trust === "untrusted_external") { ctx.setLineDash([3, 3]); return tok("--danger", "#ff7481"); }
  if (trust === "model_generated") { ctx.setLineDash([6, 3]); return tok("--attention", "#e0a840"); }
  ctx.setLineDash([]); return fallback || tok("--line-strong", "rgba(200,200,200,.3)");
}

function nodeFill(n) {
  // A small theme palette gives the map a calm constellation feel. These hues are decorative,
  // not semantic categories: kind, trust, and source details stay explicit in the inspector.
  if (n.kind === "source") {
    const tones = ["--accent", "--accent-2", "--accent-3", "--good", "--attention"];
    return tok(tones[Math.floor(hash01(`${n.id}:constellation`) * tones.length)], "#888");
  }
  return tok(KIND_TOKEN[n.kind] || "--muted", "#888");
}

function nodeLabel(n) {
  const label = String(n.label || n.kind || "");
  // Source cards retain the full logical upload path, but the canvas needs a calm, scannable name.
  // Paths are metadata only; this does not resolve or touch the filesystem.
  return n.kind === "source" ? label.split(/[\\/]/).pop() || label : label;
}

function nodeRadius(n, dense, focus, selected) {
  if (n.id === focus || n.id === selected) return dense ? 10 : 15;
  const degree = Math.min(8, Number(n.degree || 0));
  const hub = n.kind === "folder" || degree >= 7;
  if (dense) return hub ? 7 + Math.min(3, degree * .35) : 2.6 + Math.min(2.4, degree * .45);
  return hub ? 12 + Math.min(6, degree) : 8 + Math.min(6, degree);
}

function clamp(value, low, high) {
  return Math.max(low, Math.min(high, value));
}

function zoomLabel(scale) {
  return `${Math.round(scale * 100)}%`;
}

// Each relationship gets a stable, shallow quadratic curve. This makes a dense map read like a
// network of threads rather than a grid of straight lines, without inventing any relationship
// that isn't in the read model.
function threadControl(a, b, edge) {
  const dx = b.x - a.x, dy = b.y - a.y;
  const distance = Math.hypot(dx, dy) || 1;
  const bend = Math.min(34, Math.max(5, distance * .12))
    * (hash01(`${edge.src}|${edge.dst}|${edge.edge_kind || "linked"}`) < .5 ? -1 : 1);
  return { x: (a.x + b.x) / 2 - (dy / distance) * bend, y: (a.y + b.y) / 2 + (dx / distance) * bend };
}

function pointOnThread(a, b, edge, progress) {
  const control = threadControl(a, b, edge);
  const remaining = 1 - progress;
  return {
    x: remaining * remaining * a.x + 2 * remaining * progress * control.x + progress * progress * b.x,
    y: remaining * remaining * a.y + 2 * remaining * progress * control.y + progress * progress * b.y,
  };
}

/** Mount a graph canvas into `host`. data = {nodes, edges, focus}. opts.onNode(node) on click. */
export function mountGraph(host, data, opts = {}) {
  host.textContent = "";
  const nodes = (data.nodes || []).slice(0, NODE_CAP).map((n) => ({ ...n }));
  const shown = new Set(nodes.map((n) => n.id));
  const edges = (data.edges || []).filter((e) => shown.has(e.src) && shown.has(e.dst));
  const byId = new Map(nodes.map((node) => [node.id, node]));

  const wrap = el("div", { class: "graph-canvas-wrap" });
  const canvas = el("canvas", {
    class: "graph-canvas", tabindex: "0", role: "img",
    "aria-label": "Interactive knowledge map. Scroll to zoom, drag to pan, and click a node to inspect relationships.",
  });
  const zoomOut = el("button", { class: "graph-control", type: "button", "aria-label": "Zoom out" }, ["−"]);
  const zoomReadout = el("output", { class: "graph-zoom-readout", "aria-label": "Map zoom" }, ["100%"]);
  const zoomIn = el("button", { class: "graph-control", type: "button", "aria-label": "Zoom in" }, ["+"]);
  const reset = el("button", { class: "graph-control graph-control-reset", type: "button" }, ["Reset"]);
  const controls = el("div", { class: "graph-canvas-controls", role: "group", "aria-label": "Map controls" }, [
    zoomOut, zoomReadout, zoomIn, reset,
  ]);
  const hint = el("div", { class: "graph-canvas-hint" }, ["Hover or click a node · scroll to zoom · drag to pan"]);
  const tooltip = el("div", { class: "graph-tooltip is-hidden", role: "tooltip" });
  const status = el("div", { class: "sr-only", role: "status", "aria-live": "polite" });
  wrap.append(canvas, controls, hint, tooltip, status);
  if ((data.nodes || []).length > NODE_CAP) {
    wrap.appendChild(el("div", { class: "graph-cap dim" },
      [`Showing ${NODE_CAP} of ${data.nodes.length} nodes — filter or focus to narrow.`]));
  }
  host.appendChild(wrap);

  const dpr = window.devicePixelRatio || 1;
  const W = Math.max(320, host.clientWidth || 640), H = nodes.length > 100 ? 600 : 520;
  canvas.width = W * dpr; canvas.height = H * dpr;
  canvas.style.width = W + "px"; canvas.style.height = H + "px";
  const ctx = canvas.getContext("2d");
  ctx.scale(dpr, dpr);

  const focus = data.focus;
  const dense = nodes.length > 42;
  let hovered = null;
  let selected = null;
  let drag = null;
  let pressed = null;
  const camera = { x: 0, y: 0, scale: 1 };

  function screenPoint(ev) {
    const rect = canvas.getBoundingClientRect();
    return { x: ev.clientX - rect.left, y: ev.clientY - rect.top };
  }

  function graphPoint(point) {
    return { x: (point.x - camera.x) / camera.scale, y: (point.y - camera.y) / camera.scale };
  }

  function nearest(x, y) {
    const hitRadius = (dense ? 15 : 20) / camera.scale;
    let best = null, bestDistance = hitRadius * hitRadius;
    for (const node of nodes) {
      const distance = (node.x - x) ** 2 + (node.y - y) ** 2;
      if (distance < bestDistance) { bestDistance = distance; best = node; }
    }
    return best;
  }

  function relatedToSelection(edge) {
    return selected && (edge.src === selected.id || edge.dst === selected.id);
  }

  function pulse(edge, index, now) {
    if (reducedMotion()) return;
    const active = selected ? relatedToSelection(edge) : edge.edge_kind === "imports" && index % 17 === 0;
    if (!active) return;
    const a = byId.get(edge.src), b = byId.get(edge.dst);
    if (!a || !b) return;
    const progress = (now / (selected ? 1250 : 2600) + hash01(`${edge.src}:${edge.dst}`)) % 1;
    const { x, y } = pointOnThread(a, b, edge, progress);
    const color = tok("--accent-2", "#84f3dc");
    ctx.beginPath(); ctx.arc(x, y, selected ? 3 : 1.7, 0, Math.PI * 2);
    ctx.fillStyle = color; ctx.globalAlpha = selected ? .95 : .7;
    ctx.shadowColor = color; ctx.shadowBlur = selected ? 14 : 8;
    ctx.fill(); ctx.shadowBlur = 0; ctx.globalAlpha = 1;
  }

  function draw(now = 0) {
    ctx.clearRect(0, 0, W, H);
    const related = new Set(selected ? [selected.id] : []);
    if (selected) {
      for (const edge of edges) {
        if (relatedToSelection(edge)) { related.add(edge.src); related.add(edge.dst); }
      }
    }
    ctx.save();
    ctx.translate(camera.x, camera.y);
    ctx.scale(camera.scale, camera.scale);
    ctx.lineWidth = dense ? .75 : 1;
    for (const [index, e] of edges.entries()) {  // edges under nodes
      const a = byId.get(e.src), b = byId.get(e.dst);
      if (!a || !b) continue;
      // Local import links are the neural threads in Code map; hierarchy edges stay quieter so
      // a deep Files view remains navigable rather than becoming a coloured hairball.
      ctx.strokeStyle = e.edge_kind === "imports"
        ? tok("--accent", "rgba(200,200,200,.3)")
        : tok("--line-strong", "rgba(200,200,200,.3)");
      ctx.setLineDash(e.origin === "asserted" ? [] : [2, 3]);
      const baseAlpha = e.edge_kind === "imports" ? (dense ? .18 : .52) : (dense ? .16 : .7);
      ctx.globalAlpha = selected ? (relatedToSelection(e) ? Math.max(baseAlpha, .82) : .10) : baseAlpha;
      const control = threadControl(a, b, e);
      ctx.beginPath(); ctx.moveTo(a.x, a.y); ctx.quadraticCurveTo(control.x, control.y, b.x, b.y); ctx.stroke();
      pulse(e, index, now);
    }
    ctx.setLineDash([]); ctx.globalAlpha = 1;
    for (const n of nodes) {
      const isSelected = selected && n.id === selected.id;
      const r = nodeRadius(n, dense, focus, selected && selected.id)
        * (isSelected && !reducedMotion() ? 1 + Math.sin(now / 220) * .06 : 1);
      const fill = nodeFill(n);
      ctx.beginPath(); ctx.arc(n.x, n.y, r, 0, Math.PI * 2);
      if (n.kind === "source") { ctx.shadowColor = fill; ctx.shadowBlur = dense ? 10 : 14; }
      ctx.fillStyle = fill;
      ctx.globalAlpha = selected && !related.has(n.id) ? .24 : (dense ? .78 : .9);
      ctx.fill();
      ctx.globalAlpha = 1; ctx.shadowBlur = 0;
      ctx.lineWidth = n.id === focus || isSelected ? 2.5 : (dense ? 1 : 2);
      ctx.strokeStyle = ringStroke(ctx, n.trust_class, n.kind === "source" ? fill : null);
      ctx.stroke(); ctx.setLineDash([]);
      // Titles are interaction-only. A graph becomes a usable constellation when the user sees
      // the one node they are examining, not a competing label for every file on screen.
      const labelVisible = n === hovered || isSelected;
      if (labelVisible) {
        ctx.fillStyle = tok("--ink", "#eee"); ctx.font = "11px ui-sans-serif, system-ui";
        ctx.textAlign = "center";
        const label = nodeLabel(n).slice(0, 22);
        ctx.fillText(label, n.x, n.y + r + 12);
      }
    }
    ctx.restore();
  }

  function updateZoom() {
    zoomReadout.textContent = zoomLabel(camera.scale);
  }

  function zoomAt(point, factor) {
    const before = graphPoint(point);
    camera.scale = clamp(camera.scale * factor, .45, 3.5);
    camera.x = point.x - before.x * camera.scale;
    camera.y = point.y - before.y * camera.scale;
    updateZoom(); draw(performance.now());
  }

  function resetCamera() {
    camera.x = 0; camera.y = 0; camera.scale = 1;
    updateZoom(); status.textContent = "Map view reset."; draw(performance.now());
  }

  function selectNode(node) {
    selected = node;
    const neighborCount = edges.filter((edge) => relatedToSelection(edge)).length;
    status.textContent = `${node.label || node.kind} selected. ${neighborCount} connected relationship${neighborCount === 1 ? "" : "s"} highlighted.`;
    draw(performance.now());
    if (opts.onNode) opts.onNode(node);
  }

  function setTooltip(node, point) {
    if (!node) {
      tooltip.classList.add("is-hidden");
      return;
    }
    tooltip.textContent = `${nodeLabel(node)} · ${node.kind}`;
    tooltip.style.left = `${clamp(point.x + 12, 8, W - 200)}px`;
    tooltip.style.top = `${clamp(point.y + 12, 8, H - 32)}px`;
    tooltip.classList.remove("is-hidden");
  }

  if (reducedMotion()) {
    layout(nodes, edges, W, H, 140);  // settle synchronously, draw once — no animation
    draw();
  } else {
    layout(nodes, edges, W, H, 0);    // seed positions
    let step = 0;
    const tick = () => {
      layout(nodes, edges, W, H, 6);  // a few iterations per frame
      draw(performance.now());
      if (++step < 22 && document.body.contains(canvas)) requestAnimationFrame(tick);
      else if (document.body.contains(canvas)) requestAnimationFrame(signalTick);
    };
    const signalTick = (now) => {
      draw(now);
      if (document.body.contains(canvas)) setTimeout(() => requestAnimationFrame(signalTick), 80);
    };
    requestAnimationFrame(tick);
  }

  canvas.addEventListener("pointermove", (ev) => {
    const point = screenPoint(ev);
    if (drag && drag.pointerId === ev.pointerId) {
      camera.x += point.x - drag.x; camera.y += point.y - drag.y;
      drag.x = point.x; drag.y = point.y;
      draw(performance.now());
      return;
    }
    const graph = graphPoint(point);
    const next = nearest(graph.x, graph.y);
    if (next === hovered) return;
    hovered = next;
    canvas.style.cursor = next ? "pointer" : "grab";
    setTooltip(next, point); draw(performance.now());
  });
  canvas.addEventListener("pointerleave", () => {
    if (!drag && hovered) { hovered = null; canvas.style.cursor = "grab"; setTooltip(null); draw(performance.now()); }
  });
  canvas.addEventListener("pointerdown", (ev) => {
    const point = screenPoint(ev), graph = graphPoint(point);
    const node = nearest(graph.x, graph.y);
    canvas.setPointerCapture(ev.pointerId);
    if (node) {
      pressed = { node, x: point.x, y: point.y, pointerId: ev.pointerId };
      return;
    }
    drag = { x: point.x, y: point.y, pointerId: ev.pointerId };
    canvas.classList.add("is-panning"); setTooltip(null);
  });
  function releasePointer(ev) {
    const point = screenPoint(ev);
    if (drag && drag.pointerId === ev.pointerId) {
      drag = null; canvas.classList.remove("is-panning");
    } else if (pressed && pressed.pointerId === ev.pointerId) {
      const moved = Math.hypot(point.x - pressed.x, point.y - pressed.y);
      if (moved < 6) selectNode(pressed.node);
      pressed = null;
    }
    if (canvas.hasPointerCapture(ev.pointerId)) canvas.releasePointerCapture(ev.pointerId);
  }
  canvas.addEventListener("pointerup", releasePointer);
  canvas.addEventListener("pointercancel", releasePointer);
  canvas.addEventListener("wheel", (ev) => {
    ev.preventDefault();
    zoomAt(screenPoint(ev), ev.deltaY < 0 ? 1.15 : .87);
  }, { passive: false });
  canvas.addEventListener("keydown", (ev) => {
    if (ev.key === "+" || ev.key === "=") { ev.preventDefault(); zoomAt({ x: W / 2, y: H / 2 }, 1.15); }
    else if (ev.key === "-") { ev.preventDefault(); zoomAt({ x: W / 2, y: H / 2 }, .87); }
    else if (ev.key === "0") { ev.preventDefault(); resetCamera(); }
    else if (["ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight"].includes(ev.key)) {
      ev.preventDefault();
      const amount = 36;
      if (ev.key === "ArrowUp") camera.y += amount;
      if (ev.key === "ArrowDown") camera.y -= amount;
      if (ev.key === "ArrowLeft") camera.x += amount;
      if (ev.key === "ArrowRight") camera.x -= amount;
      draw(performance.now());
    }
  });
  zoomOut.addEventListener("click", () => zoomAt({ x: W / 2, y: H / 2 }, .87));
  zoomIn.addEventListener("click", () => zoomAt({ x: W / 2, y: H / 2 }, 1.15));
  reset.addEventListener("click", resetCamera);
  return { redraw: draw, reset: resetCamera };
}
