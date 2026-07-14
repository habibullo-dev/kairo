// Daily — Kairo's calm briefing. Chat owns conversation and actions; Daily only orients the
// person toward the day, their current project, and the one thing needing attention.
import { showToast } from "../ui/feedback.js";
import { money, relTime } from "../ui/format.js";
import { openProjectReport } from "../ui/project-report.js";

let briefingRefreshOperation = null;
let briefingRefreshSequence = 0;
let briefingReadRevision = 0;
let tasksReadRevision = 0;
const routeApiByContainer = new WeakMap();
let activeDailyContainer = null;

function renderIsCurrent(container, api) {
  return container.isConnected
    && (typeof api.renderIsCurrent !== "function" || api.renderIsCurrent());
}

function authorityToken(api) {
  return typeof api.authorityToken === "function" ? api.authorityToken() : null;
}

function authorityIsCurrent(api, token) {
  return token === null || typeof api.authorityIsCurrent !== "function"
    || api.authorityIsCurrent(token);
}

function refreshBelongsTo(api) {
  if (!briefingRefreshOperation) return false;
  const token = authorityToken(api);
  return briefingRefreshOperation.authorityToken === token && authorityIsCurrent(api, token);
}

export function render(container, api) {
  activeDailyContainer = container;
  if (!container.querySelector("#daily-briefing")) {
    container.innerHTML = `
      <section class="daily-briefing rise">
        <header class="daily-briefing-head">
          <div><div class="chat-kicker">Today</div><h1>Daily briefing</h1><p class="sub">A quiet start for the work ahead.</p></div>
          <a class="btn btn-cyan" href="#chat">Continue chat</a>
        </header>
        <div id="daily-pending"></div>
        <section class="daily-status" id="daily-status">
          <span class="runner-dot" id="daily-now-dot"></span>
          <div><strong id="daily-now-lead">Kairo is idle</strong><span id="daily-now-desc">Nothing needs your attention right now.</span></div>
          <span class="daily-cost" id="daily-cost-today">—</span>
        </section>
        <section class="daily-grid">
          <article class="surface" id="daily-briefing"><div class="panel-title"><h3>Morning briefing</h3><button class="plain-button ghost" id="daily-briefing-refresh" type="button" title="Run a fresh briefing from your connected sources.">Refresh</button></div><div id="daily-briefing-body"></div></article>
          <article class="surface" id="daily-project"><div class="panel-title"><h3>Active project</h3></div><div id="daily-project-body"></div></article>
          <article class="surface" id="daily-today"><div class="panel-title"><h3>Next tasks</h3><a href="#tasks">All tasks →</a></div><div id="daily-today-rows" class="daily-rows"></div></article>
          <article class="surface" id="daily-notice"><div class="panel-title"><h3>Latest notification</h3><a href="#gate">Notifications →</a></div><div id="daily-notice-body"></div></article>
        </section>
        <nav class="daily-links" aria-label="Daily detail locations">
          <a href="#hub">Connectors</a><a href="#studio">Runs</a><a href="#costs">Costs</a><a href="#vault">Knowledge</a><a class="debug-only" href="#lab">Lab / Debug</a>
        </nav>
      </section>`;
  }
  // The Daily shell is intentionally reused. Keep its one persistent Refresh listener pointed at
  // the latest route facade so a same-workspace handshake does not strand it on an old generation.
  routeApiByContainer.set(container, api);
  renderPending(container, api);
  renderStatus(container, api);
  renderProject(container, api);
  renderNotice(container, api);
  renderBriefingRefresh(container, api);
  scheduleFills(container, api);
}

function renderBriefingRefresh(container, api) {
  const button = container.querySelector("#daily-briefing-refresh");
  if (!button) return;
  const busy = !!api.state.runner?.turn_busy;
  const projectScoped = api.state.context?.project_id != null;
  const refreshing = refreshBelongsTo(api);
  button.disabled = refreshing || busy || projectScoped;
  button.textContent = refreshing ? "Refreshing…" : (projectScoped ? "Global only" : "Refresh");
  button.title = projectScoped
    ? "Daily briefing refresh uses global connected sources. Open the global workspace to run it."
    : (busy
    ? "Wait for the current chat turn to finish before refreshing the briefing."
    : "Run a fresh briefing from your connected sources.");
  if (button.dataset.bound === "true") return;
  button.dataset.bound = "true";
  button.addEventListener("click", () => {
    void refreshBriefing(container, routeApiByContainer.get(container) || api);
  });
}

async function refreshBriefing(container, api) {
  if (refreshBelongsTo(api) || api.state.runner?.turn_busy) return;
  if (api.state.context?.project_id != null) {
    showToast("Open the global workspace to refresh the daily briefing.", "error");
    return;
  }
  const operation = {
    id: ++briefingRefreshSequence,
    authorityToken: authorityToken(api),
  };
  briefingRefreshOperation = operation;
  renderBriefingRefresh(container, api);
  try {
    const result = await api.post("/api/digest/run", {});
    if (briefingRefreshOperation !== operation
        || !authorityIsCurrent(api, operation.authorityToken)) return;
    if (!result.ok) {
      const busy = result.status === 409 || result.data?.message === "busy";
      showToast(busy ? "Kairo is already working. Try refreshing the briefing shortly." : "Briefing refresh failed.", "error");
      return;
    }
    showToast("Briefing refreshed.");
    const liveContainer = activeDailyContainer?.isConnected ? activeDailyContainer : container;
    const liveApi = routeApiByContainer.get(liveContainer) || api;
    await fillBriefing(liveContainer, liveApi);
  } catch {
    if (briefingRefreshOperation === operation
        && authorityIsCurrent(api, operation.authorityToken)) {
      showToast("Briefing refresh failed.", "error");
    }
  } finally {
    if (briefingRefreshOperation === operation) briefingRefreshOperation = null;
    if (!authorityIsCurrent(api, operation.authorityToken)) return;
    const liveContainer = activeDailyContainer?.isConnected ? activeDailyContainer : container;
    const liveApi = routeApiByContainer.get(liveContainer) || api;
    renderBriefingRefresh(liveContainer, liveApi);
  }
}

function clear(host) {
  host.textContent = "";
}

function emptyState(heading, hint, actions = []) {
  const box = document.createElement("div");
  box.className = "empty-state";
  const h = document.createElement("h4");
  h.textContent = heading;
  const p = document.createElement("div");
  p.textContent = hint;
  box.append(h, p);
  if (actions.length) {
    const row = document.createElement("div");
    row.className = "chip-row";
    for (const [label, href] of actions) {
      const a = document.createElement("a");
      a.className = "chip-btn";
      a.href = href;
      a.textContent = label;
      row.appendChild(a);
    }
    box.appendChild(row);
  }
  return box;
}

// One attention surface: approval always wins; otherwise a live run is the sole highlighted item.
function renderPending(container, api) {
  const host = container.querySelector("#daily-pending");
  if (!host) return;
  clear(host);
  const pending = [...api.state.pending.values()];
  if (pending.length) {
    const p = pending[0];
    const card = document.createElement("div");
    card.className = "zone-pending rise";
    const icon = document.createElement("div");
    icon.className = "ico";
    icon.textContent = "⚠";
    const body = document.createElement("div");
    body.className = "body";
    const label = document.createElement("div");
    label.className = "card-label amber";
    label.textContent = "Waiting on you";
    const lead = document.createElement("div");
    lead.className = "lead";
    lead.textContent = p.title ? `${p.tool} — ${p.title}` : (p.tool || "Approval required");
    body.append(label, lead);
    const button = document.createElement("button");
    button.className = "btn btn-amber";
    button.type = "button";
    button.textContent = "Review";
    button.addEventListener("click", () => api.reviewPending());
    card.append(icon, body, button);
    host.appendChild(card);
    return;
  }
  if (api.state.runner && api.state.runner.turn_busy) {
    const card = document.createElement("div");
    card.className = "daily-active rise";
    const text = document.createElement("span");
    text.textContent = "Kairo is working in this chat.";
    const link = document.createElement("a");
    link.href = "#chat";
    link.textContent = "Open chat →";
    card.append(text, link);
    host.appendChild(card);
  }
}

function renderStatus(container, api) {
  const runner = api.state.runner || {};
  const currentBusy = !!runner.turn_busy;
  const globalBusy = currentBusy || !!runner.global_turn_busy;
  const backgroundBusy = !!runner.background_busy;
  const busy = globalBusy || backgroundBusy;
  const statusCurrent = !api.state.runnerStatusError;
  const available = runner.runner_available === true;
  const paused = available && runner.runner_running === false;
  const lead = container.querySelector("#daily-now-lead");
  const desc = container.querySelector("#daily-now-desc");
  const dot = container.querySelector("#daily-now-dot");
  const cost = container.querySelector("#daily-cost-today");
  let leadText = "Kairo is idle";
  let descText = "Your briefing is up to date.";
  if (!statusCurrent) {
    leadText = "Runner status is unavailable";
    descText = busy
      ? "Last known work may still be running. Stop all remains available."
      : "Kairo will retry automatically.";
  } else if (currentBusy) {
    leadText = "Kairo is working";
    descText = "Progress is available in Chat.";
  } else if (globalBusy) {
    leadText = "Kairo is working in another chat";
    descText = "That chat's progress is available in Chat.";
  } else if (backgroundBusy) {
    leadText = "Scheduled work is running";
    descText = "Background work continues independently of this chat.";
  } else if (paused) {
    leadText = "Schedules are paused";
    descText = "Stopped chats stay stopped. Resume schedules when you're ready.";
  } else if (runner.runner_available === false) {
    leadText = "Schedules are unavailable";
    descText = "Global runner controls are unavailable.";
  }
  if (lead) lead.textContent = leadText;
  if (desc) desc.textContent = descText;
  if (dot) dot.className = "runner-dot" + (busy ? " busy" : "");
  if (cost) cost.textContent = typeof runner.today_spend_usd === "number"
    ? `${money(runner.today_spend_usd)} today` : "Cost unavailable";
}

function renderProject(container, api) {
  const host = container.querySelector("#daily-project-body");
  if (!host) return;
  clear(host);
  const project = api.state.runner && api.state.runner.project;
  if (!project || !project.id) {
    host.appendChild(emptyState("Working globally", "Choose a project when this work needs a home.",
      [["Choose project", "#projects"], ["Continue chat", "#chat"]]));
    return;
  }
  const name = document.createElement("div");
  name.className = "daily-project-name";
  name.textContent = project.name || `Project ${project.id}`;
  const links = document.createElement("div");
  links.className = "chip-row";
  for (const [label, suffix] of [["Workspace", ""], ["Knowledge", "/memory"], ["Artifacts", "/artifacts"]]) {
    const a = document.createElement("a");
    a.className = "chip-btn";
    a.href = `#workspace/${project.id}${suffix}`;
    a.textContent = label;
    links.appendChild(a);
  }
  const assessment = document.createElement("div");
  assessment.id = "daily-project-assessment";
  assessment.className = "daily-project-assessment";
  host.append(name, links, assessment);
}

function renderProjectAssessment(container, api, assessment) {
  const host = container.querySelector("#daily-project-assessment");
  if (!host) return;
  clear(host);
  if (!assessment) return;
  const state = assessment.state;
  const copy = {
    disabled: "Automatic project assessment is off.",
    unavailable: "Automatic project assessment is unavailable.",
    queued: "Kairo queued a read-only assessment of this project.",
    running: "Kairo is updating this project's read-only assessment.",
    failed: "The latest project assessment could not complete.",
    idle: "No current assessment. Import or finalize the project to refresh it.",
  };
  if (state !== "ready" || !assessment.report) {
    host.appendChild(elStatus(copy[state] || "Project assessment status is unavailable.", state));
    return;
  }
  const report = assessment.report;
  const block = document.createElement("div");
  block.className = "daily-assessment-ready";
  const label = document.createElement("div");
  label.className = "card-label";
  label.textContent = "Project assessment";
  const summary = document.createElement("p");
  summary.textContent = report.summary_preview || "The current read-only assessment is ready.";
  const counts = report.counts || {};
  const meta = document.createElement("div");
  meta.className = "dim";
  meta.textContent = [
    `${Number(counts.weaknesses) || 0} weaknesses`,
    `${Number(counts.security_candidates) || 0} unvalidated security candidates`,
    `${Number(counts.frontend_backend_gaps) || 0} frontend/backend gaps`,
    `${Number(counts.test_reliability_gaps) || 0} test gaps`,
  ].join(" · ");
  const button = document.createElement("button");
  button.type = "button";
  button.className = "chip-btn";
  button.textContent = "View report";
  button.addEventListener("click", () => { void openProjectReport(api, report.id); });
  block.append(label, summary, meta, button);
  host.appendChild(block);
}

function elStatus(message, state) {
  const line = document.createElement("div");
  line.className = `daily-assessment-state ${state || "unavailable"}`;
  line.textContent = message;
  return line;
}

function renderNotice(container, api) {
  const host = container.querySelector("#daily-notice-body");
  if (!host) return;
  clear(host);
  const notice = (api.state.notices || [])[0];
  if (!notice) {
    host.appendChild(emptyState("No new notifications", "Approvals and important updates will appear here."));
    return;
  }
  const title = document.createElement("div");
  title.className = "lr-t";
  title.textContent = notice.title || notice.summary || notice.message || notice.text || notice.kind || "Notification";
  const when = notice.ts || notice.created_at || notice.at;
  host.appendChild(title);
  if (when) {
    const time = document.createElement("div");
    time.className = "lr-s";
    time.textContent = relTime(when);
    host.appendChild(time);
  }
}

let fillTimer = null;
function scheduleFills(container, api) {
  if (fillTimer) clearTimeout(fillTimer);
  // A rerender supersedes both old responses immediately, including the debounce interval.
  briefingReadRevision += 1;
  tasksReadRevision += 1;
  fillTimer = setTimeout(() => {
    fillTimer = null;
    if (!renderIsCurrent(container, api)) return;
    fillBriefing(container, api);
    fillToday(container, api);
  }, 150);
}

async function fillBriefing(container, api) {
  const host = container.querySelector("#daily-briefing-body");
  if (!host) return;
  const revision = ++briefingReadRevision;
  const data = await api.get("/api/daily");
  if (revision !== briefingReadRevision || !renderIsCurrent(container, api)) return;
  const projectScoped = api.state.context?.project_id != null;
  renderProjectAssessment(
    container,
    api,
    data ? data.project_assessment : (projectScoped ? { state: "unavailable" } : null),
  );
  if (!data) {
    clear(host);
    host.appendChild(emptyState("Briefing unavailable", "Open Chat while Kairo refreshes this overview.", [["Continue chat", "#chat"]]));
    return;
  }
  clear(host);
  const digest = data.digest;
  if (!digest) {
    host.appendChild(emptyState("No briefing yet", "Start with Chat, connect Calendar or Gmail, or add a source to Vault.", [
      ["Continue chat", "#chat"], ["Connect Calendar / Gmail", "#hub"], ["Add Vault source", "#vault"],
    ]));
    return;
  }
  const summary = document.createElement("div");
  summary.className = "briefing-summary";
  summary.textContent = digest.summary || "Your briefing is ready.";
  host.appendChild(summary);
  for (const section of (digest.sections || []).slice(0, 3)) {
    const line = document.createElement("div");
    line.className = "briefing-line" + (section.status !== "ok" ? " warn" : "");
    const count = section.status === "ok" ? (section.items || []).length : (section.reason || section.status);
    line.textContent = `${section.title}: ${count}`;
    host.appendChild(line);
  }
}

async function fillToday(container, api) {
  const host = container.querySelector("#daily-today-rows");
  if (!host) return;
  const revision = ++tasksReadRevision;
  const tasks = await api.get("/api/tasks");
  if (revision !== tasksReadRevision || !renderIsCurrent(container, api)) return;
  clear(host);
  if (tasks === null) {
    host.appendChild(emptyState("Tasks unavailable", "Kairo couldn't load scheduled work right now."));
    return;
  }
  const active = Array.isArray(tasks) ? tasks.filter((task) => task.status === "active").slice(0, 4) : [];
  if (!active.length) {
    host.appendChild(emptyState("No next tasks", "Create a task when you want Kairo to keep something on the radar.", [["Create task", "#tasks"]]));
    return;
  }
  for (const task of active) {
    const row = document.createElement("div");
    row.className = "today-row";
    const time = document.createElement("span");
    time.className = "time";
    time.textContent = shortTime(task.next_run_at);
    const dot = document.createElement("span");
    dot.className = "rdot";
    const title = document.createElement("span");
    title.className = "title";
    title.textContent = task.title || "Task";
    row.append(time, dot, title);
    host.appendChild(row);
  }
}

function shortTime(iso) {
  if (!iso) return "—";
  const match = /T(\d{2}:\d{2})/.exec(iso);
  return match ? match[1] : iso.slice(0, 10);
}
