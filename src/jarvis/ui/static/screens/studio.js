// Studio — the AI Orchestration Studio (Phase 10B; polished T12). Pick a team + workflow, describe
// the task, preview the worst-case cost, and launch a run. Everything here is a VIEW + a click: the
// engine enforces the read-only council/review floor, the single writer under the turn lock,
// the budget reservation, and the two-step confirm server-side. No key, prompt, or report body
// ever reaches this screen — only metadata, summaries, and manifests.
import { esc } from "../ui/dom.js";
import { openTaskDraft } from "../ui/task-draft.js";

const S = {
  catalog: null,     // { teams, workflows, services, model_routes, active_project_id, busy }
  runs: [],          // recent orchestration runs (summaries)
  team: null,        // selected team id
  workflow: null,    // selected workflow id
  estimate: null,    // last previewed estimate
  live: null,        // { run_id, team, workflow, title, stage, agents:[], status, verdict }
  detail: null,      // an expanded run detail { run, members }
  head: null,        // the planner route {model, provider} (Fable) — synthesis + final verdict
};

// The head reviewer/synthesizer is an ENGINE STAGE (Fable on the planner route), not a team
// member — badged visibly on the roster, the live verdict, and a run's synthesis.
function headBadge(route) {
  return `<span class="head-badge" title="Head synthesizer + final verdict — an engine stage, not a team member">
    <span class="hb-dot"></span>Fable <span class="dim mono">${esc(routeLabel(route, "planner"))}</span></span>`;
}

export async function render(container, api, args = []) {
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
  S.head = routeModel["planner"] || null;
  const requestedRun = /^\d+$/.test(String(args[0] || "")) ? Number(args[0]) : null;

  container.innerHTML = `
    <div class="rise studio-head">
      <h1>Orchestration Studio</h1>
      <div class="sub">A project team runs a workflow: council → synthesis → (execution) →
        review → verdict. Read-only members fan out; one writer runs under the turn lock.</div>
    </div>
    ${cat.active_project_id == null
      ? `<div class="card warn rise">Teams are project-scoped — select a project first.</div>`
      : ""}
    <div class="studio-grid"${requestedRun != null ? " hidden" : ""}>
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
        <div class="head-line">Synthesis + final verdict ${headBadge(S.head)}</div>
      </div>
    </div>
    <div id="st-live"></div>
    <div id="st-detail"></div>
    <div class="card rise">
      <div class="card-label">Recent runs</div>
      <table><tbody id="st-runs">${runsRows(S.runs)}</tbody></table>
    </div>`;

  renderEstimatePanel(container);
  renderLive(container);
  wire(container, api);
  if (requestedRun != null) await showRunDetail(container, api, requestedRun);
  else { S.detail = null; renderDetail(container); }
}

// live orchestration events (schema v2) → update the in-flight run panel
export function onEvent(state, evt) {
  const k = evt.kind;
  if (!k || !k.startsWith("orchestration_")) return false;
  const ctx = state && state.context;
  if (!ctx || evt.session_id !== ctx.session_id || evt.project_id !== ctx.project_id) return false;
  if (k === "orchestration_started") {
    S.live = { run_id: evt.run_id, team: evt.team, workflow: evt.workflow, title: evt.title,
               stage: "starting", agents: [], status: "running", est: evt.estimated_cost_usd };
  } else if (k === "orchestration_resumed") {
    S.live = { run_id: evt.run_id, team: evt.team, workflow: evt.workflow, title: evt.title,
               stage: evt.stage || "execution", agents: [], status: "running" };
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
    row.addEventListener("click", () => { location.hash = `studio/${row.dataset.run}`; });
  }
}

async function showRunDetail(container, api, runId) {
  const detail = await api.get(`/api/orchestration/${runId}`);
  S.detail = detail && detail.run ? detail : null;
  renderDetail(container);
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
  const atHead = ["synthesis", "verdict", "done"].some((s) => String(L.stage).startsWith(s));
  el.innerHTML = `<div class="card rise live">
    <div class="card-label">Live · ${esc(L.title || "run")} ${statusPill(L.status, L.verdict)}</div>
    <div class="timeline">${timeline}</div>
    ${atHead ? `<div class="head-line">Head reviewer ${headBadge(S.head)}</div>` : ""}
    <div style="margin-top:.5rem">${agents}</div></div>`;
}

function renderDetail(container) {
  const el = container.querySelector("#st-detail");
  if (!el || !S.detail || !S.detail.run) { if (el) el.innerHTML = ""; return; }
  const r = S.detail.run;
  const money = (n) => (n == null ? "—" : `$${n.toFixed(4)}`);
  const recordedSkills = (entries) => (entries || []).map((skill) =>
    `<span class="chip dim" title="pack ${esc(skill.sha256)} · compiled ${esc(skill.compiled_sha256)}">${esc(skill.pack)} v${esc(skill.version)} · ${esc(skill.member)}/${esc(skill.stage)}</span>`).join(" ");
  const members = (S.detail.members || []).map((m) =>
    `<tr><td><b>${esc(memberLabel(m))}</b><div class="dim mono">${esc(m.role || "?")}</div></td><td class="dim">${esc(m.stage || "")}</td>
    <td>${statusPill(m.status)}</td><td style="text-align:right" class="dim">${m.iterations} it · ${m.denied_count} denied</td>
     <td style="text-align:right">${money(m.cost_usd)}<div class="dim mono">${esc((m.models || []).join(" / ") || "model not recorded")}</div>
     <div class="dim">recorded skills: ${recordedSkills(m.skills_manifest) || "—"}</div></td></tr>`).join("");
  const manifest = (r.context_manifest || []).map((c) =>
    `<span class="chip dim">${esc(c.kind)}:${esc(c.ref)}</span>`).join(" ");
  const skillManifest = recordedSkills(r.skills_manifest);
  const roi = S.detail.roi;
  const outcome = roi ? esc(String(roi.outcome || "completed_unreviewed").replace(/_/g, " ")) : "";
  const roiLine = !roi
    ? ""
    : roi.outcome === "review_accepted"
      ? `<div class="dim">ROI (review accepted): value ${money(roi.value_usd)} (${roi.baseline_minutes}m) − actual
         ${money(roi.actual_cost_usd)} = <b>${roi.net_usd == null ? "unknown" : money(roi.net_usd)}</b></div>`
      : `<div class="dim">Outcome: ${outcome} · actual model cost ${money(roi.actual_cost_usd)}. Time-saved value is not claimed.</div>`;
  const bd = S.detail.cost_breakdown;
  const bdLine = bd
    ? `<div class="dim" style="margin-top:.3rem">by stage: ${(bd.by_stage || [])
        .map((s) => `${esc(s.stage || "?")} ${money(s.cost_usd)}`).join(" · ") || "—"}
        ${(bd.services || []).length ? ` · services: ${bd.services
          .map((s) => `${esc(s.service)}×${s.calls}`).join(" · ")}` : ""}</div>`
    : "";
  const findings = (r.synthesis_findings || []).map((finding) =>
    `<article class="run-finding"><b>${esc(finding.title || finding.member || "Team member")}</b>
      <div>${esc(finding.finding || "")}</div></article>`).join("");
  const findingBlock = findings
    ? `<section class="run-findings"><div class="synth-head">What each member found</div>${findings}</section>`
    : `<div class="dim run-findings-note">This earlier run did not record member-level syntheses. The team synthesis below is its safe result record.</div>`;
  const verdictBlock = r.verdict_rationale
    ? `<section class="run-verdict"><div class="synth-head">Final rationale</div>${esc(r.verdict_rationale)}</section>`
    : "";
  const actionItems = Array.isArray(r.action_items) ? r.action_items : [];
  const actions = actionItems.map((item, index) =>
    `<article class="run-action-item"><div class="run-action-title"><b>${esc(item.title || "Follow-up")}</b>
      <span class="chip dim">${esc(item.priority || "medium")}</span></div>
      <div>${esc(item.goal || "")}</div>
      <button class="plain-button ghost run-actions-promote" data-promote-follow-up="${index}">Review &amp; schedule</button></article>`).join("");
  const actionBlock = actions
    ? `<section class="run-actions"><div class="synth-head">Recommended next steps</div>
        <div class="dim run-actions-note">Added to this project's Team follow-ups. They are not scheduled or run automatically.</div>
        ${actions}<button class="plain-button ghost run-actions-open" data-project-tasks="${r.project_id}">Open project Tasks</button></section>`
    : `<div class="dim run-findings-note">This earlier run did not record a structured follow-up plan. Run the review again to create one.</div>`;
  const resumeBlock = r.can_resume
    ? `<section class="run-actions"><div class="synth-head">Recover safely</div>
        <div class="dim run-actions-note">This run stopped after synthesis and before any writer began. Re-enter the exact original task brief to continue from that bounded synthesis. The original brief and team reports were not stored.</div>
        <textarea id="st-resume-task" rows="3" placeholder="Re-enter the exact original task brief"></textarea>
        <button id="st-resume" class="plain-button ghost">Continue from synthesis</button>
        <div id="st-resume-note" class="dim run-actions-note"></div></section>`
    : "";
  el.innerHTML = `<div class="card rise">
    <div class="card-label">Run #${r.id} · ${esc(r.title)} ${statusPill(r.status, r.verdict)}</div>
    <div class="dim">est ${money(r.estimated_cost_usd)} · actual ${money(r.actual_cost_usd)}
      ${r.budget_usd != null ? ` · cap ${money(r.budget_usd)}` : ""}</div>
    ${roiLine}${bdLine}
    ${r.synthesis_summary ? `<div class="synth"><div class="synth-head">What the team found ${headBadge(S.head)}</div>${esc(r.synthesis_summary)}</div>` : ""}
    ${actionBlock}${resumeBlock}${findingBlock}${verdictBlock}
    <table style="margin-top:.4rem">${members || '<tr><td class="dim">no members</td></tr>'}</table>
    <div class="dim" style="margin-top:.4rem">context: ${manifest || "—"}</div>
    <div class="dim" style="margin-top:.4rem">Skill packs recorded at run start: ${skillManifest || "No skill packs recorded for this run."}</div>
    <div class="dim" style="margin-top:.2rem">Recorded metadata does not prove prompt injection; Shadow mode records manifests without injecting guidance.</div></div>`;
  el.querySelector("[data-project-tasks]")?.addEventListener("click", (event) => {
    const projectId = Number(event.currentTarget.dataset.projectTasks);
    if (Number.isInteger(projectId) && projectId > 0) location.hash = `workspace/${projectId}/tasks`;
  });
  el.querySelectorAll("[data-promote-follow-up]").forEach((button) => {
    button.addEventListener("click", async (event) => {
      const index = Number(event.currentTarget.dataset.promoteFollowUp);
      const item = actionItems[index];
      if (!item) return;
      await openTaskDraft({
        runId: r.id,
        runTitle: r.title || r.workflow || "Team run",
        title: item.title,
        goal: item.goal,
        priority: item.priority,
      }, api_());
    });
  });
  el.querySelector("#st-resume")?.addEventListener("click", async () => {
    const task = el.querySelector("#st-resume-task")?.value.trim() || "";
    const note = el.querySelector("#st-resume-note");
    if (!task) { if (note) note.textContent = "Re-enter the original task brief first."; return; }
    const response = await api_().post(`/api/orchestration/${r.id}/resume`, { task });
    if (!response.ok) {
      if (note) note.textContent = response.data?.message || `HTTP ${response.status}`;
      return;
    }
    if (note) note.textContent = "Continuation started. The checkpoint has been consumed before any writer runs.";
  });
}

function memberLabel(member) {
  const raw = String(member.title || member.role || "Team member");
  const id = raw.includes(":") ? raw.split(":").pop() : raw;
  return id.replace(/[_-]+/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
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
    <div class="dim mono">${esc(m.route_role)} → ${esc(routeLabel(routeModel[m.route_role]))}</div>
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
  for (const r of routes || []) m[r.role] = r; // keep the whole route (model + provider + …)
  return m;
}
function routeLabel(rt, fb = "?") {
  return rt && rt.model ? `${rt.model}${rt.provider ? " · " + rt.provider : ""}` : fb;
}
function mapServices(services) {
  const m = {};
  for (const s of services || []) m[s.name] = s.state;
  return m;
}
// render() stashes the `api` handle so the confirm button (re-rendered after a needs_confirmation
// response) can re-POST with confirmed=true without threading `api` through every helper.
let _api = null;
function api_() { return _api; }
