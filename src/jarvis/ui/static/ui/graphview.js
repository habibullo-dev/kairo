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
const NODE_CAP = 150; // the calm ceiling; the read model already bounds the subgraph

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
  const cx = w / 2, cy = h / 2, R = Math.min(w, h) * 0.36;
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
        const f = 1400 / d2;
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

function ringStroke(ctx, trust) {
  // never color-only: the ring STYLE also encodes trust (the side panel adds the text badge).
  if (trust === "untrusted_external") { ctx.setLineDash([3, 3]); return tok("--danger", "#ff7481"); }
  if (trust === "model_generated") { ctx.setLineDash([6, 3]); return tok("--attention", "#e0a840"); }
  ctx.setLineDash([]); return tok("--good", "#68d69c");
}

/** Mount a graph canvas into `host`. data = {nodes, edges, focus}. opts.onNode(node) on click. */
export function mountGraph(host, data, opts = {}) {
  host.textContent = "";
  const nodes = (data.nodes || []).slice(0, NODE_CAP).map((n) => ({ ...n }));
  const shown = new Set(nodes.map((n) => n.id));
  const edges = (data.edges || []).filter((e) => shown.has(e.src) && shown.has(e.dst));

  const wrap = el("div", { class: "graph-canvas-wrap" });
  const canvas = el("canvas", { class: "graph-canvas" });
  wrap.appendChild(canvas);
  if ((data.nodes || []).length > NODE_CAP) {
    wrap.appendChild(el("div", { class: "graph-cap dim" },
      [`Showing ${NODE_CAP} of ${data.nodes.length} nodes — filter or focus to narrow.`]));
  }
  host.appendChild(wrap);

  const dpr = window.devicePixelRatio || 1;
  const W = Math.max(320, host.clientWidth || 640), H = 520;
  canvas.width = W * dpr; canvas.height = H * dpr;
  canvas.style.width = W + "px"; canvas.style.height = H + "px";
  const ctx = canvas.getContext("2d");
  ctx.scale(dpr, dpr);

  const focus = data.focus;

  function draw() {
    ctx.clearRect(0, 0, W, H);
    ctx.lineWidth = 1;
    for (const e of edges) {  // edges under nodes
      const a = nodes.find((n) => n.id === e.src), b = nodes.find((n) => n.id === e.dst);
      if (!a || !b) continue;
      ctx.strokeStyle = tok("--line-strong", "rgba(200,200,200,.3)");
      ctx.setLineDash(e.origin === "asserted" ? [] : [2, 3]);
      ctx.beginPath(); ctx.moveTo(a.x, a.y); ctx.lineTo(b.x, b.y); ctx.stroke();
    }
    ctx.setLineDash([]);
    for (const n of nodes) {
      const r = n.id === focus ? 15 : 10 + Math.min(6, (n.degree || 0));
      ctx.beginPath(); ctx.arc(n.x, n.y, r, 0, Math.PI * 2);
      ctx.fillStyle = tok(KIND_TOKEN[n.kind] || "--muted", "#888");
      ctx.globalAlpha = 0.9; ctx.fill(); ctx.globalAlpha = 1;
      ctx.lineWidth = n.id === focus ? 3 : 2;
      ctx.strokeStyle = ringStroke(ctx, n.trust_class); ctx.stroke(); ctx.setLineDash([]);
      ctx.fillStyle = tok("--ink", "#eee"); ctx.font = "11px ui-sans-serif, system-ui";
      ctx.textAlign = "center";
      const label = (n.label || n.kind || "").slice(0, 22);
      ctx.fillText(label, n.x, n.y + r + 12);
    }
  }

  if (reducedMotion()) {
    layout(nodes, edges, W, H, 140);  // settle synchronously, draw once — no animation
    draw();
  } else {
    layout(nodes, edges, W, H, 0);    // seed positions
    let step = 0;
    const tick = () => {
      layout(nodes, edges, W, H, 6);  // a few iterations per frame
      draw();
      if (++step < 22 && document.body.contains(canvas)) requestAnimationFrame(tick);
    };
    requestAnimationFrame(tick);
  }

  canvas.addEventListener("click", (ev) => {
    const rect = canvas.getBoundingClientRect();
    const x = ev.clientX - rect.left, y = ev.clientY - rect.top;
    let best = null, bd = 18 * 18;
    for (const n of nodes) {
      const d = (n.x - x) ** 2 + (n.y - y) ** 2;
      if (d < bd) { bd = d; best = n; }
    }
    if (best && opts.onNode) opts.onNode(best);
  });
  return { redraw: draw };
}
