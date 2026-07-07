// Studio — the AI Orchestration Studio (Phase 10B). Pick a team + workflow, describe the task,
// preview the worst-case cost, and launch a run. Everything here is a VIEW + a click: the
// engine enforces the read-only council/review floor, the single writer under the turn lock,
// the budget reservation, and the two-step confirm server-side. No key, prompt, or report body
// ever reaches this screen — only metadata, summaries, and manifests.

const S = {
  catalog: null,     // { teams, workflows, services, model_routes, active_project_id, busy }
  runs: [],          // recent orchestration runs (summaries)
  team: null,        // selected team id
  workflow: null,    // selected workflow id
  estimate: null,    // last previewed estimate
  live: null,        // { run_id, team, workflow, title, stage, agents:[], status, verdict }
  detail: null,      // an expanded run detail { run, members }
};

export async function render(container, api) {
  _api = api;
  const [cat, hist] = await Promise.all([api.get("/api/studio"), api.get("/api/orchestration")]);
  if (!cat) {
    container.innerHTML = `<div class="rise"><h1>Studio</h1>
      <div class="sub">Orchestration is not available (delegation/sub-agents off).</div></div>`;
    return;
  }
  S.catalog = cat;
  S.runs = (hist && hist.runs) || [];
  S.team = S.team || (cat.teams[0] && cat.teams[0].id);
  S.workflow = S.workflow || defaultWorkflow(cat, S.team);

  const routeModel = mapRoutes(cat.model_routes);
  const svcState = mapServices(cat.services);
  const team = cat.teams.find((t) => t.id === S.team) || cat.teams[0];

  container.innerHTML = `
    <div class="rise studio-head">
      <h1>Orchestration Studio</h1>
      <div class="sub">A project team runs a workflow: council → synthesis → (execution) →
        review → verdict. Read-only members fan out; one writer runs under the turn lock.</div>
    </div>
    ${cat.active_project_id == null
      ? `<div class="card warn rise">Teams are project-scoped — select a project first.</div>`
      : ""}
    <div class="studio-grid">
      <div class="card rise">
        <div class="card-label">Team</div>
        <select id="st-team">${team ? cat.teams.map((t) =>
          `<option value="${t.id}" ${t.id === S.team ? "selected" : ""}>${esc(t.icon)} ${esc(t.name)}</option>`
        ).join("") : ""}</select>
        <div class="dim" style="margin:.3rem 0 .6rem">${esc(team ? team.description : "")}</div>
        <div class="card-label">Workflow</div>
        <select id="st-workflow">${workflowOptions(cat, team)}</select>
        <div class="card-label" style="margin-top:.6rem">Task brief</div>
        <textarea id="st-task" rows="4" placeholder="What should this team do?"></textarea>
        <div class="studio-run-row">
          <input id="st-budget" type="number" step="0.01" min="0" placeholder="per-run $ (optional)">
          <button id="st-estimate">Estimate</button>
          <button id="st-run" class="btn-approve">Run</button>
        </div>
        <div id="st-estimate-panel"></div>
      </div>
      <div class="card rise">
        <div class="card-label">Roster · ${esc(team ? team.name : "")}</div>
        ${team ? team.members.map((m) => memberCard(m, routeModel, svcState)).join("") : ""}
        <div class="dim mono" style="margin-top:.5rem">Head synthesis + verdict:
          ${esc(routeModel["planner"] || "planner")} (Fable)</div>
      </div>
    </div>
    <div id="st-live"></div>
    <div class="card rise">
      <div class="card-label">Recent runs</div>
      <table><tbody id="st-runs">${runsRows(S.runs)}</tbody></table>
    </div>
    <div id="st-detail"></div>`;

  renderEstimatePanel(container);
  renderLive(container);
  wire(container, api);
}

// live orchestration events (schema v2) → update the in-flight run panel
export function onEvent(state, evt) {
  const k = evt.kind;
  if (!k || !k.startsWith("orchestration_")) return false;
  if (k === "orchestration_started") {
    S.live = { run_id: evt.run_id, team: evt.team, workflow: evt.workflow, title: evt.title,
               stage: "starting", agents: [], status: "running", est: evt.estimated_cost_usd };
  } else if (S.live && evt.run_id === S.live.run_id) {
    if (k === "orchestration_stage") S.live.stage = evt.stage;
    else if (k === "orchestration_agent")
      S.live.agents.push({ role: evt.role, member: evt.member, stage: evt.stage, ok: evt.ok });
    else if (k === "orchestration_round") S.live.stage = `verdict (round ${evt.round}: ${evt.verdict})`;
    else if (k === "orchestration_completed") {
      S.live.stage = "done"; S.live.status = evt.status; S.live.verdict = evt.verdict;
    }
  }
  return true; // signal app.js to refresh the studio screen if active
}

function wire(container, api) {
  container.querySelector("#st-team")?.addEventListener("change", (e) => {
    S.team = e.target.value; S.workflow = defaultWorkflow(S.catalog, S.team); S.estimate = null;
    render(container, api);
  });
  container.querySelector("#st-workflow")?.addEventListener("change", (e) => {
    S.workflow = e.target.value; S.estimate = null; renderEstimatePanel(container);
  });
  container.querySelector("#st-estimate")?.addEventListener("click", () => doEstimate(container, api));
  container.querySelector("#st-run")?.addEventListener("click", () => doRun(container, api, false));
  for (const row of container.querySelectorAll("[data-run]")) {
    row.addEventListener("click", async () => {
      const d = await api.get(`/api/orchestration/${row.dataset.run}`);
      S.detail = d; renderDetail(container);
    });
  }
}

function params(container) {
  const task = container.querySelector("#st-task")?.value.trim() || "";
  const b = container.querySelector("#st-budget")?.value;
  const budget_usd = b ? Number(b) : null;
  return { team: S.team, workflow: S.workflow, task, budget_usd };
}

async function doEstimate(container, api) {
  const p = params(container);
  const q = new URLSearchParams({ team: p.team, workflow: p.workflow, task: p.task });
  if (p.budget_usd != null) q.set("budget_usd", String(p.budget_usd));
  const r = await api.get(`/api/orchestration/estimate?${q.toString()}`);
  S.estimate = r && r.ok ? r.estimate : { decision: "error", reason: (r && r.message) || "failed" };
  renderEstimatePanel(container);
}

async function doRun(container, api, confirmed) {
  const p = params(container);
  const r = await api.post("/api/orchestration/run", { ...p, confirmed });
  if (r.status === 200 && r.data.needs_confirmation) {
    S.estimate = r.data.estimate; S.estimate._needs_confirm = true;
    renderEstimatePanel(container);
    return;
  }
  if (!r.ok) {
    S.estimate = { decision: "error", reason: r.data.message || `HTTP ${r.status}` };
    renderEstimatePanel(container);
    return;
  }
  S.estimate = r.data.estimate || null;
  renderEstimatePanel(container);
}

function renderEstimatePanel(container) {
  const el = container.querySelector("#st-estimate-panel");
  if (!el) return;
  const e = S.estimate;
  if (!e) { el.innerHTML = ""; return; }
  if (e.decision === "error") {
    el.innerHTML = `<div class="est-panel warn">${esc(e.reason || "estimate failed")}</div>`;
    return;
  }
  const money = (n) => (n == null ? "—" : `$${n.toFixed(4)}`);
  const cls = e.decision === "block" ? "warn" : e.decision === "confirm" ? "amber" : "ok";
  const members = (e.members || []).map((m) =>
    `<tr><td>${esc(m.member_id)}</td><td class="dim">${esc(m.model)}</td>
     <td style="text-align:right" class="dim">×${m.turns}</td>
     <td style="text-align:right">${money(m.model_usd)}</td></tr>`).join("");
  const confirmBtn = (e.decision === "confirm" && e._needs_confirm)
    ? `<button id="st-confirm" class="btn-always">Confirm & run (${money(e.total_usd)})</button>` : "";
  el.innerHTML = `<div class="est-panel ${cls}">
    <div><b>Worst case: ${money(e.total_usd)}</b> · ${esc(e.decision)}</div>
    <div class="dim">${esc(e.reason || "")}</div>
    ${e.unpriced && e.unpriced.length ? `<div class="warn dim">unpriced: ${esc(e.unpriced.join(", "))}</div>` : ""}
    <table style="margin-top:.4rem">${members}</table>
    ${confirmBtn}</div>`;
  el.querySelector("#st-confirm")?.addEventListener("click", () => doRun(container, api_(), true));
}

function renderLive(container) {
  const el = container.querySelector("#st-live");
  if (!el || !S.live) { if (el) el.innerHTML = ""; return; }
  const L = S.live;
  const stages = ["council", "synthesis", "execution", "review", "verdict", "done"];
  const timeline = stages.map((s) => {
    const active = String(L.stage).startsWith(s);
    const done = stages.indexOf(s) < stages.indexOf(String(L.stage).split(" ")[0]);
    return `<span class="stage ${active ? "on" : done ? "past" : ""}">${s}</span>`;
  }).join("<span class='arr'>→</span>");
  const agents = (L.agents || []).map((a) =>
    `<span class="chip ${a.ok ? "" : "warn"}">${esc(a.role)}·${esc(a.stage)}${a.ok ? "" : " ✗"}</span>`).join(" ");
  el.innerHTML = `<div class="card rise live">
    <div class="card-label">Live · ${esc(L.title || "run")} ${statusPill(L.status, L.verdict)}</div>
    <div class="timeline">${timeline}</div>
    <div style="margin-top:.5rem">${agents}</div></div>`;
}

function renderDetail(container) {
  const el = container.querySelector("#st-detail");
  if (!el || !S.detail || !S.detail.run) { if (el) el.innerHTML = ""; return; }
  const r = S.detail.run;
  const money = (n) => (n == null ? "—" : `$${n.toFixed(4)}`);
  const members = (S.detail.members || []).map((m) =>
    `<tr><td>${esc(m.role || "?")}</td><td class="dim">${esc(m.stage || "")}</td>
     <td>${statusPill(m.status)}</td><td style="text-align:right" class="dim">${m.iterations} it · ${m.denied_count} denied</td>
     <td style="text-align:right">${money(m.cost_usd)}</td></tr>`).join("");
  const manifest = (r.context_manifest || []).map((c) =>
    `<span class="chip dim">${esc(c.kind)}:${esc(c.ref)}</span>`).join(" ");
  const roi = S.detail.roi;
  const roiLine = roi
    ? `<div class="dim">ROI: value ${money(roi.value_usd)} (${roi.baseline_minutes}m) − actual
       ${money(roi.actual_cost_usd)} = <b>${roi.net_usd == null ? "unknown" : money(roi.net_usd)}</b></div>`
    : "";
  const bd = S.detail.cost_breakdown;
  const bdLine = bd
    ? `<div class="dim" style="margin-top:.3rem">by stage: ${(bd.by_stage || [])
        .map((s) => `${esc(s.stage || "?")} ${money(s.cost_usd)}`).join(" · ") || "—"}
        ${(bd.services || []).length ? ` · services: ${bd.services
          .map((s) => `${esc(s.service)}×${s.calls}`).join(" · ")}` : ""}</div>`
    : "";
  el.innerHTML = `<div class="card rise">
    <div class="card-label">Run #${r.id} · ${esc(r.title)} ${statusPill(r.status, r.verdict)}</div>
    <div class="dim">est ${money(r.estimated_cost_usd)} · actual ${money(r.actual_cost_usd)}
      ${r.budget_usd != null ? ` · cap ${money(r.budget_usd)}` : ""}</div>
    ${roiLine}${bdLine}
    ${r.synthesis_summary ? `<div class="synth">${esc(r.synthesis_summary)}</div>` : ""}
    <table style="margin-top:.4rem">${members || '<tr><td class="dim">no members</td></tr>'}</table>
    <div class="dim" style="margin-top:.4rem">context: ${manifest || "—"}</div></div>`;
}

// --- helpers ---
function memberCard(m, routeModel, svcState) {
  const capCls = m.capability === "write_capable" ? "amber" : "";
  const tools = m.tools.map((t) => `<span class="chip dim">${esc(t)}</span>`).join(" ");
  const services = m.services.map((s) => {
    const st = svcState[s] || "unknown";
    const cls = st === "available" ? "ok" : st === "deferred" || st === "disabled" ? "dim" : "warn";
    return `<span class="chip ${cls}" title="${esc(st)}">${esc(s)} · ${esc(st)}</span>`;
  }).join(" ");
  return `<div class="member">
    <div class="member-head"><b>${esc(m.title)}</b>
      <span class="chip ${capCls}">${esc(m.capability.replace("_", " "))}</span></div>
    <div class="dim mono">${esc(m.route_role)} → ${esc(routeModel[m.route_role] || "?")}</div>
    <div class="chips">${tools} ${services}</div></div>`;
}

function runsRows(runs) {
  if (!runs.length) return '<tr><td class="dim">No runs yet.</td></tr>';
  const money = (n) => (n == null ? "—" : `$${n.toFixed(4)}`);
  return runs.map((r) =>
    `<tr data-run="${r.id}" class="clickable"><td>${esc(r.team || "?")}</td>
     <td class="dim">${esc(r.workflow)}</td><td>${statusPill(r.status, r.verdict)}</td>
     <td style="text-align:right" class="dim">${money(r.actual_cost_usd ?? r.estimated_cost_usd)}</td></tr>`
  ).join("");
}

function statusPill(status, verdict) {
  const map = { ok: "ok", running: "amber", budget_stopped: "warn", error: "warn",
                rejected: "warn", cancelled: "dim", aborted: "dim", revise: "amber" };
  const label = verdict && status === "ok" ? verdict : status;
  return `<span class="chip ${map[status] || "dim"}">${esc(label || "")}</span>`;
}

function workflowOptions(cat, team) {
  const prefer = new Set((team && team.default_workflows) || []);
  const ordered = [...cat.workflows].sort((a, b) => (prefer.has(b.id) ? 1 : 0) - (prefer.has(a.id) ? 1 : 0));
  return ordered.map((w) =>
    `<option value="${w.id}" ${w.id === S.workflow ? "selected" : ""}>${esc(w.title)}${
      w.has_execution ? " ✎" : ""}</option>`).join("");
}

function defaultWorkflow(cat, teamId) {
  const team = cat.teams.find((t) => t.id === teamId);
  return (team && team.default_workflows[0]) || (cat.workflows[0] && cat.workflows[0].id);
}

function mapRoutes(routes) {
  const m = {};
  for (const r of routes || []) m[r.role] = r.model;
  return m;
}
function mapServices(services) {
  const m = {};
  for (const s of services || []) m[s.name] = s.state;
  return m;
}
function esc(s) { const d = document.createElement("div"); d.textContent = s ?? ""; return d.innerHTML; }

// render() stashes the `api` handle so the confirm button (re-rendered after a needs_confirmation
// response) can re-POST with confirmed=true without threading `api` through every helper.
let _api = null;
function api_() { return _api; }
