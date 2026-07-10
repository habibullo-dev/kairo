// Daily — the command center (Phase 11 T8). Calm, priority-ordered, one primary attention
// surface. Zones top→down: pending approval (amber) → Now (+ cost today) → Briefing → Today →
// recent artifacts → latest run → notices → connector health → what changed → workflows →
// Conversation → sticky composer. Everything from the digest/repo/email/artifacts/notices is
// UNTRUSTED content, rendered with textContent only — never innerHTML, never linkified (a digest
// link would be a phishing/exfil surface). The ONLY action path is the gated POST /api/turn; every
// other card reads or navigates. Detail lives in Trace/Debug.
import { esc } from "../ui/dom.js";
import { money, relTime } from "../ui/format.js";
import { mountHeader } from "../ui/header.js";
import { renderConversation, submitConversationTurn } from "./conversation.js";

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
    // Conversation-first shell (Phase 15.5 D6): the amber pending banner (top attention surface),
    // then the CONVERSATION HERO — header + chat + composer as one unit — then the dashboard zones
    // (briefing/tasks/artifacts/run/connectors/cost) as calm secondary context below. The hero is
    // built ONCE and persists (a streaming turn updates only the chat, never re-mounts the header).
    container.innerHTML = `
      <div class="rise">
        <h1>Daily</h1>
        <div class="sub">Ask Kairo anything. Risky actions pause here for your approval.</div>
      </div>
      <div id="daily-pending"></div>
      <section class="convo-hero">
        <div id="daily-convo-header"></div>
        <div class="chat" id="daily-chat"></div>
        <div class="composer"><div class="box">
          <input id="composer-input" placeholder="Message Kairo…" autocomplete="off">
          <div class="live-chips"><span id="composer-model"></span><span id="composer-mode"></span></div>
          <button class="send" id="composer-send" aria-label="Send">➜</button>
        </div></div>
      </section>
      <div id="daily-zones"></div>`;
    const input = container.querySelector("#composer-input");
    const send = () => submitConversationTurn(api, input, () => renderChat(container, api));
    container.querySelector("#composer-send").addEventListener("click", send);
    input.addEventListener("keydown", (e) => { if (e.key === "Enter") send(); });
    mountHeader(container.querySelector("#daily-convo-header"), api,
      { onChanged: () => renderChat(container, api) });
  }
  renderPending(container, api);
  renderChat(container, api);
  renderZones(container, api);
}

// The amber pending-approval banner — the single primary attention surface, in its own persistent
// host ABOVE the conversation hero (never buried under the dashboard). Empty when nothing pends.
function renderPending(container, api) {
  const host = container.querySelector("#daily-pending");
  if (!host) return;
  const pend = [...api.state.pending.values()];
  if (!pend.length) { host.textContent = ""; return; }
  const p = pend[0];
  const more = pend.length > 1 ? ` +${pend.length - 1} more` : "";
  host.innerHTML = `<div class="zone-pending rise"><div class="ico">⚠</div>
    <div class="body">
      <div class="card-label amber" style="margin-bottom:3px">Waiting on you</div>
      <div class="lead">Kairo wants to <b>${esc(p.tool)}</b>${p.title ? " — " + esc(p.title) : ""}${esc(more)}</div>
    </div>
    <button class="btn btn-amber" id="daily-review">Review</button></div>`;
  host.querySelector("#daily-review").addEventListener("click", () => api.reviewPending());
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

// The DASHBOARD below the conversation hero (Phase 15.5 D6): calm, glanceable secondary context.
// Tier order — Now → Briefing/Today (secondary) → Artifacts/Run/Connectors (tertiary) → Notices →
// Workflows → "What changed" (repo/eval noise, DEBUG-ONLY unless urgent). The pending banner and
// the conversation itself live in the persistent hero above, not here.
function renderZones(container, api) {
  const s = api.state;
  const zones = container.querySelector("#daily-zones");
  const busy = s.runner && s.runner.turn_busy;
  const spend = s.runner && typeof s.runner.today_spend_usd === "number" ? s.runner.today_spend_usd : null;

  let html = "";
  // NOW — current activity + cost today. Stable IDs so app.js renderRunnerState() writes this
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
  // BRIEFING (secondary) — the latest digest (filled from /api/daily). Quiet card, no toast.
  html += `<div class="surface rise" id="daily-briefing">
      <div class="panel-title"><h3>Briefing</h3>
        <button class="rowbtn" id="daily-digest-run">Run digest now</button></div>
      <div id="daily-briefing-body" class="dim">Loading…</div></div>`;
  // TODAY (secondary) — from /api/tasks if the scheduler is on (hidden otherwise).
  html += `<div class="surface rise is-hidden" id="daily-today">
      <div class="panel-title"><h3>Today</h3><a href="#tasks">All tasks →</a></div>
      <div id="daily-today-rows" class="daily-rows"></div></div>`;
  // TERTIARY STRIP — active workspace · recent artifacts · latest run · connectors, side by side.
  html += `<div class="daily-tertiary">
    <div class="surface rise" id="daily-workspace">
      <div class="panel-title"><h3>Active workspace</h3></div>
      <div id="daily-workspace-body"></div></div>
    <div class="surface rise" id="daily-artifacts">
      <div class="panel-title"><h3>Recent artifacts</h3><a href="#artifacts">All →</a></div>
      <div id="daily-artifacts-body" class="daily-rows"><div class="dim">Loading…</div></div></div>
    <div class="surface rise" id="daily-run">
      <div class="panel-title"><h3>Latest run</h3><a href="#studio">Studio →</a></div>
      <div id="daily-run-body" class="daily-rows"><div class="dim">Loading…</div></div></div>
    <div class="surface rise" id="daily-connectors">
      <div class="panel-title"><h3>Connectors</h3><a href="#hub">Hub →</a></div>
      <div id="daily-connectors-body"><div class="dim">Loading…</div></div></div></div>`;
  // NOTICES — background job/reminder/digest notices; hidden when there are none (calm).
  html += `<div class="surface rise is-hidden" id="daily-notices">
      <div class="panel-title"><h3>Notices</h3></div>
      <div id="daily-notices-body" class="daily-rows"></div></div>`;
  // WORKFLOWS — prepared prompts (through /api/turn) + navigation shortcuts.
  html += `<div class="surface rise"><div class="panel-title"><h3>Workflows</h3></div>
      <div class="chip-row" id="daily-workflows"></div></div>`;
  // WHAT CHANGED — repo state + eval freshness + KB review. Dev noise: DEBUG-ONLY (an urgent
  // eval-stale chip still surfaces inside, but the card itself hides in the calm Daily view).
  html += `<div class="surface rise debug-only" id="daily-changed">
      <div class="panel-title"><h3>What changed</h3></div>
      <div id="daily-changed-body" class="dim">Loading…</div></div>`;
  zones.innerHTML = html;

  container.querySelector("#daily-digest-run").addEventListener("click", async (e) => {
    const btn = e.currentTarget;
    btn.disabled = true; btn.textContent = "Running…";
    const res = await api.post("/api/digest/run");
    btn.disabled = false; btn.textContent = "Run digest now";
    if (res.ok) fillDaily(container, api);  // refresh the Briefing (no reload)
  });
  renderWorkflows(container, api);
  fillNotices(container, api);     // client-side (state.notices) — instant
  fillWorkspace(container, api);   // client-side (state.runner.project) — instant
  scheduleFills(container, api);   // coalesce the read-only GETs (see below)
}

// Active-workspace card — makes the project's Workspace (incl. the Graph) reachable from Daily.
// Client-side from the settled runner state (no fetch); navigate-only.
function fillWorkspace(container, api) {
  const body = container.querySelector("#daily-workspace-body");
  if (!body) return;
  body.textContent = "";
  const proj = api.state.runner && api.state.runner.project;
  if (!proj || !proj.id) {
    body.appendChild(emptyState("No active project",
      "Pick a project to open its workspace, or keep chatting globally."));
    const a = document.createElement("a");
    a.href = "#projects"; a.className = "review-link"; a.textContent = "Choose a project →";
    body.appendChild(a);
    return;
  }
  const name = document.createElement("div");
  name.className = "lr-t"; name.style.marginBottom = "8px";
  name.textContent = proj.name || `Project ${proj.id}`;
  const row = document.createElement("div");
  row.className = "chip-row";
  for (const [label, suffix] of [["Overview", ""], ["Graph", "/graph"],
    ["Artifacts", "/artifacts"], ["Memory", "/memory"], ["Chats", "/chats"]]) {
    const a = document.createElement("a");
    a.href = `#workspace/${proj.id}${suffix}`;
    a.className = "chip-btn";
    a.textContent = label;
    row.appendChild(a);
  }
  body.append(name, row);
}

// renderZones re-runs on every WS event, including each streaming text_delta. Coalesce the
// read-only data fetches (/api/daily, /api/tasks) so a streaming turn can't fire two GETs per
// token — the chat itself still updates immediately (renderChat in render(), above).
let _fillTimer = null;
function scheduleFills(container, api) {
  if (_fillTimer) clearTimeout(_fillTimer);
  _fillTimer = setTimeout(() => {
    _fillTimer = null;
    fillToday(container, api);
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
      const input = container.querySelector("#composer-input");
      input.value = wf.prompt;
      submitConversationTurn(api, input, () => renderChat(container, api));
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
  if (!notices.length) { card.classList.add("is-hidden"); return; }
  card.classList.remove("is-hidden");
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

// Render the SHARED capability truth (data.capabilities.connectors) — the exact rows Hub and
// Settings show, so the three surfaces can never disagree (Habib's "Daily says none, Settings says
// Google" bug). A connected-but-not-exposed-to-chat connector reads honestly (amber-ish + a "why"),
// never a false green. Presence/state/reason only — no key/token value is ever inspected.
function capTone(r) {
  if (r.state === "needs_reconnect") return "warn";
  if (r.state === "connected" && r.exposed_to_chat) return "good";
  return "";
}

function capPill(r) {
  const tone = capTone(r);
  const pill = document.createElement("span");
  pill.className = "status-pill " + (tone || "");
  if (r.reason) pill.title = r.reason; // the plain-language "why" on hover (safe: an attribute)
  const dot = document.createElement("span");
  dot.className = "dot" + (tone === "good" ? "" : " off");
  const label = document.createElement("span");
  const suffix = r.state === "connected" && !r.exposed_to_chat ? " · not in chat" : "";
  label.textContent = r.name + suffix;
  pill.append(dot, label);
  return pill;
}

function fillConnectors(container, data) {
  const body = container.querySelector("#daily-connectors-body");
  if (!body) return;
  body.textContent = "";
  const rows = (data.capabilities && data.capabilities.connectors) || [];
  // Show what's actually set up (connected / needs-reconnect); a fresh machine reaches the empty
  // state — matching Settings, which reports the very same rows as not_configured.
  const active = rows.filter((r) => r.state !== "not_configured");
  if (!active.length) {
    body.appendChild(emptyState("No connectors configured",
      "Connect accounts in the Hub to enrich your briefing."));
    return;
  }
  const strip = document.createElement("div");
  strip.className = "conn-strip";
  for (const r of active) strip.appendChild(capPill(r));
  body.appendChild(strip);
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
  renderConversation(chat, api.state, {
    emptyHeading: "No messages yet",
    emptyHint: "Ask Kairo anything, or pick a workflow below to get started.",
  });
}

async function fillToday(container, api) {
  const rows = await api.get("/api/tasks");
  const card = container.querySelector("#daily-today");
  if (!card) return;
  const active = Array.isArray(rows) ? rows.filter((t) => t.status === "active").slice(0, 4) : [];
  if (!active.length) { card.classList.add("is-hidden"); return; }  // nothing today ⇒ hide (calm)
  card.classList.remove("is-hidden");
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

// (Recent chats moved into the conversation header's Resume menu in Phase 15.5 — the Daily
// dashboard no longer carries a separate chats card.)

function shortTime(iso) {
  if (!iso) return "—";
  const m = /T(\d{2}:\d{2})/.exec(iso);
  return m ? m[1] : iso.slice(0, 10);
}
