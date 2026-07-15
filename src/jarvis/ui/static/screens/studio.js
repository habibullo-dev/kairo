// Studio — the AI Orchestration Studio (Phase 10B; polished T12). Pick a team + workflow, describe
// the task, preview the worst-case cost, and launch a run. Everything here is a VIEW + a click: the
// engine enforces the read-only council/review floor, the single writer under the turn lock,
// the budget reservation, and the two-step confirm server-side. No key, prompt, or report body
// ever reaches this screen — only metadata, summaries, and manifests.
import { esc } from "../ui/dom.js";
import { openTaskDraft } from "../ui/task-draft.js";

const S = {
  catalog: null,     // { teams, workflows, ..., busy, cancellable_run_id }
  runs: [],          // recent orchestration runs (summaries)
  team: null,        // selected team id
  workflow: null,    // selected workflow id
  estimate: null,    // last previewed estimate
  live: null,        // { run_id, team, workflow, title, stage, agents:[], status, verdict }
  detail: null,      // an expanded run detail { run, members }
  head: null,        // the planner route {model, provider} (Fable) — synthesis + final verdict
  projectId: null,   // reset all private draft/run state when the exact active project changes
  task: "",           // human-editable draft; survives harmless Studio rerenders in one project
  budget: "",
  prefillKey: null,  // prevents a rerender from overwriting a human-edited assessment draft
  confirmation: null, // exact params + authority for one visible cost-confirmation response
  authorityToken: null,
  resumeNotice: null,
  cancelNotice: null,
  cancelUnavailableRunId: null,
};
let _renderGeneration = 0;
let _estimateGeneration = 0;
let _runSequence = 0;
let _runOperation = null;
let _resumeOperation = null;
let _cancelOperation = null;
let _activeContainer = null;
let _activeApi = null;
const TERMINAL_RUN_STATUSES = new Set([
  "ok", "rejected", "revise", "error", "cancelled", "aborted", "budget_stopped",
]);
const CANCEL_RECONCILE_DELAYS_MS = [500, 1000, 2000, 4000, 8000];
const CANCEL_RECONCILE_READ_TIMEOUT_MS = 4000;

// The head reviewer/synthesizer is an ENGINE STAGE (Fable on the planner route), not a team
// member — badged visibly on the roster, the live verdict, and a run's synthesis.
function headBadge(route) {
  return `<span class="head-badge" title="Head synthesizer + final verdict — an engine stage, not a team member">
    <span class="hb-dot"></span>Fable <span class="dim mono">${esc(routeLabel(route, "planner"))}</span></span>`;
}

export async function render(container, api, args = []) {
  const renderGeneration = ++_renderGeneration;
  _activeContainer = container;
  _activeApi = api;
  const authorityToken = typeof api.authorityToken === "function" ? api.authorityToken() : null;
  const contextProjectId = api.state.context?.project_id ?? null;
  // Retire old private state before yielding to catalog/history reads. Otherwise a new
  // authority's first orchestration event can arrive during the await and be wiped by a late reset.
  if (S.projectId !== contextProjectId || S.authorityToken !== authorityToken) {
    resetForProject(contextProjectId, authorityToken);
  }
  const reportIntent = args[0] === "report";
  const reportRoute = parseReportRoute(args);
  const requestedPrefillKey = reportRoute
    ? `${reportRoute.reportId}:${reportRoute.recommendation}`
    : null;
  if (reportIntent && requestedPrefillKey !== S.prefillKey) S.estimate = null;
  const [cat, hist, reportSuggestion] = await Promise.all([
    api.getRequired("/api/studio"),
    api.get("/api/orchestration"),
    reportRoute ? api.get(
      `/api/project-intelligence/reports/${encodeURIComponent(reportRoute.reportId)}/studio-prefill?recommendation=${encodeURIComponent(reportRoute.recommendation)}`
    ) : Promise.resolve(null),
  ]);
  if (renderGeneration !== _renderGeneration) return;
  if (reportIntent && !reportRouteIsActive(args)) return;
  if (!cat) {
    container.innerHTML = `<div class="rise"><h1>Studio</h1>
      <div class="sub">Orchestration is not available (delegation/sub-agents off).</div></div>`;
    return;
  }
  if (S.projectId !== cat.active_project_id || S.authorityToken !== authorityToken) {
    resetForProject(cat.active_project_id, authorityToken);
  }
  S.catalog = cat;
  S.runs = (hist && hist.runs) || [];
  reconcileLiveSnapshot(cat.cancellable_run_id, S.runs);
  S.team = S.team || (cat.teams[0] && cat.teams[0].id);
  S.workflow = S.workflow || defaultWorkflow(cat, S.team);
  const prefill = validateReportPrefill(reportSuggestion, reportRoute, cat);
  if (prefill && requestedPrefillKey !== S.prefillKey) {
    S.team = prefill.team;
    S.workflow = prefill.workflow;
  }

  const routeModel = mapRoutes(cat.model_routes);
  const svcState = mapServices(cat.services);
  const team = cat.teams.find((t) => t.id === S.team) || cat.teams[0];
  S.head = routeModel["planner"] || null;
  if (S.confirmation) {
    S.confirmation.api = api;
    S.confirmation.container = container;
  }
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
        <div id="st-prefill-note"></div>
        <div class="card-label">Team</div>
        <select id="st-team">${team ? cat.teams.map((t) =>
          `<option value="${t.id}" ${t.id === S.team ? "selected" : ""}>${esc(t.icon)} ${esc(t.name)}</option>`
        ).join("") : ""}</select>
        <div class="dim studio-team-description">${esc(team ? team.description : "")}</div>
        <div class="card-label">Workflow</div>
        <select id="st-workflow">${workflowOptions(cat, team)}</select>
        <div class="card-label studio-task-label">Task brief</div>
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
  renderLive(container, api);
  const taskInput = container.querySelector("#st-task");
  const budgetInput = container.querySelector("#st-budget");
  if (taskInput) taskInput.value = S.task;
  if (budgetInput) budgetInput.value = S.budget;
  wire(container, api);
  renderRunControls(container);
  if (requestedRun != null) await showRunDetail(container, api, requestedRun, authorityToken);
  else {
    S.detail = null;
    renderDetail(container, api, authorityToken);
    if (reportIntent) await applyReportPrefill(container, api, prefill, reportRoute);
  }
}

// live orchestration events (schema v2) → update the in-flight run panel
export function onEvent(state, evt) {
  const k = evt.kind;
  if (!k || !k.startsWith("orchestration_")) return false;
  const ctx = state && state.context;
  if (!ctx || evt.session_id !== ctx.session_id || evt.project_id !== ctx.project_id) return false;
  if (k === "orchestration_started") {
    if (_cancelOperation && _cancelOperation.runId !== evt.run_id) {
      retireCancelOperation(_cancelOperation);
    }
    S.live = { run_id: evt.run_id, team: evt.team, workflow: evt.workflow, title: evt.title,
               stage: "starting", agents: [], status: "running", est: evt.estimated_cost_usd };
    S.cancelNotice = null;
    S.cancelUnavailableRunId = null;
  } else if (k === "orchestration_resumed") {
    if (_cancelOperation && _cancelOperation.runId !== evt.run_id) {
      retireCancelOperation(_cancelOperation);
    }
    S.live = { run_id: evt.run_id, team: evt.team, workflow: evt.workflow, title: evt.title,
               stage: evt.stage || "execution", agents: [], status: "running" };
    S.cancelNotice = null;
    S.cancelUnavailableRunId = null;
  } else if (S.live && evt.run_id === S.live.run_id) {
    if (k === "orchestration_stage") S.live.stage = evt.stage;
    else if (k === "orchestration_agent")
      S.live.agents.push({ role: evt.role, member: evt.member, stage: evt.stage, ok: evt.ok });
    else if (k === "orchestration_round") S.live.stage = `verdict (round ${evt.round}: ${evt.verdict})`;
    else if (k === "orchestration_completed") {
      S.live.stage = "done"; S.live.status = evt.status; S.live.verdict = evt.verdict;
      if (S.catalog?.cancellable_run_id === evt.run_id) S.catalog.cancellable_run_id = null;
      const requestedHere = _cancelOperation
        && _cancelOperation.runId === evt.run_id
        && _cancelOperation.authorityToken === S.authorityToken;
      const hadCancelNotice = Boolean(S.cancelNotice);
      if (requestedHere) retireCancelOperation(_cancelOperation);
      S.cancelUnavailableRunId = null;
      if (evt.status === "cancelled") {
        S.cancelNotice = "Run stopped. Completed work remains in this run record.";
      } else if (requestedHere || hadCancelNotice) {
        S.cancelNotice = `Run finished as ${humanStatus(evt.status)} before the stop request settled.`;
      }
    }
  }
  return true; // signal app.js to refresh the studio screen if active
}

function wire(container, api) {
  container.querySelector("#st-task")?.addEventListener("input", (e) => {
    S.task = e.target.value;
    invalidateEstimate(container);
  });
  container.querySelector("#st-budget")?.addEventListener("input", (e) => {
    S.budget = e.target.value;
    invalidateEstimate(container);
  });
  container.querySelector("#st-team")?.addEventListener("change", (e) => {
    S.team = e.target.value; S.workflow = defaultWorkflow(S.catalog, S.team);
    invalidateEstimate(container);
    void api.refreshRoute();
  });
  container.querySelector("#st-workflow")?.addEventListener("change", (e) => {
    S.workflow = e.target.value;
    invalidateEstimate(container);
  });
  container.querySelector("#st-estimate")?.addEventListener("click", () => doEstimate(container, api));
  container.querySelector("#st-run")?.addEventListener("click", () => doRun(container, api, false));
  for (const row of container.querySelectorAll("[data-run]")) {
    row.addEventListener("click", () => { location.hash = `studio/${row.dataset.run}`; });
  }
}

async function showRunDetail(container, api, runId, authorityToken) {
  const detail = await api.getRequired(`/api/orchestration/${runId}`);
  if ((authorityToken !== null && typeof api.authorityIsCurrent === "function"
      && !api.authorityIsCurrent(authorityToken))
      || (typeof api.renderIsCurrent === "function" && !api.renderIsCurrent())) return;
  S.detail = detail && detail.run ? detail : null;
  S.resumeNotice = null;
  renderDetail(container, api, authorityToken);
}

function parseReportRoute(args) {
  if (args.length !== 3 || args[0] !== "report") return null;
  if (!/^[1-9]\d{0,9}$/.test(String(args[1] || ""))) return null;
  if (!/^[0-4]$/.test(String(args[2] ?? ""))) return null;
  const reportId = Number(args[1]);
  const recommendation = Number(args[2]);
  return Number.isSafeInteger(reportId) ? { reportId, recommendation } : null;
}

function reportRouteIsActive(args) {
  return location.hash.replace(/^#/, "") === `studio/${args.join("/")}`;
}

function resetForProject(projectId, authorityToken = null) {
  S.projectId = projectId;
  S.authorityToken = authorityToken;
  S.team = null;
  S.workflow = null;
  S.estimate = null;
  S.live = null;
  S.detail = null;
  S.task = "";
  S.budget = "";
  S.prefillKey = null;
  S.confirmation = null;
  S.resumeNotice = null;
  S.cancelNotice = null;
  S.cancelUnavailableRunId = null;
  _runOperation = null;
  _resumeOperation = null;
  retireCancelOperation(_cancelOperation);
  _estimateGeneration += 1;
}

function retireCancelOperation(operation) {
  if (!operation) return;
  if (operation.reconcileTimer !== null && operation.reconcileTimer !== undefined) {
    clearTimeout(operation.reconcileTimer);
    operation.reconcileTimer = null;
  }
  operation.reconcileAbort?.abort();
  operation.reconcileAbort = null;
  if (_cancelOperation === operation) _cancelOperation = null;
}

function runId(value) {
  return typeof value === "number" && Number.isSafeInteger(value) && value > 0 ? value : null;
}

function terminalRunStatus(value) {
  return typeof value === "string" && TERMINAL_RUN_STATUSES.has(value) ? value : null;
}

function reconcileLiveSnapshot(cancellableRunId, runs) {
  const activeRunId = runId(cancellableRunId);
  const summaries = Array.isArray(runs) ? runs : [];
  if (activeRunId !== null) {
    const previousLiveRunId = runId(S.live?.run_id);
    if (
      (previousLiveRunId !== null && previousLiveRunId !== activeRunId)
      || (_cancelOperation && _cancelOperation.runId !== activeRunId)
    ) {
      retireCancelOperation(_cancelOperation);
      S.cancelNotice = null;
      S.cancelUnavailableRunId = null;
    }
    if (S.cancelUnavailableRunId !== activeRunId) S.cancelUnavailableRunId = null;
    if (!S.live || runId(S.live.run_id) !== activeRunId) {
      const summary = summaries.find((item) => runId(item.id) === activeRunId) || {};
      S.live = {
        run_id: activeRunId,
        team: summary.team,
        workflow: summary.workflow,
        title: summary.title || `Run #${activeRunId}`,
        stage: summary.stage || "active",
        agents: [],
        status: "running",
        verdict: summary.verdict,
        est: summary.estimated_cost_usd,
      };
    }
    return;
  }
  if (!S.live || S.live.status !== "running") return;
  const summary = summaries.find((item) => runId(item.id) === runId(S.live.run_id));
  const summaryStatus = terminalRunStatus(summary?.status);
  if (!summaryStatus) return;
  S.live.stage = "done";
  S.live.status = summaryStatus;
  S.live.verdict = summary.verdict;
  const requestedHere = _cancelOperation
    && _cancelOperation.runId === runId(S.live.run_id)
    && _cancelOperation.authorityToken === S.authorityToken;
  const hadCancelNotice = Boolean(S.cancelNotice);
  if (requestedHere) retireCancelOperation(_cancelOperation);
  S.cancelUnavailableRunId = null;
  if (summaryStatus === "cancelled") {
    S.cancelNotice = "Run stopped. Completed work remains in this run record.";
  } else if (requestedHere || hadCancelNotice) {
    S.cancelNotice = `Run finished as ${humanStatus(summaryStatus)} before the stop request settled.`;
  }
}

function invalidateEstimate(container) {
  S.estimate = null;
  S.confirmation = null;
  _estimateGeneration += 1;
  renderEstimatePanel(container);
}

function renderRunControls(container) {
  const busy = Boolean(_runOperation && _runOperation.authorityToken === S.authorityToken);
  for (const selector of ["#st-team", "#st-workflow", "#st-task", "#st-budget", "#st-estimate", "#st-run", "#st-confirm"]) {
    const control = container.querySelector(selector);
    if (control) control.disabled = busy;
  }
  const run = container.querySelector("#st-run");
  if (run) run.textContent = busy ? "Starting…" : "Run";
}

function sameParams(left, right) {
  return Boolean(left && right
    && left.team === right.team
    && left.workflow === right.workflow
    && left.task === right.task
    && left.budget_usd === right.budget_usd);
}

function draftParams() {
  const budget = S.budget ? Number(S.budget) : null;
  return { team: S.team, workflow: S.workflow, task: S.task.trim(), budget_usd: budget };
}

function validateReportPrefill(data, route, catalog) {
  const prefill = data?.prefill;
  if (!route || !prefill || typeof prefill !== "object") return null;
  const team = catalog.teams.find((item) => item.id === prefill.team);
  const workflowExists = catalog.workflows.some((item) => item.id === prefill.workflow);
  if (
    prefill.report_id !== route.reportId
    || prefill.recommendation !== route.recommendation
    || !team
    || !workflowExists
    || !Array.isArray(team.default_workflows)
    || !team.default_workflows.includes(prefill.workflow)
    || typeof prefill.task !== "string"
    || !prefill.task.trim()
    || prefill.task.length > 720
  ) return null;
  return {
    team: prefill.team,
    workflow: prefill.workflow,
    task: prefill.task,
  };
}

async function applyReportPrefill(container, api, prefill, route) {
  const notice = container.querySelector("#st-prefill-note");
  const task = container.querySelector("#st-task");
  if (!notice || !task) return;
  notice.className = "studio-prefill-note";
  if (!prefill) {
    notice.classList.add("warn");
    notice.textContent = "This assessment recommendation is unavailable or no longer current.";
    return;
  }
  notice.classList.add("ready");
  notice.textContent = "Suggested from project assessment. Review scope and cost. Nothing has started.";
  const key = `${route.reportId}:${route.recommendation}`;
  if (S.prefillKey === key) {
    task.value = S.task;
    return;
  }
  S.prefillKey = key;
  S.task = prefill.task;
  task.value = S.task;
  const expected = params(container);
  await doEstimate(container, api, () => (
    reportRouteIsActive(["report", String(route.reportId), String(route.recommendation)])
    && S.prefillKey === key
    && S.projectId === S.catalog?.active_project_id
    && S.team === expected.team
    && S.workflow === expected.workflow
    && S.task.trim() === expected.task
    && task.isConnected
    && task.value.trim() === expected.task
  ));
}

function params(container) {
  const task = container.querySelector("#st-task")?.value.trim() || "";
  const b = container.querySelector("#st-budget")?.value;
  const budget_usd = b ? Number(b) : null;
  return { team: S.team, workflow: S.workflow, task, budget_usd };
}

async function doEstimate(container, api, responseIsCurrent = null) {
  const estimateGeneration = ++_estimateGeneration;
  const projectId = S.projectId;
  const authorityToken = typeof api.authorityToken === "function" ? api.authorityToken() : null;
  const p = params(container);
  const q = new URLSearchParams({ team: p.team, workflow: p.workflow, task: p.task });
  if (p.budget_usd != null) q.set("budget_usd", String(p.budget_usd));
  const r = await api.get(`/api/orchestration/estimate?${q.toString()}`);
  if (
    estimateGeneration !== _estimateGeneration
    || projectId !== S.projectId
    || (authorityToken !== null && typeof api.authorityIsCurrent === "function"
      && !api.authorityIsCurrent(authorityToken))
    || (typeof api.renderIsCurrent === "function" && !api.renderIsCurrent())
    || (responseIsCurrent && !responseIsCurrent())
  ) return;
  S.estimate = r && r.ok ? r.estimate : { decision: "error", reason: (r && r.message) || "failed" };
  renderEstimatePanel(container);
}

async function doRun(container, api, confirmed, confirmation = null) {
  if (_runOperation) return;
  if (confirmed && (!confirmation || S.confirmation !== confirmation)) return;
  const p = confirmed ? confirmation.params : params(container);
  const projectId = S.projectId;
  const authorityToken = typeof api.authorityToken === "function" ? api.authorityToken() : null;
  const operation = {
    id: ++_runSequence, authorityToken, projectId, params: { ...p }, confirmed, confirmation,
  };
  _runOperation = operation;
  renderRunControls(container);
  let r;
  try {
    r = await api.post("/api/orchestration/run", { ...p, confirmed });
  } catch {
    r = { ok: false, status: 0, data: { message: "run request failed" } };
  }
  const authorityCurrent = authorityToken === null
    || typeof api.authorityIsCurrent !== "function"
    || api.authorityIsCurrent(authorityToken);
  if (_runOperation !== operation || !authorityCurrent || projectId !== S.projectId
      || authorityToken !== S.authorityToken) return;
  _runOperation = null;
  const liveContainer = _activeContainer?.isConnected ? _activeContainer : null;
  S.confirmation = null;
  if (r.status === 200 && r.data?.needs_confirmation) {
    S.estimate = { ...r.data.estimate, _needs_confirm: true };
    // A cost prompt is useful only while its exact unchanged draft is still visible. A committed
    // launch response is reconciled below even after a harmless rerender, but an unseen prompt is
    // deliberately dropped so returning to Studio requires a fresh estimate/run decision.
    if (liveContainer && sameParams(draftParams(), p)) {
      S.confirmation = {
        api: _activeApi || api, container: liveContainer, authorityToken, projectId,
        params: { ...p },
      };
      renderEstimatePanel(liveContainer);
      renderRunControls(liveContainer);
    } else {
      S.estimate = null;
    }
    return;
  }
  if (!r.ok) {
    S.estimate = { decision: "error", reason: r.data?.message || `HTTP ${r.status}` };
    if (liveContainer) {
      renderEstimatePanel(liveContainer);
      renderRunControls(liveContainer);
    }
    return;
  }
  S.estimate = r.data?.estimate || null;
  if (liveContainer) {
    renderEstimatePanel(liveContainer);
    renderRunControls(liveContainer);
  }
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
     <td class="dim num">×${m.turns}</td>
     <td class="num">${money(m.model_usd)}</td></tr>`).join("");
  const confirmation = S.confirmation;
  const confirmBtn = (e.decision === "confirm" && e._needs_confirm && confirmation)
    ? `<button id="st-confirm" class="btn-always">Confirm & run (${money(e.total_usd)})</button>` : "";
  el.innerHTML = `<div class="est-panel ${cls}">
    <div><b>Worst case: ${money(e.total_usd)}</b> · ${esc(e.decision)}</div>
    <div class="dim">${esc(e.reason || "")}</div>
    ${e.unpriced && e.unpriced.length ? `<div class="warn dim">unpriced: ${esc(e.unpriced.join(", "))}</div>` : ""}
    <table class="studio-table">${members}</table>
    ${confirmBtn}</div>`;
  el.querySelector("#st-confirm")?.addEventListener(
    "click",
    () => doRun(container, confirmation.api, true, confirmation),
  );
  renderRunControls(container);
}

function renderLive(container, api) {
  const el = container.querySelector("#st-live");
  if (!el || !S.live) { if (el) el.innerHTML = ""; return; }
  const L = S.live;
  const liveRunId = runId(L.run_id);
  const stopping = Boolean(_cancelOperation
    && _cancelOperation.authorityToken === S.authorityToken
    && _cancelOperation.runId === liveRunId);
  const canCancel = L.status === "running"
    && liveRunId !== null
    && runId(S.catalog?.cancellable_run_id) === liveRunId
    && S.cancelUnavailableRunId !== liveRunId;
  const stages = ["council", "synthesis", "execution", "review", "verdict", "done"];
  const timeline = stages.map((s) => {
    const active = String(L.stage).startsWith(s);
    const done = stages.indexOf(s) < stages.indexOf(String(L.stage).split(" ")[0]);
    return `<span class="stage ${active ? "on" : done ? "past" : ""}">${s}</span>`;
  }).join("<span class='arr'>→</span>");
  const agents = (L.agents || []).map((a) =>
    `<span class="chip ${a.ok ? "" : "warn"}">${esc(a.role)}·${esc(a.stage)}${a.ok ? "" : " ✗"}</span>`).join(" ");
  const atHead = ["synthesis", "verdict", "done"].some((s) => String(L.stage).startsWith(s));
  const cancelControl = canCancel || stopping
    ? `<button type="button" class="plain-button warning studio-cancel-run"
         data-studio-cancel-run="${liveRunId}" ${stopping ? "disabled" : ""}
         aria-label="Stop Studio run ${liveRunId}"
         title="Stops only this Studio run. Chat and schedules are unchanged.">${stopping ? "Stopping…" : "Stop this run"}</button>`
    : "";
  const cancelCopy = S.cancelNotice || (canCancel
    ? "Stops only this Studio run. Chat and schedules keep running."
    : "");
  const cancelActions = cancelControl || cancelCopy
    ? `<div class="studio-live-actions">${cancelControl}
         <div class="run-actions-note dim" role="status" aria-live="polite">${esc(cancelCopy)}</div></div>`
    : "";
  el.innerHTML = `<div class="card rise live">
    <div class="card-label">Live · ${esc(L.title || "run")} ${statusPill(L.status, L.verdict)}</div>
    <div class="timeline">${timeline}</div>
    ${atHead ? `<div class="head-line">Head reviewer ${headBadge(S.head)}</div>` : ""}
    <div class="studio-agent-list">${agents}</div>${cancelActions}</div>`;
  const cancelButton = el.querySelector("[data-studio-cancel-run]");
  cancelButton?.addEventListener("click", () => {
    void cancelLiveRun(container, api, cancelButton, liveRunId, S.authorityToken);
  });
}

function cancelOperationIsCurrent(operation, api) {
  const authorityCurrent = operation.authorityToken === null
    || typeof api.authorityIsCurrent !== "function"
    || api.authorityIsCurrent(operation.authorityToken);
  return _cancelOperation === operation
    && authorityCurrent
    && S.authorityToken === operation.authorityToken
    && S.projectId === operation.projectId
    && runId(S.live?.run_id) === operation.runId
    && S.live?.status === "running";
}

function renderCurrentLive(fallbackApi) {
  const container = _activeContainer?.isConnected ? _activeContainer : null;
  if (container?.querySelector("#st-live")) renderLive(container, _activeApi || fallbackApi);
}

function settleLiveRun(runIdValue, status, verdict = null) {
  const exactRunId = runId(runIdValue);
  const terminalStatus = terminalRunStatus(status);
  if (exactRunId === null || runId(S.live?.run_id) !== exactRunId || terminalStatus === null) {
    return false;
  }
  S.live.stage = "done";
  S.live.status = terminalStatus;
  S.live.verdict = verdict;
  if (
    _cancelOperation
    && _cancelOperation.runId === exactRunId
    && _cancelOperation.authorityToken === S.authorityToken
  ) {
    retireCancelOperation(_cancelOperation);
  }
  if (runId(S.catalog?.cancellable_run_id) === exactRunId) {
    S.catalog.cancellable_run_id = null;
  }
  S.cancelUnavailableRunId = null;
  S.cancelNotice = terminalStatus === "cancelled"
    ? "Run stopped. Completed work remains in this run record."
    : `Run finished as ${humanStatus(terminalStatus)} before the stop request settled.`;
  return true;
}

async function cancelLiveRun(container, api, button, liveRunId, authorityToken) {
  if (
    _cancelOperation
    || liveRunId === null
    || !button.isConnected
    || _activeContainer !== container
    || S.authorityToken !== authorityToken
    || runId(S.live?.run_id) !== liveRunId
    || S.live?.status !== "running"
    || runId(S.catalog?.cancellable_run_id) !== liveRunId
    || (authorityToken !== null && typeof api.authorityIsCurrent === "function"
      && !api.authorityIsCurrent(authorityToken))
    || (typeof api.renderIsCurrent === "function" && !api.renderIsCurrent())
  ) return;
  const operation = {
    authorityToken,
    projectId: S.projectId,
    runId: liveRunId,
    reconcileTimer: null,
    reconcileAbort: null,
  };
  _cancelOperation = operation;
  S.cancelNotice = null;
  renderCurrentLive(api);

  let response;
  try {
    response = await api.post(`/api/orchestration/${liveRunId}/cancel`, {});
  } catch {
    response = { ok: false, status: 0, data: { message: "stop request failed" } };
  }
  if (!cancelOperationIsCurrent(operation, api)) return;

  const responseRunId = runId(response.data?.run_id);
  const responseStatus = terminalRunStatus(response.data?.status);
  if (
    response.ok
    && response.data?.state === "settled"
    && responseRunId === liveRunId
    && responseStatus
    && response.data.cancelled === (responseStatus === "cancelled")
  ) {
    settleLiveRun(liveRunId, responseStatus);
    renderCurrentLive(api);
    return;
  }
  if (
    response.ok
    && response.status === 202
    && response.data?.state === "stop_requested"
    && responseRunId === liveRunId
    && response.data.status === "running"
    && response.data.cancelled === false
    && response.data.stop_requested === true
  ) {
    S.cancelNotice = "Stop requested. Waiting for the run to finish safely; refresh if this takes a while.";
    renderCurrentLive(api);
    scheduleCancelReconciliation(operation, api, 0);
    return;
  }

  let detail = null;
  try {
    detail = await readCancelDetail(operation, api);
  } catch { /* an ambiguous write remains ambiguous until another read succeeds */ }
  if (!cancelOperationIsCurrent(operation, api)) return;
  const detailStatus = runId(detail?.run?.id) === liveRunId
    ? terminalRunStatus(detail.run.status)
    : null;
  retireCancelOperation(operation);
  if (detailStatus) {
    settleLiveRun(liveRunId, detailStatus, detail.run.verdict);
    renderCurrentLive(api);
    return;
  }
  const definitivelyRejected = response.data?.state === "not_cancellable";
  if (definitivelyRejected) {
    S.cancelUnavailableRunId = liveRunId;
    if (runId(S.catalog?.cancellable_run_id) === liveRunId) {
      S.catalog.cancellable_run_id = null;
    }
    S.cancelNotice = "Kira did not accept this stop request. The run may already be finishing; refresh to confirm.";
  } else {
    S.cancelNotice = "Could not confirm the stop request. The run may still be active; try again.";
  }
  renderCurrentLive(api);
}

function scheduleCancelReconciliation(operation, api, attempt) {
  if (!cancelOperationIsCurrent(operation, api)) return;
  const delay = CANCEL_RECONCILE_DELAYS_MS[attempt];
  if (delay === undefined || operation.reconcileTimer !== null) return;
  operation.reconcileTimer = setTimeout(() => {
    operation.reconcileTimer = null;
    void reconcileCancelOperation(operation, api, attempt);
  }, delay);
}

async function reconcileCancelOperation(operation, api, attempt) {
  if (!cancelOperationIsCurrent(operation, api)) return;
  let detail = null;
  try {
    detail = await readCancelDetail(operation, api);
  } catch { /* retry bounded exact reads; a terminal event may still settle the operation */ }
  if (!cancelOperationIsCurrent(operation, api)) return;

  const exactDetail = runId(detail?.run?.id) === operation.runId;
  const detailStatus = exactDetail ? terminalRunStatus(detail.run.status) : null;
  if (detailStatus) {
    settleLiveRun(operation.runId, detailStatus, detail.run.verdict);
    renderCurrentLive(api);
    return;
  }

  const nextAttempt = attempt + 1;
  if (nextAttempt < CANCEL_RECONCILE_DELAYS_MS.length) {
    scheduleCancelReconciliation(operation, api, nextAttempt);
    return;
  }

  retireCancelOperation(operation);
  S.cancelNotice = exactDetail && detail.run.status === "running"
    ? "Stop was requested, but final status is still pending. You can retry or refresh."
    : "Could not confirm the final run status. Refresh or try the stop request again.";
  renderCurrentLive(api);
}

async function readCancelDetail(operation, api) {
  if (!cancelOperationIsCurrent(operation, api)) return null;
  const controller = new AbortController();
  operation.reconcileAbort = controller;
  const timeout = setTimeout(() => controller.abort(), CANCEL_RECONCILE_READ_TIMEOUT_MS);
  try {
    return await api.get(`/api/orchestration/${operation.runId}`, {
      signal: controller.signal,
    });
  } finally {
    clearTimeout(timeout);
    if (operation.reconcileAbort === controller) operation.reconcileAbort = null;
  }
}

function humanStatus(status) {
  return String(status || "unknown").replace(/_/g, " ");
}

function renderDetail(container, api, authorityToken) {
  const el = container.querySelector("#st-detail");
  if (!el || !S.detail || !S.detail.run) { if (el) el.innerHTML = ""; return; }
  const detail = S.detail;
  const r = detail.run;
  const ownsDetail = () => (
    S.detail === detail
    && S.authorityToken === authorityToken
    && (authorityToken === null || typeof api.authorityIsCurrent !== "function"
      || api.authorityIsCurrent(authorityToken))
    && (typeof api.renderIsCurrent !== "function" || api.renderIsCurrent())
  );
  const money = (n) => (n == null ? "—" : `$${n.toFixed(4)}`);
  const recordedSkills = (entries) => (entries || []).map((skill) =>
    `<span class="chip dim" title="pack ${esc(skill.sha256)} · compiled ${esc(skill.compiled_sha256)}">${esc(skill.pack)} v${esc(skill.version)} · ${esc(skill.member)}/${esc(skill.stage)}</span>`).join(" ");
  const members = (S.detail.members || []).map((m) =>
    `<tr><td><b>${esc(memberLabel(m))}</b><div class="dim mono">${esc(m.role || "?")}</div></td><td class="dim">${esc(m.stage || "")}</td>
    <td>${statusPill(m.status)}</td><td class="dim num">${m.iterations} it · ${m.denied_count} denied</td>
     <td class="num">${money(m.cost_usd)}<div class="dim mono">${esc((m.models || []).join(" / ") || "model not recorded")}</div>
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
    ? `<div class="dim studio-breakdown">by stage: ${(bd.by_stage || [])
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
  const resumeBusy = Boolean(_resumeOperation
    && _resumeOperation.authorityToken === authorityToken
    && _resumeOperation.runId === r.id);
  const resumeBlock = r.can_resume
    ? `<section class="run-actions"><div class="synth-head">Recover safely</div>
        <div class="dim run-actions-note">This run stopped after synthesis and before any writer began. Re-enter the exact original task brief to continue from that bounded synthesis. The original brief and team reports were not stored.</div>
        <textarea id="st-run-resume-task" rows="3" placeholder="Re-enter the exact original task brief"></textarea>
        <button id="st-run-resume" class="plain-button ghost" ${resumeBusy ? "disabled" : ""}>${resumeBusy ? "Continuing…" : "Continue from synthesis"}</button>
        <div id="st-run-resume-note" class="dim run-actions-note"></div></section>`
    : (S.resumeNotice
      ? `<section class="run-actions"><div class="dim run-actions-note">${esc(S.resumeNotice)}</div></section>`
      : "");
  el.innerHTML = `<div class="card rise">
    <div class="card-label">Run #${r.id} · ${esc(r.title)} ${statusPill(r.status, r.verdict)}</div>
    <div class="dim">est ${money(r.estimated_cost_usd)} · actual ${money(r.actual_cost_usd)}
      ${r.budget_usd != null ? ` · cap ${money(r.budget_usd)}` : ""}</div>
    ${roiLine}${bdLine}
    ${r.synthesis_summary ? `<div class="synth"><div class="synth-head">What the team found ${headBadge(S.head)}</div>${esc(r.synthesis_summary)}</div>` : ""}
    ${actionBlock}${resumeBlock}${findingBlock}${verdictBlock}
    <table class="studio-table">${members || '<tr><td class="dim">no members</td></tr>'}</table>
    <div class="dim studio-context">context: ${manifest || "—"}</div>
    <div class="dim studio-context">Skill packs recorded at run start: ${skillManifest || "No skill packs recorded for this run."}</div>
    <div class="dim studio-context-note">Recorded metadata does not prove prompt injection; Shadow mode records manifests without injecting guidance.</div></div>`;
  el.querySelector("[data-project-tasks]")?.addEventListener("click", (event) => {
    if (!ownsDetail()) return;
    const projectId = Number(event.currentTarget.dataset.projectTasks);
    if (Number.isInteger(projectId) && projectId > 0) location.hash = `workspace/${projectId}/tasks`;
  });
  el.querySelectorAll("[data-promote-follow-up]").forEach((button) => {
    button.addEventListener("click", async (event) => {
      const index = Number(event.currentTarget.dataset.promoteFollowUp);
      const item = actionItems[index];
      if (!item || !ownsDetail()) return;
      await openTaskDraft({
        runId: r.id,
        runTitle: r.title || r.workflow || "Team run",
        title: item.title,
        goal: item.goal,
        priority: item.priority,
      }, api);
    });
  });
  el.querySelector("#st-run-resume")?.addEventListener("click", async () => {
    if (_resumeOperation || !ownsDetail()) return;
    const task = el.querySelector("#st-run-resume-task")?.value.trim() || "";
    const note = el.querySelector("#st-run-resume-note");
    if (!task) { if (note) note.textContent = "Re-enter the original task brief first."; return; }
    const button = el.querySelector("#st-run-resume");
    const operation = { authorityToken, runId: r.id, task };
    _resumeOperation = operation;
    S.resumeNotice = null;
    if (button) button.disabled = true;
    let response;
    try {
      response = await api.post(`/api/orchestration/${r.id}/resume`, { task });
    } catch {
      response = { ok: false, status: 0, data: { message: "Continuation request failed." } };
    }
    const authorityCurrent = authorityToken === null
      || typeof api.authorityIsCurrent !== "function"
      || api.authorityIsCurrent(authorityToken);
    const operationCurrent = _resumeOperation === operation;
    if (operationCurrent) _resumeOperation = null;
    if (!operationCurrent || !authorityCurrent
        || S.authorityToken !== authorityToken || S.detail?.run?.id !== r.id) return;
    if (!response.ok) {
      S.resumeNotice = response.data?.message || `HTTP ${response.status}`;
      const liveContainer = _activeContainer?.isConnected ? _activeContainer : null;
      if (liveContainer) renderDetail(liveContainer, _activeApi || api, authorityToken);
      return;
    }
    S.resumeNotice = "Continuation started. The checkpoint has been consumed before any writer runs.";
    S.detail.run.can_resume = false;
    const liveContainer = _activeContainer?.isConnected ? _activeContainer : null;
    if (liveContainer) renderDetail(liveContainer, _activeApi || api, authorityToken);
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
     <td class="dim num">${money(r.actual_cost_usd ?? r.estimated_cost_usd)}</td></tr>`
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
