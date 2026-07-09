// Workspace › Graph tab (Phase 15) — the calm Memory Graph view: a focus node + its neighborhood as
// a canvas, with kind filters and a per-node inspect panel. Read/navigate ONLY — clicking a node
// shows its card + GET/navigate links (Trace / Artifacts / Costs) and a "Focus here" that re-centers;
// nothing here mutates. All text is textContent (el()/_util); the canvas is token-drawn (no assets).
import { el } from "../../ui/dom.js";
import { mountGraph } from "../../ui/graphview.js";
import { chip, emptyState, row, section } from "./_util.js";

const KIND_ICON = {
  project: "📁", run: "🧩", member: "◆", source: "📄", artifact: "🗎", memory: "🧠",
  task: "✓", team: "👥", wiki: "🔗", digest: "📰", person: "🧑", decision: "⚖️", topic: "🏷️",
};

export async function render(container, api, ctx) {
  container.textContent = "";
  const pid = ctx.projectId;
  // Saved view — last focus + kind filters, persisted to localStorage ONLY (like themes / office
  // layout). No server route, so the tab stays read-only.
  const LKEY = `kairo:graph:${pid}`;
  function loadState() {
    try {
      const r = JSON.parse(localStorage.getItem(LKEY) || "{}") || {};
      return { focus: r.focus || null, kinds: new Set(Array.isArray(r.kinds) ? r.kinds : []),
               depth: 1 };
    } catch {
      return { focus: null, kinds: new Set(), depth: 1 };
    }
  }
  function saveState() {
    try {
      localStorage.setItem(LKEY, JSON.stringify({ focus: state.focus, kinds: [...state.kinds] }));
    } catch { /* storage disabled — the view just isn't remembered */ }
  }
  const state = loadState();

  async function load() {
    const params = new URLSearchParams({ depth: String(state.depth), limit: "150" });
    if (state.focus) params.set("focus", state.focus);
    if (state.kinds.size) params.set("kinds", [...state.kinds].join(","));
    const data = await api.get(`/api/workspace/${pid}/graph?` + params.toString());
    container.textContent = "";
    if (!data || !(data.nodes || []).length) {
      container.appendChild(section("Knowledge Graph", [emptyState(
        "Nothing to graph yet",
        "Run `jarvis graph rebuild` (or work in this project) to populate the graph.")]));
      return;
    }
    container.appendChild(buildHeader(data));
    const main = el("div", { class: "graph-main" });
    const canvasHost = el("div", { class: "graph-host" });
    const side = el("div", { class: "graph-side" }, [
      el("div", { class: "dim", style: "padding:12px" }, ["Click a node to inspect it."])]);
    main.append(canvasHost, side);
    container.appendChild(main);
    mountGraph(canvasHost, data, { onNode: (n) => showCard(side, n) });
  }

  function buildHeader(data) {
    const counts = (data.counts && data.counts.by_kind) || {};
    const chips = Object.keys(counts).sort().map((k) => {
      const on = state.kinds.has(k);
      const c = chip(`${KIND_ICON[k] || "•"} ${k} ${counts[k]}`, on ? "svc available" : "");
      c.style.cursor = "pointer";
      c.addEventListener("click", () => {
        if (on) state.kinds.delete(k); else state.kinds.add(k);
        saveState();
        load();
      });
      return c;
    });
    const head = el("div", { class: "graph-head" }, [
      el("h2", {}, ["Knowledge Graph"]),
      el("div", { class: "graph-filters" }, chips),
    ]);
    if (state.focus || state.kinds.size) {
      const reset = el("button", { class: "plain-button ghost" }, ["Reset view"]);
      reset.addEventListener("click", () => {
        state.focus = null; state.kinds.clear(); saveState(); load();
      });
      head.appendChild(reset);
    }
    return head;
  }

  async function showCard(side, node) {
    side.textContent = "";
    const card = await api.get(`/api/graph/node/${node.kind}/${node.ref_id}`);
    const trust = (card && card.trust_class) || node.trust_class;
    const links = [
      ["Trace", "#trace"], ["Artifacts", `#workspace/${pid}/artifacts`],
      ["Costs", `#workspace/${pid}/costs`],
    ].map(([t, href]) => el("a", { href, class: "plain-button ghost" }, [t]));
    const focusBtn = el("button", { class: "plain-button" }, ["Focus here"]);
    focusBtn.addEventListener("click", () => { state.focus = node.id; saveState(); load(); });
    const neighbors = ((card && card.neighbors) || []).slice(0, 12).map((nb) =>
      row(KIND_ICON[nb.node.kind] || "•", nb.node.label, `${nb.direction} · ${nb.edge_kind}`));
    side.append(
      el("div", { class: "graph-card" }, [
        el("div", { class: "graph-card-title" }, [node.label || node.kind]),
        el("div", { class: "mono dim" }, [`${node.kind} · ${trust}`]),
        chip(trust, trust === "untrusted_external" ? "svc missing_credentials" : "svc available"),
        el("div", { class: "graph-card-actions" }, [focusBtn, ...links]),
      ]),
      section("Connected", neighbors.length ? neighbors : [el("div", { class: "dim" }, ["(none)"])]),
    );
  }

  await load();
}
