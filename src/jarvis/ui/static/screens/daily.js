// Daily Mode — the calm default. Zones in priority order (one primary attention surface):
// pending approval (amber) → current activity → Briefing (digest) → Today → What changed →
// Workflows → Conversation → sticky composer. Everything from the digest/repo/email is
// UNTRUSTED content, so it is rendered with textContent only — never innerHTML, never
// linkified (a digest link would be a phishing/exfil surface). Detail lives in Trace/Debug.

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

function renderZones(container, api) {
  const s = api.state;
  const zones = container.querySelector("#daily-zones");
  const busy = s.runner && s.runner.turn_busy;
  const pend = [...s.pending.values()];

  let html = "";
  // 1) PENDING APPROVAL — the one primary attention surface (amber), when present.
  if (pend.length) {
    const p = pend[0];
    html += `<div class="zone-pending rise"><div class="ico">⚠</div>
      <div class="body">
        <div class="card-label amber" style="margin-bottom:3px">Waiting on you</div>
        <div class="lead">Kairo wants to <b>${esc(p.tool)}</b>${p.title ? " — " + esc(p.title) : ""}</div>
      </div>
      <button class="btn btn-amber" id="daily-review">Review</button></div>`;
  }
  // 2) NOW — current activity. Stable IDs so app.js renderRunnerState() writes this card from
  // the SAME settled state.runner as the status bar (never diverge after a turn ends).
  html += `<div class="card rise"><div class="zone-now">
      <span class="runner-dot${busy ? " busy" : ""}" id="daily-now-dot"></span>
      <div class="body">
        <div class="lead${busy ? "" : " idle"}" id="daily-now-lead">${busy ? "Kairo is working" : "Kairo is idle"}</div>
        <div class="desc" id="daily-now-desc">${busy ? "Working on your request." : "Nothing running. Send a message to begin."}</div>
      </div></div></div>`;
  // 3) BRIEFING — the latest digest (filled from /api/daily). Quiet card, no toast.
  html += `<div class="card rise" id="daily-briefing">
      <div class="card-head"><div class="t">Briefing</div>
        <button class="rowbtn" id="daily-digest-run">Run digest now</button></div>
      <div id="daily-briefing-body" class="dim">Loading…</div></div>`;
  // 4) TODAY — populated from /api/tasks if the scheduler is on (hidden otherwise).
  html += `<div class="card rise" id="daily-today" style="display:none">
      <div class="card-head"><div class="t">Today</div><a href="#tasks">All tasks →</a></div>
      <div id="daily-today-rows"></div></div>`;
  // 5) WHAT CHANGED — repo state + eval freshness + KB review (filled from /api/daily).
  html += `<div class="card rise" id="daily-changed">
      <div class="card-head"><div class="t">What changed</div></div>
      <div id="daily-changed-body" class="dim">Loading…</div></div>`;
  // 6) WORKFLOWS — prepared prompts (through /api/turn) + navigation shortcuts.
  html += `<div class="card rise"><div class="card-head"><div class="t">Workflows</div></div>
      <div class="chip-row" id="daily-workflows"></div></div>`;
  // 7) CONVERSATION
  html += `<div class="rise"><div class="card-head" style="margin-bottom:14px"><div class="t">Conversation</div></div>
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
  fillToday(container, api);
  fillDaily(container, api);
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
  if (!data) return;
  fillBriefing(container, data);
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
    const none = document.createElement("div");
    none.className = "dim";
    none.textContent = "No digest yet. Run one to see your morning briefing.";
    body.appendChild(none);
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
    chat.innerHTML = `<div class="dim" style="font-size:13px">No messages yet.</div>`;
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

function shortTime(iso) {
  if (!iso) return "—";
  const m = /T(\d{2}:\d{2})/.exec(iso);
  return m ? m[1] : iso.slice(0, 10);
}

function esc(s) { const d = document.createElement("div"); d.textContent = s ?? ""; return d.innerHTML; }

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
