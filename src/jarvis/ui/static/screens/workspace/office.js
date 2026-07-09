// Workspace › Office tab (Phase 14) — the AI Team Office: a calm, render-only visual view over the
// existing orchestration system. Teams are rooms, members are status nodes, the workflow is a stage
// rail, Fable is the head "chair". It composes ONE assembler read model (/api/workspace/{id}/office)
// and patches live updates from the orchestration WS bus (a module-singleton listener guarded on the
// office root's DOM presence — surgical repaint of only the affected stage/room/node/feed, never a
// full re-render or refreshIfActive). It mints no authority: "Launch" deep-links to #studio, "Cancel"
// POSTs the existing /api/orchestration/{id}/cancel, approvals surface through the app's global amber
// overlay, and inspect only navigates (GET) to Trace/Artifacts/Costs. The calm #studio timeline stays
// the app default; this is an opt-in tab. All text is set via el() text children (textContent), so any
// model/service/member string is inert. Compact mode is the default; Office mode + toggle arrive next.
import { el } from "../../ui/dom.js";
import { money, relTime } from "../../ui/format.js";
import { on as busOn } from "../../ui/bus.js";
import { pushEscape } from "../../ui/keys.js";
import { actionButton, chip, emptyState, section, statusPill } from "./_util.js";

const STAGE_LABEL = {
  council: "Council", synthesis: "Synthesis", execution: "Execution",
  review: "Review", verdict: "Verdict",
};
// Node live-status → a status-pill/ring tone token (never color-only: the text label rides along).
const NODE_TONE = { idle: "", running: "busy", ok: "good", denied: "attention", error: "danger" };
const FEED_ICON = { artifact: "📄", run: "🧩", chat: "💬", agent: "◆", stage: "▸", done: "✓" };
const FEED_CAP = 50; // the live feed is bounded; long history lives in recent_runs / the Activity tab

// Module-singleton mount state: the CURRENT office instance the bus handler patches, or null when
// the tab is not open. Guarding on this (+ the live root) keeps the once-registered listener inert
// after a tab switch and prevents stale-DOM writes.
let _mounted = null; // { projectId, api, live, runId, root }

// View mode: "compact" (default — dense, information-first) or "office" (the richer operations
// floor). Same data + DOM; only the root class differs, so the toggle is a pure CSS relayout (no
// re-render, no refetch). Session-scoped here; per-project persistence arrives in Task 6. The
// Office is never the app default — #studio stays home; this only picks the *tab's* inner layout.
let _mode = "compact";
const rootClass = () => "office " + (_mode === "office" ? "office-full" : "office-compact");

// Per-project view layout, persisted to localStorage ONLY (like ui/theme.js — appearance is
// client-side; there is deliberately NO server route, no new authority, the mutation-route set is
// unchanged). Blob: { mode, collapsed:[team] }. Values are clamped on read (localStorage is
// same-origin + user-writable, so never trust its shape).
const _LKEY = (pid) => `kairo:office:${pid}`;
function loadLayout(pid) {
  try {
    const raw = JSON.parse(localStorage.getItem(_LKEY(pid)) || "{}") || {};
    const mode = raw.mode === "office" ? "office" : "compact"; // clamp to a known mode
    const collapsed = Array.isArray(raw.collapsed)
      ? raw.collapsed.filter((t) => typeof t === "string") : [];
    return { mode, collapsed: new Set(collapsed) };
  } catch {
    return { mode: "compact", collapsed: new Set() };
  }
}
function saveLayout() {
  if (!_mounted) return;
  try {
    localStorage.setItem(_LKEY(_mounted.projectId), JSON.stringify(
      { mode: _mode, collapsed: [..._mounted.collapsed] }));
  } catch { /* storage disabled — layout falls back to defaults next open */ }
}

const initials = (s) =>
  (s || "?").split(/\s+/).map((w) => w[0] || "").join("").slice(0, 2).toUpperCase() || "?";

// The head "chair": Fable on the planner route = synthesis + final verdict (an engine stage, never a
// team member).
function headChair(head) {
  const route = head && (head.model || head.provider)
    ? `${head.model || "—"} · ${head.provider || "—"}` : "unconfigured";
  return el("div", { class: "office-chair" }, [
    el("span", { class: "chair-badge" }, [(head && head.label) || "Fable"]),
    el("div", { class: "chair-meta" }, [
      el("div", { class: "chair-role" }, ["Head · synthesis + verdict"]),
      el("div", { class: "chair-route mono dim" }, [route]),
    ]),
  ]);
}

function pipState(stages, activeStage, i) {
  const at = activeStage ? stages.indexOf(activeStage) : -1;
  if (at < 0) return "future";
  return i < at ? "past" : i === at ? "active" : "future";
}

function stageRail(stages, activeStage) {
  return el("div", { class: "stage-rail", role: "list", "aria-label": "Workflow stages" },
    stages.map((s, i) =>
      el("span", {
        class: `stage-pip ${pipState(stages, activeStage, i)}`, role: "listitem",
        dataset: { stage: s },
      }, [STAGE_LABEL[s] || s])));
}

// One member status node. Clicking (or Enter/Space) opens the inspect drawer. Carries data-node-*
// attributes so a live agent event can find + repaint it without a stale ref map.
function memberNode(n, roomTeam) {
  const tone = NODE_TONE[n.status] || "";
  const chips = [
    ...(n.tools || []).map((t) => chip(t, "tool")),
    ...(n.services || []).map((s) => chip(s.name, "svc " + (s.state || "unknown"))),
  ];
  const node = el("div", {
    class: "office-node", tabindex: "0", role: "button",
    "aria-label": `${n.title || n.role || "member"} — inspect`,
    dataset: { nodeTeam: roomTeam, nodeRole: n.role || "" },
  }, [
    el("div", { class: "node-ring " + (tone || "idle"), dataset: { ring: "1" } }, [
      el("span", { class: "node-mono" }, [initials(n.title || n.role)]),
    ]),
    el("div", { class: "node-body" }, [
      el("div", { class: "node-title" }, [n.title || "(member)"]),
      el("div", { class: "node-route mono dim" }, [
        `${n.role || "—"} · ${n.model || "—"}·${n.provider || "—"}`,
      ]),
      chips.length ? el("div", { class: "node-chips" }, chips) : null,
      el("div", { class: "node-foot" }, [
        statusPill(n.status || "idle", tone),
        el("span", { class: "node-cost mono dim" }, [money(n.cost_usd)]),
      ]),
    ]),
  ]);
  const open = () => openInspect(n);
  node.addEventListener("click", open);
  node.addEventListener("keydown", (ev) => {
    if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); open(); }
  });
  return node;
}

function roomSub(r, live) {
  const bits = [`${(r.nodes || []).length} members`];
  if (live && live.team === r.team) {
    if (live.stage) bits.push(STAGE_LABEL[live.stage] || live.stage);
    bits.push(money(live.actual_cost_usd));
  }
  return bits.join(" · ");
}

function room(r, live, collapsed) {
  const isLive = live && live.team === r.team;
  const isCollapsed = !!(collapsed && collapsed.has(r.team));
  const caret = el("button", {
    class: "room-caret", "aria-expanded": isCollapsed ? "false" : "true",
    "aria-label": `Toggle ${r.name || r.team} room`,
  }, [isCollapsed ? "▸" : "▾"]);
  const card = el("div", {
    class: "room-card" + (isLive ? " live" : "") + (isCollapsed ? " collapsed" : ""),
    role: "region", "aria-label": `${r.name || r.team} team`, dataset: { room: r.team },
  }, [
    el("div", { class: "room-head" }, [
      el("span", { class: "room-icon" }, [r.icon || "•"]),
      el("div", { class: "room-headtext" }, [
        el("div", { class: "room-name" }, [r.name || r.team]),
        el("div", { class: "room-sub dim", dataset: { roomSub: "1" } }, [roomSub(r, live)]),
      ]),
      caret,
    ]),
    el("div", { class: "node-grid" }, (r.nodes || []).map((n) => memberNode(n, r.team))),
  ]);
  caret.addEventListener("click", () => toggleRoomCollapse(r.team, card, caret));
  if (r.accent) card.style.setProperty("--room-accent", r.accent);
  return card;
}

// Collapse/expand a room — persisted per project to localStorage (no server route).
function toggleRoomCollapse(team, card, caret) {
  if (!_mounted) return;
  const nowCollapsed = !card.classList.contains("collapsed");
  card.classList.toggle("collapsed", nowCollapsed);
  caret.textContent = nowCollapsed ? "▸" : "▾";
  caret.setAttribute("aria-expanded", nowCollapsed ? "false" : "true");
  if (nowCollapsed) _mounted.collapsed.add(team);
  else _mounted.collapsed.delete(team);
  saveLayout();
}

function feedRow(e) {
  return el("div", { class: "feed-row" }, [
    el("span", { class: "feed-icon" }, [FEED_ICON[e.type] || "•"]),
    el("div", { class: "feed-mid" }, [
      el("div", { class: "feed-title" }, [e.title || "(event)"]),
      el("div", { class: "feed-sub dim" }, [
        `${e.type || ""}${e.status ? " · " + e.status : ""} · ${relTime(e.ts)}`,
      ]),
    ]),
  ]);
}

function liveStrip(live, api) {
  if (!live) return el("div", { class: "office-live idle", dataset: { officeLive: "1" } }, [
    el("span", { class: "live-dot idle" }, []),
    el("div", { class: "live-text dim" }, ["No run in flight — launch one from Studio."]),
  ]);
  const parts = [live.team, live.workflow, live.status,
    live.stage && (STAGE_LABEL[live.stage] || live.stage)].filter(Boolean).join(" · ");
  const kids = [
    el("span", { class: "live-dot " + (live.status === "running" ? "on" : "") }, []),
    el("div", { class: "live-text" }, [live.title || "(run)"]),
    el("div", { class: "live-meta dim mono" }, [
      `${parts} · ${money(live.actual_cost_usd)} / ${money(live.estimated_cost_usd)}`,
    ]),
  ];
  if (live.status === "running" && (live.id != null) && api) {
    kids.push(actionButton("Cancel", () => cancelRun(api, live.id), "danger"));
  }
  return el("div", { class: "office-live", dataset: { officeLive: "1" } }, kids);
}

// --- actions (existing routes only) ---------------------------------------
async function cancelRun(api, runId) {
  try { await api.post(`/api/orchestration/${runId}/cancel`, {}); } catch { /* WS reflects state */ }
}

// --- inspect drawer (GET/navigate only) -----------------------------------
let _closeInspect = null;
function openInspect(n) {
  if (_closeInspect) _closeInspect();
  const root = _mounted && _mounted.root;
  if (!root) return;
  const pid = _mounted.projectId;
  const links = [
    ["Trace", "#trace"], ["Artifacts", `#workspace/${pid}/artifacts`], ["Costs", `#workspace/${pid}/costs`],
  ].map(([label, href]) => el("a", { href, class: "plain-button ghost" }, [label]));
  const chips = [
    ...(n.tools || []).map((t) => chip(t, "tool")),
    ...(n.services || []).map((s) => chip(s.name, "svc " + (s.state || "unknown"))),
  ];
  const closeBtn = el("button", { class: "icon-button", "aria-label": "Close" }, ["✕"]);
  const drawer = el("div", { class: "office-inspect", role: "dialog", "aria-label": "Member details" }, [
    el("div", { class: "inspect-head" }, [
      el("div", { class: "inspect-title" }, [n.title || "(member)"]), closeBtn,
    ]),
    el("div", { class: "inspect-route mono dim" }, [
      `${n.role || "—"} · ${n.model || "—"}·${n.provider || "—"} · ${n.capability || ""}`,
    ]),
    el("div", { class: "inspect-foot" }, [
      statusPill(n.status || "idle", NODE_TONE[n.status] || ""),
      el("span", { class: "mono dim" }, [`stage ${n.stage || "—"} · ${money(n.cost_usd)}`]),
    ]),
    chips.length ? el("div", { class: "node-chips" }, chips) : null,
    el("div", { class: "inspect-links" }, links),
  ]);
  const close = () => {
    drawer.remove();
    if (_off) _off();
    _closeInspect = null;
  };
  closeBtn.addEventListener("click", close);
  const _off = pushEscape(close);
  _closeInspect = close;
  root.appendChild(drawer);
  closeBtn.focus();
}

// --- live bus patching (surgical; guarded on mount) -----------------------
function officeRoot() {
  const root = document.querySelector('[data-office-root]');
  return root && _mounted && root === _mounted.root ? root : null;
}

function repaintStages(root, activeStage) {
  const stages = _mounted.live && _mounted.stages ? _mounted.stages : [];
  root.querySelectorAll(".stage-pip").forEach((pip) => {
    const i = stages.indexOf(pip.dataset.stage);
    pip.className = `stage-pip ${pipState(stages, activeStage, i)}`;
  });
}

function repaintLive(root) {
  const strip = root.querySelector('[data-office-live]');
  if (strip) strip.replaceWith(liveStrip(_mounted.live, _mounted.api));
}

function repaintRoom(root, team, live) {
  const card = root.querySelector(`[data-room="${CSS.escape(team)}"]`);
  if (!card) return;
  card.classList.toggle("live", !!(live && live.team === team));
  const sub = card.querySelector('[data-room-sub]');
  const r = (_mounted.rooms || []).find((x) => x.team === team);
  if (sub && r) sub.textContent = roomSub(r, live);
}

function repaintAgentNodes(root, team, role, status, stage) {
  const tone = NODE_TONE[status] || "";
  root.querySelectorAll(
    `[data-node-team="${CSS.escape(team)}"][data-node-role="${CSS.escape(role || "")}"]`
  ).forEach((node) => {
    const ring = node.querySelector('[data-ring]');
    if (ring) ring.className = "node-ring " + (tone || "idle");
    const pill = node.querySelector(".status-pill");
    if (pill) { pill.textContent = status || "idle"; pill.className = "status-pill" + (tone ? " " + tone : ""); }
    node.title = `${role} · stage ${stage || "—"}`;
  });
}

function pushFeed(root, e) {
  const feed = root.querySelector('[data-office-feed]');
  if (!feed) return;
  const empty = feed.querySelector(".empty-state");
  if (empty) empty.remove();
  feed.insertBefore(feedRow(e), feed.firstChild);
  while (feed.children.length > FEED_CAP) feed.removeChild(feed.lastChild);
}

// Coalesced repaint (perf): a bus event MUTATES the model + marks what changed here; the actual DOM
// repaint is batched into one requestAnimationFrame, so a burst of N events is a single relayout,
// not N. Every pending structure is BOUNDED — rooms (Set ≤ teams), nodes (Map ≤ members, keyed so
// repeats collapse), feed (array trimmed to FEED_CAP on queue) — so nothing grows without limit even
// if a flush is delayed (e.g. a backgrounded tab). Feed history beyond the cap lives in recent_runs.
const _pending = { stages: false, live: false, rooms: new Set(), nodes: new Map(), feed: [] };
let _rafId = 0;
function queueFeed(e) {
  _pending.feed.push(e);
  if (_pending.feed.length > FEED_CAP) _pending.feed.shift(); // bound the buffer, not just the DOM
}
function scheduleFlush() {
  if (_rafId) return;
  _rafId = requestAnimationFrame(flushPending);
}
function flushPending() {
  _rafId = 0;
  const root = officeRoot();
  const p = _pending;
  if (root && _mounted) {
    if (p.stages) repaintStages(root, _mounted.live && _mounted.live.stage);
    if (p.live) repaintLive(root);
    p.rooms.forEach((t) => repaintRoom(root, t, _mounted.live));
    p.nodes.forEach((n) => repaintAgentNodes(root, n.team, n.role, n.status, n.stage));
    p.feed.forEach((e) => pushFeed(root, e));
  }
  p.stages = false; p.live = false; p.rooms.clear(); p.nodes.clear(); p.feed.length = 0;
}

// The ONE registered handler (module singleton). Inert unless the office is mounted; only touches
// the model + the bounded pending buffers, then schedules the coalesced flush.
function onOrchestration(msg) {
  if (!officeRoot() || !msg || !msg.kind) return;
  const k = msg.kind;
  if (k === "orchestration_started") {
    _mounted.runId = msg.run_id;
    _mounted.live = {
      id: msg.run_id, team: msg.team, workflow: msg.workflow, title: msg.title,
      stage: null, status: "running", estimated_cost_usd: msg.estimated_cost_usd,
      actual_cost_usd: null,
    };
    _pending.live = true; _pending.stages = true; _pending.rooms.add(msg.team);
    queueFeed({ type: "run", title: msg.title || "run started", status: "running", ts: msg.ts });
    return scheduleFlush();
  }
  if (!_mounted.live || msg.run_id !== _mounted.runId) return; // only the in-flight run
  if (k === "orchestration_stage") {
    _mounted.live.stage = msg.stage;
    _pending.stages = true; _pending.live = true; _pending.rooms.add(_mounted.live.team);
    queueFeed({ type: "stage", title: `Stage · ${STAGE_LABEL[msg.stage] || msg.stage}`, ts: msg.ts });
  } else if (k === "orchestration_agent") {
    _pending.nodes.set(`${msg.team}::${msg.role}`,
      { team: msg.team, role: msg.role, status: msg.ok ? "ok" : "denied", stage: msg.stage });
    queueFeed({
      type: "agent", status: msg.ok ? "ok" : "denied", ts: msg.ts,
      title: `${msg.member || msg.role || "member"} · ${STAGE_LABEL[msg.stage] || msg.stage || ""}`,
    });
  } else if (k === "orchestration_round") {
    _mounted.live.stage = "verdict"; _pending.stages = true;
    queueFeed({ type: "stage", title: `Verdict round ${msg.round}`, status: msg.verdict, ts: msg.ts });
  } else if (k === "orchestration_completed") {
    _mounted.live.status = msg.status; _mounted.live.verdict = msg.verdict;
    _pending.live = true; _pending.rooms.add(_mounted.live.team);
    queueFeed({ type: "done", title: "Run complete", status: msg.verdict || msg.status, ts: msg.ts });
  }
  scheduleFlush();
}
busOn("orchestration", onOrchestration); // registered once per module load (import is cached)

// --- view mode toggle (Compact | Office) ----------------------------------
function setMode(mode) {
  _mode = mode === "office" ? "office" : "compact";
  const root = _mounted && _mounted.root;
  if (!root) return;
  root.className = rootClass(); // pure relayout — CSS handles the two arrangements
  root.querySelectorAll(".mode-btn").forEach((b) => {
    const on = b.dataset.mode === _mode;
    b.classList.toggle("active", on);
    b.setAttribute("aria-pressed", on ? "true" : "false");
  });
  saveLayout(); // remember the view mode per project
}

function modeToggle() {
  const mk = (mode, label) => {
    const b = el("button", {
      class: "mode-btn" + (_mode === mode ? " active" : ""),
      "aria-pressed": _mode === mode ? "true" : "false", dataset: { mode },
    }, [label]);
    b.addEventListener("click", () => setMode(mode));
    return b;
  };
  return el("div", { class: "office-modes", role: "group", "aria-label": "Office view mode" }, [
    mk("compact", "Compact"), mk("office", "Office"),
  ]);
}

// Roving keyboard navigation: arrow keys move focus between member nodes (they are role="button",
// tabindex 0 — Enter/Space open inspect). Only active once a node holds focus, so it never hijacks
// the arrow keys elsewhere. Attached to the freshly-built floor each render (no accumulation).
function onFloorKeys(ev) {
  if (!["ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight"].includes(ev.key)) return;
  const root = officeRoot();
  if (!root) return;
  const nodes = [...root.querySelectorAll(".office-node")];
  const i = nodes.indexOf(document.activeElement);
  if (i < 0) return; // rove only when a node is focused
  ev.preventDefault();
  const fwd = ev.key === "ArrowDown" || ev.key === "ArrowRight";
  const next = nodes[(i + (fwd ? 1 : -1) + nodes.length) % nodes.length];
  if (next) next.focus();
}

// --- build + render -------------------------------------------------------
function build(data, api, collapsed) {
  const stages = data.stages || [];
  const live = data.live || null;
  const rooms = (data.rooms || []).map((r) => room(r, live, collapsed));
  const recent = (data.recent_runs || []).map((rn) =>
    el("div", { class: "office-recent-row" }, [
      el("span", {}, [(rn.team || "") + " · " + (rn.workflow || "")]),
      statusPill(rn.verdict || rn.status || "—"),
      el("span", { class: "mono dim" }, [money(rn.actual_cost_usd)]),
    ]));
  const feedRows = (data.feed || []).slice(0, FEED_CAP).map(feedRow);

  const floor = el("div", { class: "office-floor", onkeydown: onFloorKeys },
    rooms.length ? rooms : [emptyState("No teams", "This project's teams will appear here as rooms.")]);
  const side = el("div", { class: "office-side" }, [
    section("Live activity", [
      el("div", { class: "office-feed", "aria-live": "polite", "aria-label": "Live activity",
        dataset: { officeFeed: "1" } },
      feedRows.length ? feedRows : [emptyState("Quiet", "Live agent activity will stream here.")]),
    ]),
    section("Recent runs", recent.length ? recent : [emptyState("None yet", "Completed runs show here.")]),
  ]);

  return el("div", { class: rootClass(), dataset: { officeRoot: "1" } }, [
    el("div", { class: "office-head" }, [
      el("div", { class: "office-title" }, [
        el("h2", {}, ["Team Office"]),
        el("div", { class: "sub dim" }, ["A calm view of this project's teams, stages, and activity."]),
      ]),
      modeToggle(),
      el("a", { href: "#studio", class: "plain-button", "aria-label": "Launch a run in Studio" },
        ["Launch in Studio"]),
    ]),
    // The workflow as a calm flow terminating at Fable's "chair" (synthesis + verdict engine stage).
    el("div", { class: "office-flow" }, [stageRail(stages, live && live.stage), headChair(data.head)]),
    liveStrip(live, api),
    el("div", { class: "office-main" }, [floor, side]),
  ]);
}

export async function render(container, api, ctx) {
  container.textContent = "";
  if (_closeInspect) _closeInspect();
  const data = await api.get("/api/workspace/" + ctx.projectId + "/office");
  if (!data) {
    container.appendChild(emptyState("Unavailable", "Couldn't load the Office — it'll refresh shortly."));
    _mounted = null;
    return;
  }
  const layout = loadLayout(ctx.projectId);
  _mode = layout.mode; // restore the per-project view mode BEFORE build (the root class reads it)
  const root = build(data, api, layout.collapsed);
  container.appendChild(root);
  _mounted = {
    projectId: ctx.projectId, api, root,
    live: data.live || null, runId: data.live ? data.live.id : null,
    stages: data.stages || [], rooms: data.rooms || [], collapsed: layout.collapsed,
  };
}
