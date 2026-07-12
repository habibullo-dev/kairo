// Daily — Kairo's calm briefing. Chat owns conversation and actions; Daily only orients the
// person toward the day, their current project, and the one thing needing attention.
import { money, relTime } from "../ui/format.js";

export function render(container, api) {
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
          <article class="surface" id="daily-briefing"><div class="panel-title"><h3>Morning briefing</h3></div><div id="daily-briefing-body"></div></article>
          <article class="surface" id="daily-project"><div class="panel-title"><h3>Active project</h3></div><div id="daily-project-body"></div></article>
          <article class="surface" id="daily-today"><div class="panel-title"><h3>Next tasks</h3><a href="#tasks">All tasks →</a></div><div id="daily-today-rows" class="daily-rows"></div></article>
          <article class="surface" id="daily-notice"><div class="panel-title"><h3>Latest notification</h3><a href="#gate">Notifications →</a></div><div id="daily-notice-body"></div></article>
        </section>
        <nav class="daily-links" aria-label="Daily detail locations">
          <a href="#hub">Connectors</a><a href="#studio">Runs</a><a href="#costs">Costs</a><a href="#vault">Knowledge</a><a class="debug-only" href="#lab">Lab / Debug</a>
        </nav>
      </section>`;
  }
  renderPending(container, api);
  renderStatus(container, api);
  renderProject(container, api);
  renderNotice(container, api);
  scheduleFills(container, api);
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
  const busy = !!runner.turn_busy;
  const lead = container.querySelector("#daily-now-lead");
  const desc = container.querySelector("#daily-now-desc");
  const dot = container.querySelector("#daily-now-dot");
  const cost = container.querySelector("#daily-cost-today");
  if (lead) lead.textContent = busy ? "Kairo is working" : "Kairo is idle";
  if (desc) desc.textContent = busy ? "Progress is available in Chat." : "Your briefing is up to date.";
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
  host.append(name, links);
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
  fillTimer = setTimeout(() => {
    fillTimer = null;
    fillBriefing(container, api);
    fillToday(container, api);
  }, 150);
}

async function fillBriefing(container, api) {
  const host = container.querySelector("#daily-briefing-body");
  if (!host) return;
  const data = await api.get("/api/daily");
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
  const tasks = await api.get("/api/tasks");
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
