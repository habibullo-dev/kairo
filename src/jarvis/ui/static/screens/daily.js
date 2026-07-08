// Daily — the command center (Phase 11 T8). Calm, priority-ordered, one primary attention
// surface. Zones top→down: pending approval (amber) → Now (+ cost today) → Briefing → Today →
// recent artifacts → latest run → notices → connector health → what changed → workflows →
// Conversation → sticky composer. Everything from the digest/repo/email/artifacts/notices is
// UNTRUSTED content, rendered with textContent only — never innerHTML, never linkified (a digest
// link would be a phishing/exfil surface). The ONLY action path is the gated POST /api/turn; every
// other card reads or navigates. Detail lives in Trace/Debug.
import { esc } from "../ui/dom.js";
import { money, relTime } from "../ui/format.js";

// Workflow chips are prepared prompts submitted through the SAME gated POST /api/turn — the
// single action path (no new authority). Two are navigation only.
const WORKFLOWS = [
  { label: "Summarize my inbox", prompt: "Use gmail_search and gmail_read to summarize today's unread email. Read-only — do not draft or send anything." },
  { label: "Prepare a reply", prompt: "Find my most recent unread email and prepare a draft reply for my review (gmail_create_draft). Do not send it." },
  { label: "Summarize repo changes", prompt: "Summarize the recent git activity and working-tree changes in this project." },
  { label: "Schedule a reminder", prefill: "Remind me at 5pm to " },
  { label: "Ingest a file", nav: "vault" },
  { label: "Review KB queue", nav: "vault" },
];

// Keys are the real artifact `kind` values the producers emit (wiki_page, digest, eval_report,
// orchestration, meeting_note); anything else falls back to ◆.
const ARTIFACT_ICONS = {
  wiki_page: "📝", digest: "🗞", eval_report: "🧪",
  orchestration: "🧩", meeting_note: "🎙",
};

export function render(container, api) {
  if (!container.querySelector("#composer-input")) {
    container.innerHTML = `
      <div class="rise">
        <h1>Daily</h1>
        <div class="sub">Ask Kairo anything. Risky actions pause here for your approval.</div>
      </div>
      <div id="daily-zones"></div>
      <div class="composer"><div class="box">
        <input id="composer-input" placeholder="Message Kairo…" autocomplete="off">
        <div class="chips"><span>opus-4-8</span><span>effort high</span></div>
        <button class="send" id="composer-send" aria-label="Send">➜</button>
      </div></div>`;
    const input = container.querySelector("#composer-input");
    const send = () => submitText(container, api, input.value.trim(), input);
    container.querySelector("#composer-send").addEventListener("click", send);
    input.addEventListener("keydown", (e) => { if (e.key === "Enter") send(); });
  }
  renderZones(container, api);
}

// Submit a prepared/typed prompt through the one gated turn path.
async function submitText(container, api, text, input) {
  if (!text) return;
  if (input) input.value = "";
  api.state.chat.push({ role: "user", text });
  renderChat(container, api);
  const res = await api.post("/api/turn", { text });
  if (!res.ok) {
    api.state.chat.push({ role: "assistant", text: `— ${res.data.message || "busy"} —` });
    renderChat(container, api);
  }
}

// A designed empty state: heading + a line that teaches the next action.
function emptyState(heading, hint) {
  const box = document.createElement("div");
  box.className = "empty-state";
  const h = document.createElement("h4");
  h.textContent = heading;
  const p = document.createElement("div");
  p.textContent = hint;
  box.append(h, p);
  return box;
}

function renderZones(container, api) {
  const s = api.state;
  const zones = container.querySelector("#daily-zones");
  const busy = s.runner && s.runner.turn_busy;
  const pend = [...s.pending.values()];
  const spend = s.runner && typeof s.runner.today_spend_usd === "number" ? s.runner.today_spend_usd : null;

  let html = "";
  // 1) PENDING APPROVAL — the one primary attention surface (amber), when present.
  if (pend.length) {
    const p = pend[0];
    const more = pend.length > 1 ? ` <span class="dim">+${pend.length - 1} more</span>` : "";
    html += `<div class="zone-pending rise"><div class="ico">⚠</div>
      <div class="body">
        <div class="card-label amber" style="margin-bottom:3px">Waiting on you</div>
        <div class="lead">Kairo wants to <b>${esc(p.tool)}</b>${p.title ? " — " + esc(p.title) : ""}${more}</div>
      </div>
      <button class="btn btn-amber" id="daily-review">Review</button></div>`;
  }
  // 2) NOW — current activity + cost today. Stable IDs so app.js renderRunnerState() writes this
  // card (lead/dot/desc AND the cost metric) from the SAME settled state.runner as the status bar.
  html += `<div class="surface rise"><div class="zone-now">
      <span class="runner-dot${busy ? " busy" : ""}" id="daily-now-dot"></span>
      <div class="body">
        <div class="lead${busy ? "" : " idle"}" id="daily-now-lead">${busy ? "Kairo is working" : "Kairo is idle"}</div>
        <div class="desc" id="daily-now-desc">${busy ? "Working on your request." : "Nothing running. Send a message to begin."}</div>
      </div>
      <div class="metric cost-metric">
        <div class="n" id="daily-cost-today">${spend == null ? "—" : money(spend)}</div>
        <div class="l">spent today</div>
      </div></div></div>`;
  // 3) BRIEFING — the latest digest (filled from /api/daily). Quiet card, no toast.
  html += `<div class="surface rise" id="daily-briefing">
      <div class="panel-title"><h3>Briefing</h3>
        <button class="rowbtn" id="daily-digest-run">Run digest now</button></div>
      <div id="daily-briefing-body" class="dim">Loading…</div></div>`;
  // 4) TODAY — populated from /api/tasks if the scheduler is on (hidden otherwise).
  html += `<div class="surface rise" id="daily-today" style="display:none">
      <div class="panel-title"><h3>Today</h3><a href="#tasks">All tasks →</a></div>
      <div id="daily-today-rows" class="daily-rows"></div></div>`;
  // 5) RECENT ARTIFACTS (new) — newest across projects; a servable one opens its read-only content.
  html += `<div class="surface rise" id="daily-artifacts">
      <div class="panel-title"><h3>Recent artifacts</h3></div>
      <div id="daily-artifacts-body" class="daily-rows"><div class="dim">Loading…</div></div></div>`;
  // 5b) RECENT CHATS (T11) — jump back into a recent conversation.
  html += `<div class="surface rise" id="daily-chats">
      <div class="panel-title"><h3>Recent chats</h3></div>
      <div id="daily-chats-body" class="daily-rows"><div class="dim">Loading…</div></div></div>`;
  // 6) LATEST RUN (new) — the most recent orchestration run; links into Studio.
  html += `<div class="surface rise" id="daily-run">
      <div class="panel-title"><h3>Latest run</h3><a href="#studio">Studio →</a></div>
      <div id="daily-run-body" class="daily-rows"><div class="dim">Loading…</div></div></div>`;
  // 7) NOTICES (new) — background job/reminder/digest notices; hidden when there are none (calm).
  html += `<div class="surface rise" id="daily-notices" style="display:none">
      <div class="panel-title"><h3>Notices</h3></div>
      <div id="daily-notices-body" class="daily-rows"></div></div>`;
  // 8) CONNECTOR HEALTH (new) — presence booleans from the hub read model (never a key value).
  html += `<div class="surface rise" id="daily-connectors">
      <div class="panel-title"><h3>Connectors</h3><a href="#hub">Hub →</a></div>
      <div id="daily-connectors-body"><div class="dim">Loading…</div></div></div>`;
  // 9) WHAT CHANGED — repo state + eval freshness + KB review (filled from /api/daily).
  html += `<div class="surface rise" id="daily-changed">
      <div class="panel-title"><h3>What changed</h3></div>
      <div id="daily-changed-body" class="dim">Loading…</div></div>`;
  // 10) WORKFLOWS — prepared prompts (through /api/turn) + navigation shortcuts.
  html += `<div class="surface rise"><div class="panel-title"><h3>Workflows</h3></div>
      <div class="chip-row" id="daily-workflows"></div></div>`;
  // 11) CONVERSATION
  html += `<div class="rise"><div class="panel-title" style="margin-bottom:14px"><h3>Conversation</h3></div>
      <div class="chat" id="daily-chat"></div></div>`;
  zones.innerHTML = html;

  const review = container.querySelector("#daily-review");
  if (review) review.addEventListener("click", () => api.reviewPending());
  container.querySelector("#daily-digest-run").addEventListener("click", async (e) => {
    const btn = e.currentTarget;
    btn.disabled = true; btn.textContent = "Running…";
    const res = await api.post("/api/digest/run");
    btn.disabled = false; btn.textContent = "Run digest now";
    if (res.ok) fillDaily(container, api);  // refresh the Briefing (no reload)
  });
  renderWorkflows(container, api);
  renderChat(container, api);
  fillNotices(container, api);   // client-side (state.notices) — instant
  scheduleFills(container, api); // coalesce the read-only GETs (see below)
}

// renderZones re-runs on every WS event, including each streaming text_delta. Coalesce the
// read-only data fetches (/api/daily, /api/tasks) so a streaming turn can't fire two GETs per
// token — the chat itself still updates immediately (renderChat, above).
let _fillTimer = null;
function scheduleFills(container, api) {
  if (_fillTimer) clearTimeout(_fillTimer);
  _fillTimer = setTimeout(() => {
    _fillTimer = null;
    fillToday(container, api);
    fillChats(container, api);
    fillDaily(container, api);
  }, 200);
}

function renderWorkflows(container, api) {
  const row = container.querySelector("#daily-workflows");
  if (!row) return;
  row.innerHTML = "";
  for (const wf of WORKFLOWS) {
    const b = document.createElement("button");
    b.className = "chip-btn";
    b.textContent = wf.label;
    b.addEventListener("click", () => {
      if (wf.nav) { location.hash = wf.nav; return; }
      if (wf.prefill) {
        const input = container.querySelector("#composer-input");
        input.value = wf.prefill; input.focus(); return;
      }
      submitText(container, api, wf.prompt);
    });
    row.appendChild(b);
  }
}

async function fillDaily(container, api) {
  const data = await api.get("/api/daily");
  if (!data) {
    // The overview didn't load — never leave the cards stuck on "Loading…".
    for (const id of ["daily-briefing-body", "daily-artifacts-body", "daily-run-body",
      "daily-connectors-body", "daily-changed-body"]) {
      const b = container.querySelector(`#${id}`);
      if (b) {
        b.textContent = "";
        b.className = "";
        b.appendChild(emptyState("Unavailable", "Couldn't load your daily overview — it'll refresh shortly."));
      }
    }
    return;
  }
  fillBriefing(container, data);
  fillArtifacts(container, data);
  fillRun(container, data);
  fillConnectors(container, data);
  fillChanged(container, data);
}

function fillBriefing(container, data) {
  const body = container.querySelector("#daily-briefing-body");
  if (!body) return;
  body.innerHTML = "";
  body.className = "";
  if (data.demo) {
    const badge = document.createElement("span");
    badge.className = "tag amber";
    badge.textContent = "Demo data — not your real accounts";
    body.appendChild(badge);
  }
  const d = data.digest;
  if (!d) {
    body.appendChild(emptyState("No briefing yet", "Run a digest to see your morning summary of schedule, email and tasks."));
    return;
  }
  const summary = document.createElement("div");
  summary.className = "briefing-summary";
  summary.textContent = d.summary;  // textContent — untrusted, never HTML/linkified
  body.appendChild(summary);
  // Section counts (schedule/email/tasks/…): title + a count or the friendly failure reason.
  for (const sec of d.sections || []) {
    const line = document.createElement("div");
    line.className = "briefing-line" + (sec.status !== "ok" ? " warn" : "");
    const label = sec.status === "ok" ? `${sec.items.length}` : (sec.reason || sec.status);
    line.textContent = `${sec.title}: ${label}`;
    body.appendChild(line);
  }
  // Suggested actions — plain-text chips, DISPLAY ONLY (never executed).
  for (const action of d.suggested_actions || []) {
    const chip = document.createElement("div");
    chip.className = "suggest-chip";
    chip.textContent = action;
    body.appendChild(chip);
  }
}

function fillArtifacts(container, data) {
  const body = container.querySelector("#daily-artifacts-body");
  if (!body) return;
  body.textContent = "";
  const arts = data.recent_artifacts || [];
  if (!arts.length) {
    body.appendChild(emptyState("No artifacts yet", "Reports, drafts and outputs Kairo produces are filed here."));
    return;
  }
  for (const a of arts.slice(0, 5)) {
    const row = document.createElement("div");
    row.className = "list-row";
    const icon = document.createElement("span");
    icon.className = "list-icon";
    icon.textContent = ARTIFACT_ICONS[a.kind] || "◆";
    const mid = document.createElement("div");
    mid.style.minWidth = "0";
    const t = document.createElement("div");
    t.className = "lr-t";
    t.textContent = a.title || "(untitled)";
    const sub = document.createElement("div");
    sub.className = "lr-s";
    sub.textContent = `${a.kind}${a.pinned ? " · pinned" : ""}${a.created_at ? " · " + relTime(a.created_at) : ""}`;
    mid.append(t, sub);
    const chip = document.createElement("span");
    chip.className = "p-chip";
    chip.textContent = a.has_content ? "open" : "";
    row.append(icon, mid, chip);
    // Only a locally-stored artifact opens (its hardened, read-only /content GET, same-origin).
    // external_uri artifacts (digest/orchestration deep-links) are display-only — never auto-open
    // an arbitrary URI (a linkified digest would be a phishing/exfil surface).
    if (a.has_content) {
      row.style.cursor = "pointer";
      row.addEventListener("click", () =>
        window.open(`/api/artifacts/${encodeURIComponent(a.id)}/content`, "_blank", "noopener"),
      );
    }
    body.appendChild(row);
  }
}

function fillRun(container, data) {
  const body = container.querySelector("#daily-run-body");
  if (!body) return;
  body.textContent = "";
  const run = data.latest_run;
  if (!run) {
    body.appendChild(emptyState("No runs yet", "Assemble a team in Studio to run a multi-agent workflow."));
    return;
  }
  const row = document.createElement("div");
  row.className = "list-row";
  const pill = document.createElement("span");
  pill.className = "status-pill " + runTone(run.status);
  pill.textContent = run.status || "—";
  const mid = document.createElement("div");
  mid.style.minWidth = "0";
  const t = document.createElement("div");
  t.className = "lr-t";
  t.textContent = run.title || run.workflow || "Run";
  const sub = document.createElement("div");
  sub.className = "lr-s";
  const team = run.team ? `${run.team} · ` : "";
  sub.textContent = `${team}${run.workflow || ""}${run.finished_at ? " · " + relTime(run.finished_at) : ""}`;
  mid.append(t, sub);
  const cost = document.createElement("span");
  cost.className = "p-chip";
  cost.textContent = money(run.actual_cost_usd != null ? run.actual_cost_usd : run.estimated_cost_usd);
  row.append(pill, mid, cost);
  row.style.cursor = "pointer";
  row.addEventListener("click", () => { location.hash = "studio"; });
  body.appendChild(row);
}

// Map the real OrchestrationRun status vocabulary (running/ok/rejected/revise/error/cancelled/
// aborted/budget_stopped) to a pill tone.
function runTone(status) {
  if (status === "ok") return "good";
  if (status === "running" || status === "revise") return "busy";
  if (status === "error" || status === "rejected" || status === "aborted" ||
      status === "budget_stopped" || status === "cancelled") return "danger";
  return "";
}

function fillNotices(container, api) {
  const card = container.querySelector("#daily-notices");
  const body = container.querySelector("#daily-notices-body");
  if (!card || !body) return;
  const notices = api.state.notices || [];
  if (!notices.length) { card.style.display = "none"; return; }
  card.style.display = "";
  body.textContent = "";
  for (const n of notices.slice(0, 5)) {
    const row = document.createElement("div");
    row.className = "list-row";
    const chip = document.createElement("span");
    chip.className = "p-chip";
    chip.textContent = n.kind || "notice";
    const mid = document.createElement("div");
    mid.style.minWidth = "0";
    const t = document.createElement("div");
    t.className = "lr-t";
    t.textContent = n.title || n.summary || n.message || n.text || n.kind || "Notice";
    mid.appendChild(t);
    const when = n.ts || n.created_at || n.at;
    if (when) {
      const sub = document.createElement("div");
      sub.className = "lr-s";
      sub.textContent = relTime(when);
      mid.appendChild(sub);
    }
    row.append(chip, mid, document.createElement("span"));
    body.appendChild(row);
  }
}

// Connected-state from the registry's presence status (a dict, NOT a bool): honour
// connected/configured and needs_reconnect. A present-but-unconfigured connector reads as NOT
// connected (grey), never a false green. Presence-only — no key/token value is ever inspected.
function connOn(status) {
  if (status == null) return false;
  if (typeof status === "boolean") return status;
  const base = status.connected ?? status.configured ?? true; // a present dict w/o those = configured
  return !!base && !status.needs_reconnect;
}

function fillConnectors(container, data) {
  const body = container.querySelector("#daily-connectors-body");
  if (!body) return;
  body.textContent = "";
  const c = data.connectors || {};
  // Only pill connectors that are actually PRESENT, so a fresh machine reaches the empty state.
  const pills = [];
  if (data.demo) pills.push(connPill("Demo mode", true));
  if (c.google != null) pills.push(connPill("Google", connOn(c.google)));
  for (const [name, status] of Object.entries(c.notifiers || {})) {
    pills.push(connPill(name, connOn(status)));
  }
  if (!pills.length) {
    body.appendChild(emptyState("No connectors configured", "Connect accounts in the Hub to enrich your briefing."));
    return;
  }
  const strip = document.createElement("div");
  strip.className = "conn-strip";
  for (const p of pills) strip.appendChild(p);
  body.appendChild(strip);
}

function connPill(name, on) {
  const pill = document.createElement("span");
  pill.className = "status-pill " + (on ? "good" : "");
  const dot = document.createElement("span");
  dot.className = "dot" + (on ? "" : " off"); // grey, un-glowed when not connected — never a false green
  const label = document.createElement("span");
  label.textContent = name;
  pill.append(dot, label);
  return pill;
}

function fillChanged(container, data) {
  const body = container.querySelector("#daily-changed-body");
  if (!body) return;
  body.innerHTML = "";
  body.className = "";
  // Repo cards — branch/dirty + recent commit subjects (subjects are untrusted → textContent).
  for (const r of data.repos || []) {
    const row = document.createElement("div");
    row.className = "repo-row";
    if (!r.state) {
      row.className = "dim";
      row.textContent = `${r.path}: not a git repo`;
    } else {
      const head = document.createElement("div");
      head.textContent = `${r.path} · ${r.state.branch} @ ${r.state.head_rev} · ${r.state.dirty_files} dirty`;
      row.appendChild(head);
      for (const c of (r.state.recent_commits || []).slice(0, 3)) {
        const line = document.createElement("div");
        line.className = "commit dim";
        line.textContent = `${c.short_rev}  ${c.subject}`;  // untrusted subject as text
        row.appendChild(line);
      }
    }
    body.appendChild(row);
  }
  // Eval freshness — a CHIP with the copy-command, never a run button (ADR-0005).
  const evals = data.evals || {};
  const chip = document.createElement("div");
  chip.className = "eval-chip" + (evals.stale ? " stale" : "");
  const label = document.createElement("span");
  label.textContent = !evals.ever_run
    ? "Evals never run"
    : evals.stale ? "Evals not run at HEAD" : `Evals current · ${evals.verdict || ""}`;
  chip.appendChild(label);
  const cmd = document.createElement("code");
  cmd.className = "eval-cmd";
  cmd.textContent = evals.command || "jarvis eval gate";
  const copy = document.createElement("button");
  copy.className = "rowbtn";
  copy.textContent = "Copy";
  copy.addEventListener("click", () => navigator.clipboard && navigator.clipboard.writeText(cmd.textContent));
  chip.append(cmd, copy);
  body.appendChild(chip);
  // Projected eval cost (cost-control layer): default replay = $0; live gate cost estimated
  // from the last run. Informational — the eval is a CLI ritual, never run from the UI.
  if (evals.cost_note) {
    const cost = document.createElement("div");
    cost.className = "eval-cost-note";
    cost.textContent = "💲 " + evals.cost_note.replace(/`/g, "");
    body.appendChild(cost);
  }
  // KB review queue → link to the Vault.
  if (data.kb_review_count) {
    const kb = document.createElement("a");
    kb.href = "#vault";
    kb.className = "review-link";
    kb.textContent = `${data.kb_review_count} source(s) awaiting review →`;
    body.appendChild(kb);
  }
}

function renderChat(container, api) {
  const chat = container.querySelector("#daily-chat");
  if (!chat) return;
  chat.innerHTML = "";
  if (!api.state.chat.length) {
    chat.appendChild(emptyState("No messages yet", "Ask Kairo anything, or pick a workflow above to get started."));
    return;
  }
  for (const item of api.state.chat) {
    const div = document.createElement("div");
    if (item.tool) {
      div.className = "toolline" + (item.resolution === "denied" ? " deny" : "");
      div.textContent = `${item.tool} · ${item.resolution}`;
    } else {
      div.className = "msg " + item.role;
      div.textContent = item.text;
    }
    chat.appendChild(div);
  }
}

async function fillToday(container, api) {
  const rows = await api.get("/api/tasks");
  const card = container.querySelector("#daily-today");
  if (!card) return;
  const active = Array.isArray(rows) ? rows.filter((t) => t.status === "active").slice(0, 4) : [];
  if (!active.length) return;  // nothing today ⇒ keep the card hidden (calm)
  card.style.display = "";
  const body = card.querySelector("#daily-today-rows");
  body.innerHTML = "";
  for (const t of active) {
    const row = document.createElement("div");
    row.className = "today-row";
    row.innerHTML = `<span class="time">${esc(shortTime(t.next_run_at))}</span>
      <span class="rdot"></span><span class="title">${esc(t.title)}</span>
      <span class="kind">${esc(t.kind)}</span>`;
    body.appendChild(row);
  }
}

async function fillChats(container, api) {
  const body = container.querySelector("#daily-chats-body");
  if (!body) return;
  const data = await api.get("/api/sessions?limit=6");
  body.textContent = "";
  const chats = (data && data.sessions) || [];
  if (!chats.length) {
    body.appendChild(emptyState("No chats yet", "Your recent conversations will appear here."));
    return;
  }
  for (const s of chats.slice(0, 5)) {
    const row = document.createElement("div");
    row.className = "list-row";
    const icon = document.createElement("span");
    icon.className = "list-icon";
    icon.textContent = "💬";
    const mid = document.createElement("div");
    mid.style.minWidth = "0";
    const t = document.createElement("div");
    t.className = "lr-t";
    t.textContent = s.title || "(untitled)";
    const sub = document.createElement("div");
    sub.className = "lr-s";
    sub.textContent = s.updated_at ? relTime(s.updated_at) : "";
    mid.append(t, sub);
    const resume = document.createElement("button");
    resume.className = "plain-button ghost";
    resume.textContent = "Resume";
    resume.addEventListener("click", async () => {
      // Resume loads the chat into the live session AND its transcript into this view; we're
      // already on Daily so re-render to show the loaded conversation.
      if (await api.resumeChat(s.id)) {
        location.hash = "daily";
        render(container, api);
      }
    });
    row.append(icon, mid, resume);
    body.appendChild(row);
  }
}

function shortTime(iso) {
  if (!iso) return "—";
  const m = /T(\d{2}:\d{2})/.exec(iso);
  return m ? m[1] : iso.slice(0, 10);
}

// Called by app.js for every streamed loop event; keeps Daily quiet (summary lines only).
export function onEvent(state, evt) {
  if (evt.type === "text_delta") {
    let last = state.chat[state.chat.length - 1];
    if (!last || last.role !== "assistant" || !last.live) {
      last = { role: "assistant", text: "", live: true };
      state.chat.push(last);
    }
    last.text += evt.text;
  } else if (evt.type === "tool_started") {
    state.chat.push({ tool: evt.name, resolution: "allow" });
  } else if (evt.type === "tool_decision" && evt.resolution === "deny") {
    state.chat.push({ tool: evt.name, resolution: "denied" });
  } else if (evt.type === "turn_completed") {
    const last = state.chat[state.chat.length - 1];
    if (last && last.role === "assistant") last.live = false;
  }
}
