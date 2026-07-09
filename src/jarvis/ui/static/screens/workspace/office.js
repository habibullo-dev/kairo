// Workspace › Office tab (Phase 14) — the AI Team Office: a calm, render-only visual view over the
// existing orchestration system. Teams are rooms, members are status nodes, the workflow is a stage
// rail, Fable is the head "chair". It composes ONE assembler read model (/api/workspace/{id}/office)
// and (Task 3) patches live updates from the orchestration WS bus. It mints no authority: every
// action deep-links to or POSTs an already-enumerated Studio/orchestration route. The calm #studio
// timeline stays the app default; this is an opt-in tab. All text is set via el() text children
// (textContent), so any model/service/member string is inert. Compact mode (this task) is the
// default; the richer Office mode + toggle arrive in Task 4.
import { el } from "../../ui/dom.js";
import { money, relTime } from "../../ui/format.js";
import { chip, emptyState, section, statusPill } from "./_util.js";

const STAGE_LABEL = {
  council: "Council", synthesis: "Synthesis", execution: "Execution",
  review: "Review", verdict: "Verdict",
};
// Node live-status → a status-pill/ring tone token (never color-only: the text label rides along).
const NODE_TONE = { idle: "", running: "busy", ok: "good", denied: "attention", error: "danger" };
const FEED_ICON = { artifact: "📄", run: "🧩", chat: "💬" };
const FEED_CAP = 50; // the live feed is bounded; long history lives in recent_runs / the Activity tab

// The head "chair": Fable on the planner route = synthesis + final verdict (an engine stage, never a
// team member). Rendered distinct from the rooms.
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

// The canonical stage map as a slim rail; the in-flight run's stage (if any) is the active pip,
// earlier stages are "past", later ones "future".
function stageRail(stages, activeStage) {
  const at = activeStage ? stages.indexOf(activeStage) : -1;
  return el("div", { class: "stage-rail", role: "list", "aria-label": "Workflow stages" },
    stages.map((s, i) => {
      const state = at < 0 ? "future" : i < at ? "past" : i === at ? "active" : "future";
      return el("span", { class: `stage-pip ${state}`, role: "listitem" }, [STAGE_LABEL[s] || s]);
    }));
}

// One member status node: monogram + status ring, title, role·model·provider, tool/service chips,
// a text status label + cost. Everything is textContent-safe.
function memberNode(n) {
  const tone = NODE_TONE[n.status] || "";
  const initials = (n.title || n.role || "?").split(/\s+/).map((w) => w[0]).join("").slice(0, 2).toUpperCase();
  const chips = [
    ...(n.tools || []).map((t) => chip(t, "tool")),
    ...(n.services || []).map((s) => chip(s.name, "svc " + (s.state || "unknown"))),
  ];
  return el("div", { class: "office-node", tabindex: "0" }, [
    el("div", { class: "node-ring " + (tone || "idle") }, [
      el("span", { class: "node-mono" }, [initials || "?"]),
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
}

// A team room: header (icon + name + "N members · [stage · $cost] for the live team") + node grid.
function room(r, live) {
  const isLive = live && live.team === r.team;
  const bits = [`${(r.nodes || []).length} members`];
  if (isLive) {
    if (live.stage) bits.push(STAGE_LABEL[live.stage] || live.stage);
    bits.push(money(live.actual_cost_usd));
  }
  const card = el("div", {
    class: "room-card" + (isLive ? " live" : ""), role: "region",
    "aria-label": `${r.name || r.team} team`,
  }, [
    el("div", { class: "room-head" }, [
      el("span", { class: "room-icon" }, [r.icon || "•"]),
      el("div", { class: "room-headtext" }, [
        el("div", { class: "room-name" }, [r.name || r.team]),
        el("div", { class: "room-sub dim" }, [bits.join(" · ")]),
      ]),
    ]),
    el("div", { class: "node-grid" }, (r.nodes || []).map(memberNode)),
  ]);
  if (r.accent) card.style.setProperty("--room-accent", r.accent);
  return card;
}

// The right/bottom activity feed — short metadata rows only (never a body/prompt/key).
function feedColumn(feed) {
  const rows = (feed || []).slice(0, FEED_CAP).map((e) =>
    el("div", { class: "feed-row" }, [
      el("span", { class: "feed-icon" }, [FEED_ICON[e.type] || "•"]),
      el("div", { class: "feed-mid" }, [
        el("div", { class: "feed-title" }, [e.title || "(event)"]),
        el("div", { class: "feed-sub dim" }, [
          `${e.type || ""}${e.status ? " · " + e.status : ""} · ${relTime(e.ts)}`,
        ]),
      ]),
    ]));
  return el("div", { class: "office-feed", "aria-live": "polite", "aria-label": "Live activity" },
    rows.length ? rows : [emptyState("Quiet", "Live agent activity will stream here.")]);
}

// The live summary strip for the in-flight/last run (team · workflow · status · stage · cost).
function liveStrip(live) {
  if (!live) return null;
  const parts = [live.team, live.workflow, live.status, live.stage && (STAGE_LABEL[live.stage] || live.stage)]
    .filter(Boolean).join(" · ");
  return el("div", { class: "office-live" }, [
    el("span", { class: "live-dot" }, []),
    el("div", { class: "live-text" }, [live.title || "(run)"]),
    el("div", { class: "live-meta dim mono" }, [
      `${parts} · ${money(live.actual_cost_usd)} / ${money(live.estimated_cost_usd)}`,
    ]),
  ]);
}

function build(data) {
  const stages = data.stages || [];
  const live = data.live || null;
  const rooms = (data.rooms || []).map((r) => room(r, live));
  const recent = (data.recent_runs || []).map((rn) =>
    el("div", { class: "office-recent-row" }, [
      el("span", {}, [(rn.team || "") + " · " + (rn.workflow || "")]),
      statusPill(rn.verdict || rn.status || "—"),
      el("span", { class: "mono dim" }, [money(rn.actual_cost_usd)]),
    ]));

  const floor = el("div", { class: "office-floor" },
    rooms.length ? rooms : [emptyState("No teams", "This project's teams will appear here as rooms.")]);
  const side = el("div", { class: "office-side" }, [
    section("Live activity", [feedColumn(data.feed)]),
    section("Recent runs", recent.length ? recent : [emptyState("None yet", "Completed runs show here.")]),
  ]);

  return el("div", { class: "office office-compact", "data-office-root": "1" }, [
    el("div", { class: "office-head" }, [
      el("div", { class: "office-title" }, [
        el("h2", {}, ["Team Office"]),
        el("div", { class: "sub dim" }, ["A calm view of this project's teams, stages, and activity."]),
      ]),
      headChair(data.head),
    ]),
    stageRail(stages, live && live.stage),
    liveStrip(live),
    el("div", { class: "office-main" }, [floor, side]),
  ]);
}

export async function render(container, api, ctx) {
  container.textContent = "";
  const data = await api.get("/api/workspace/" + ctx.projectId + "/office");
  if (!data) {
    container.appendChild(emptyState("Unavailable", "Couldn't load the Office — it'll refresh shortly."));
    return;
  }
  container.appendChild(build(data));
}
