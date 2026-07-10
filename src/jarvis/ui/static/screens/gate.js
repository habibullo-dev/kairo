// Notifications — the ONE attention surface (Phase 16). It renders the unified queue
// (GET /api/attention): live Gate ASKs, write-intents awaiting approval, pending graph
// suggestions, and dreaming proposals / system alerts — each acted on through its source's
// EXISTING gated route (the center adds no authority). Below it: recent writes (undo) + the
// audit trail + a read-only policy snapshot (Debug). No raw-HTML sink — user/model text is set
// via textContent only.

export async function render(container, api) {
  container.innerHTML = `
    <div class="rise">
      <h1>Notifications</h1>
      <div class="sub">Everything waiting on you, in one place. Amber means Kairo is blocked on you.</div>
    </div>
    <div class="card rise" id="gate-pending-card">
      <div class="card-label amber">Needs you</div>
      <div id="gate-pending" class="dim">loading…</div>
    </div>
    <div class="card rise">
      <div class="card-label">Recent writes · undo</div>
      <div id="gate-writes" class="dim">loading…</div>
    </div>
    <div class="card rise">
      <div class="card-label">Earlier today · audit</div>
      <div id="gate-audit" class="dim">loading…</div>
    </div>
    <div class="card rise debug-only">
      <div class="card-label">Policy snapshot · read-only</div>
      <pre class="block" id="gate-policy"></pre>
    </div>`;

  const pend = container.querySelector("#gate-pending");

  // The unified attention queue. Each item's action dispatches to its SOURCE's own route; only
  // 'attention' rows (proposals/alerts/reviews) use the new metadata resolve route.
  async function fillQueue() {
    const q = await api.get("/api/attention");
    pend.className = "";
    pend.innerHTML = "";
    const items = (q && q.items) || [];
    if (!q) {
      pend.className = "dim";
      pend.textContent = "Attention queue unavailable.";
      return;
    }
    if (!items.length) {
      pend.className = "dim";
      pend.textContent = "Nothing waiting. You're clear.";
      return;
    }
    for (const it of items) pend.appendChild(queueRow(it));
  }

  function queueRow(it) {
    const row = document.createElement("div");
    row.className = "zone-now";
    const dot = document.createElement("span");
    dot.className = "runner-dot";
    if (it.priority === "urgent") dot.style.background = "var(--amber)";
    row.appendChild(dot);

    const body = document.createElement("div");
    body.className = "body";
    const lead = document.createElement("div");
    lead.className = "lead";
    lead.textContent = it.title || it.kind;
    body.appendChild(lead);
    const meta = document.createElement("div");
    meta.className = "desc";
    meta.textContent = [labelFor(it.source), it.kind, it.priority].filter(Boolean).join(" · ");
    body.appendChild(meta);
    // Untrusted (dreaming/agent-generated) content is badged so it never reads as fact.
    if (it.trust_class && it.trust_class !== "trusted_local" && it.trust_class !== "reviewed") {
      const t = document.createElement("div");
      t.className = "desc";
      t.textContent = "⚠ proposal — untrusted, review before acting";
      body.appendChild(t);
    }
    if (it.detail && it.detail.preview) body.appendChild(renderPreview(it.detail.preview));
    row.appendChild(body);

    for (const a of actionsFor(it)) row.appendChild(a);
    return row;
  }

  // Source → its label + the existing routes its actions hit (never a new authority path).
  function labelFor(source) {
    return {
      gate: "Tool approval", intent: "Outward write", graph_suggestion: "Memory suggestion",
      attention: "Proposal", system: "Alert",
    }[source] || source;
  }

  function actionsFor(it) {
    if (it.source === "gate") {
      return [actionBtn("Review", "btn-amber", () => api.reviewPending())];
    }
    if (it.source === "intent") {
      return [
        actionBtn("Approve & send", "btn-amber", () => act(`/api/intents/${it.ref}/approve`)),
        actionBtn("Reject", "btn", () => act(`/api/intents/${it.ref}/reject`)),
      ];
    }
    if (it.source === "graph_suggestion") {
      return [
        actionBtn("Approve", "btn-amber", () => act(`/api/graph/suggestions/${it.ref}/approve`)),
        actionBtn("Reject", "btn", () => act(`/api/graph/suggestions/${it.ref}/reject`)),
      ];
    }
    // attention rows (proposals / alerts / reviews): metadata-only resolve. A proposal's real
    // acceptance is the human acting on its source elsewhere — never a hidden action here.
    return [
      actionBtn("Done", "btn", () => act(`/api/attention/${it.ref}/resolve`, { action: "done" })),
      actionBtn("Dismiss", "btn", () => act(`/api/attention/${it.ref}/resolve`, { action: "dismiss" })),
    ];
  }

  async function act(path, body) {
    await api.post(path, body || {});
    await fillQueue();
  }

  // Recent writes with undo (history, not a competing pending surface).
  const writes = container.querySelector("#gate-writes");
  async function fillWrites() {
    const q = await api.get("/api/intents");
    writes.className = "";
    writes.innerHTML = "";
    const recent = (q && q.recent) || [];
    if (!recent.length) {
      writes.className = "dim";
      writes.textContent = "No recent writes.";
      return;
    }
    for (const it of recent) {
      const row = document.createElement("div");
      row.className = "zone-now";
      const body = document.createElement("div");
      body.className = "body";
      const lead = document.createElement("div");
      lead.className = "lead";
      lead.textContent = (it.preview && it.preview.title) || it.summary || it.kind;
      body.appendChild(lead);
      const st = document.createElement("div");
      st.className = "desc";
      st.textContent = "state: " + it.state + (it.error ? " — " + it.error : "");
      body.appendChild(st);
      row.appendChild(body);
      if (it.state === "executed") {
        row.appendChild(actionBtn("Undo", "btn", async () => {
          await api.post(`/api/intents/${it.id}/undo`, {});
          await fillWrites();
        }));
      }
      writes.appendChild(row);
    }
  }

  function actionBtn(label, cls, onClick) {
    const b = document.createElement("button");
    b.className = "btn " + cls;
    b.textContent = label;
    b.addEventListener("click", onClick);
    return b;
  }
  function renderPreview(p) {
    const box = document.createElement("div");
    box.className = "desc";
    for (const f of p.fields || []) box.appendChild(kv(f.label + ": ", f.value));
    for (const d of p.diff || []) box.appendChild(kv(d.field + ": ", d.old + " → " + d.new));
    for (const n of p.notes || []) box.appendChild(line(n));
    for (const w of p.warnings || []) box.appendChild(line("⚠ " + w));
    return box;
  }
  function kv(k, v) {
    const el = document.createElement("div");
    const b = document.createElement("strong");
    b.textContent = k;
    el.appendChild(b);
    el.appendChild(document.createTextNode(String(v ?? "")));
    return el;
  }
  function line(t) {
    const el = document.createElement("div");
    el.textContent = t;
    return el;
  }

  await fillQueue();
  await fillWrites();

  const audit = await api.get("/api/audit/today");
  const at = container.querySelector("#gate-audit");
  if (audit && audit.events && audit.events.length) {
    at.className = "";
    at.innerHTML = "";
    for (const e of audit.events.slice(-40).reverse()) {
      const d = document.createElement("div");
      d.className = "toolline";
      d.textContent = [e.event, e.tool, e.permission].filter(Boolean).join(" · ");
      at.appendChild(d);
    }
  } else {
    at.textContent = "No decisions logged yet today.";
  }

  const pol = await api.get("/api/gate/policy");
  const pre = container.querySelector("#gate-policy");
  if (pre) pre.textContent = pol ? JSON.stringify(pol.policy, null, 2) : "";
}
