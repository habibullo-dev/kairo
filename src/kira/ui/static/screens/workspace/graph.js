// Workspace › Graph tab (Phase 15) — the calm Memory Graph view: a focus node + its neighborhood as
// a canvas, with kind filters and a per-node inspect panel. Read/navigate ONLY — clicking a node
// shows its card + GET/navigate links (Trace / Artifacts / Costs) and a "Focus here" that re-centers;
// nothing here mutates. All text is textContent (el()/_util); the canvas is token-drawn (no assets).
import { el } from "../../ui/dom.js";
import { mountGraph } from "../../ui/graphview.js";
import { readMigrated, removeStored, writeStored } from "../../ui/storage.js";
import { chip, emptyState, row, section } from "./_util.js";

const KIND_ICON = {
  project: "📁", folder: "📂", run: "🧩", member: "◆", source: "📄", artifact: "🗎", memory: "🧠",
  task: "✓", team: "👥", wiki: "🔗", digest: "📰", person: "🧑", decision: "⚖️", topic: "🏷️",
};

export async function render(container, api, ctx) {
  container.textContent = "";
  const pid = ctx.projectId;
  // Saved view — last focus + kind filters, persisted to localStorage ONLY (like themes / office
  // layout). Include the immutable project creation timestamp so resetting a local database
  // cannot accidentally reapply a former project's filter to its newly reused numeric id.
  // No server route is involved, so the tab stays read-only.
  const stamp = String((ctx.project && ctx.project.created_at) || "unknown");
  const LKEY = `kira:graph:v4:${pid}:${stamp}`;
  const legacyLKey = `kairo:graph:v4:${pid}:${stamp}`;
  const pendingFocusKey = `kira:graph:focus:${pid}`;
  const legacyPendingFocusKey = `kairo:graph:focus:${pid}`;
  function loadState() {
    const pendingFocus = readMigrated(
      "session", pendingFocusKey, [legacyPendingFocusKey],
    );
    // A palette jump is one-time, never stale state. Once read into memory, retire either name.
    removeStored("session", [pendingFocusKey, legacyPendingFocusKey]);
    try {
      const r = JSON.parse(readMigrated("local", LKEY, [legacyLKey]) || "{}") || {};
      return { focus: pendingFocus || r.focus || null,
        kinds: new Set(Array.isArray(r.kinds) ? r.kinds : []),
        depth: [2, 4, 6].includes(r.depth) ? r.depth : 6,
        view: r.view === "dependencies" ? "dependencies" : "structure" };
    } catch {
      return { focus: pendingFocus, kinds: new Set(), depth: 6, view: "structure" };
    }
  }
  function saveState() {
    writeStored(
      "local",
      LKEY,
      JSON.stringify({
        focus: state.focus, kinds: [...state.kinds], view: state.view, depth: state.depth,
      }),
    );
  }
  const state = loadState();

  async function load() {
    const params = new URLSearchParams({ depth: String(state.depth), limit: "300", view: state.view });
    if (state.view === "structure" && state.focus) params.set("focus", state.focus);
    if (state.view === "structure" && state.kinds.size) params.set("kinds", [...state.kinds].join(","));
    const data = await api.get(`/api/workspace/${pid}/graph?` + params.toString());
    container.textContent = "";
    if (!data || !(data.nodes || []).length) {
      // A teaching empty state: WHAT the graph is + exactly HOW to populate it (copy the CLI
      // ritual). Read-only — the copy button touches only the clipboard, never the server.
      const copy = el("button", { class: "plain-button ghost" }, ["Copy: uv run kira graph rebuild"]);
      copy.addEventListener("click", () =>
        navigator.clipboard && navigator.clipboard.writeText("uv run kira graph rebuild"));
      const help = el("div", { class: "dim graph-empty-help" }, [
        "The graph links this project's chats, runs, artifacts, memory, sources and people into a "
        + "calm map you can explore and search. It's read-only — exploring it never changes your "
        + "data, and new memories still go through review before they appear."]);
      const dependencyEmpty = state.view === "dependencies";
      container.appendChild(section("Knowledge Graph", [
        emptyState(
          dependencyEmpty ? "No local code relationships yet" : "Nothing to graph yet",
          dependencyEmpty
            ? "Kira draws Code map links only for local imports it can resolve. Switch to Files to browse the project tree."
            : "Work in this project, then rebuild the derived links to populate the graph.",
        ),
        el("div", { class: "graph-empty-actions" }, [copy]),
        help,
      ]));
      return;
    }
    container.appendChild(buildHeader(data));
    const main = el("div", { class: "graph-main" });
    const canvasHost = el("div", { class: "graph-host" });
    const sideItems = [el("div", { class: "dim graph-side-intro" }, [
      "Click a node to highlight its relationships and inspect safe metadata.",
    ])];
    if (Array.isArray(data.communities) && data.communities.length) {
      sideItems.push(section("File groups", data.communities.slice(0, 12).map((community) =>
        row("●", community.name, `${community.count} file${community.count === 1 ? "" : "s"}`),
      )));
    }
    const side = el("div", { class: "graph-side" }, sideItems);
    main.append(canvasHost, side);
    container.appendChild(main);
    mountGraph(canvasHost, data, { onNode: (n) => showCard(side, n) });
  }

  function buildHeader(data) {
    const counts = (data.counts && data.counts.by_kind) || {};
    const chips = state.view === "structure" ? Object.keys(counts).sort().map((k) => {
      const on = state.kinds.has(k);
      const c = chip(`${KIND_ICON[k] || "•"} ${k} ${counts[k]}`, on ? "svc available" : "");
      c.style.cursor = "pointer";
      c.addEventListener("click", () => {
        if (on) state.kinds.delete(k); else state.kinds.add(k);
        saveState();
        load();
      });
      return c;
    }) : [];
    const viewControl = el("div", { class: "graph-view-toggle", role: "group", "aria-label": "Graph view" });
    for (const [view, label] of [["dependencies", "Code map"], ["structure", "Files"]]) {
      const active = state.view === view;
      const button = el("button", {
        class: active ? "plain-button active" : "plain-button ghost", type: "button",
        "aria-pressed": active ? "true" : "false",
      }, [label]);
      button.addEventListener("click", () => {
        if (state.view === view) return;
        state.view = view;
        state.focus = null;
        state.kinds.clear();
        saveState();
        load();
      });
      viewControl.appendChild(button);
    }
    const depthControl = el("div", {
      class: "graph-depth-toggle", role: "group", "aria-label": "Folder depth",
    });
    if (state.view === "structure") {
      for (const depth of [2, 4, 6]) {
        const active = state.depth === depth;
        const button = el("button", {
          class: active ? "plain-button active" : "plain-button ghost", type: "button",
          "aria-pressed": active ? "true" : "false",
        }, [`${depth} levels`]);
        button.addEventListener("click", () => {
          if (state.depth === depth) return;
          state.depth = depth;
          saveState();
          load();
        });
        depthControl.appendChild(button);
      }
    }
    const explainer = state.view === "dependencies"
      ? "Local import links show likely dependents. External packages and dynamic imports stay out of the map."
      : "Project folders and files are shown through six levels. Open a folder, then choose Focus here to explore that branch.";
    const head = el("div", { class: "graph-head" }, [
      el("div", { class: "graph-title-block" }, [
        el("h2", {}, ["Knowledge Graph"]),
        el("div", { class: "dim graph-explainer" }, [explainer]),
      ]),
      viewControl,
      ...(state.view === "structure" ? [depthControl] : []),
      el("div", { class: "graph-filters" }, chips),
    ]);
    if (state.view === "structure" && (state.focus || state.kinds.size)) {
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
    const browse = state.view === "dependencies";
    const focusBtn = el("button", { class: "plain-button" }, [browse ? "Browse location" : "Focus here"]);
    focusBtn.addEventListener("click", () => {
      if (browse) state.view = "structure";
      state.focus = node.id;
      saveState();
      load();
    });
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
