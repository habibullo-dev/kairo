// Daily Mode — the calm default. Zones, in priority order: pending approval (the one amber
// attention surface) → current activity → Today → conversation → sticky composer. Tool
// activity is a quiet line; detail lives in Trace/Debug.

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
    const send = async () => {
      const text = input.value.trim();
      if (!text) return;
      input.value = "";
      api.state.chat.push({ role: "user", text });
      renderChat(container, api);
      const res = await api.post("/api/turn", { text });
      if (!res.ok) {
        api.state.chat.push({ role: "assistant", text: `— ${res.data.message || "busy"} —` });
        renderChat(container, api);
      }
    };
    container.querySelector("#composer-send").addEventListener("click", send);
    input.addEventListener("keydown", (e) => { if (e.key === "Enter") send(); });
  }
  renderZones(container, api);
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
  // 2) NOW — current activity (secondary to a pending approval).
  html += `<div class="card rise"><div class="zone-now">
      <span class="runner-dot${busy ? " busy" : ""}"></span>
      <div class="body">
        <div class="lead${busy ? "" : " idle"}">${busy ? "Kairo is working" : "Kairo is idle"}</div>
        <div class="desc">${busy ? "Working on your request." : "Nothing running. Send a message to begin."}</div>
      </div></div></div>`;
  // 3) TODAY — populated from /api/tasks if the scheduler is on (hidden otherwise).
  html += `<div class="card rise" id="daily-today" style="display:none">
      <div class="card-head"><div class="t">Today</div><a href="#tasks">All tasks →</a></div>
      <div id="daily-today-rows"></div></div>`;
  // 4) CONVERSATION
  html += `<div class="rise"><div class="card-head" style="margin-bottom:14px"><div class="t">Conversation</div></div>
      <div class="chat" id="daily-chat"></div></div>`;
  zones.innerHTML = html;

  const review = container.querySelector("#daily-review");
  if (review) review.addEventListener("click", () => api.reviewPending());
  renderChat(container, api);
  fillToday(container, api);
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
