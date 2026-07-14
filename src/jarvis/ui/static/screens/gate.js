// Notifications — the ONE attention surface (Phase 16). It renders the unified queue
// (GET /api/attention): live Gate ASKs, write-intents awaiting approval, pending graph
// suggestions, and dreaming proposals / system alerts — each acted on through its source's
// EXISTING gated route (the center adds no authority). Below it: recent writes (undo) + the
// audit trail + a read-only policy snapshot (Debug). No raw-HTML sink — user/model text is set
// via textContent only.
import { openProjectReport } from "../ui/project-report.js";

export async function render(container, api) {
  container.innerHTML = `
    <div class="rise">
      <h1>Notifications</h1>
      <div class="sub">Everything waiting on you, in one place. Amber means Kairo is blocked on you. “Approve & send” authorizes a write; clearing a proposal only removes it from this list. Configured external nudges are count-only, respect quiet hours and project mutes, and never authorize an action.</div>
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
      <div class="card-label">External write audit · metadata only</div>
      <div id="gate-connector-writes" class="dim">loading…</div>
    </div>
    <div class="card rise">
      <div class="card-label">Background activity · this session</div>
      <div id="gate-notices" class="dim">loading…</div>
    </div>
    <div class="card rise">
      <div class="card-label">Delegation history · this scope</div>
      <div id="gate-agents" class="dim">loading…</div>
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
    const actions = [];
    if (
      it.category === "project_intelligence"
      && Number.isInteger(it.detail?.report_id)
      && it.detail.report_id > 0
    ) {
      actions.push(actionBtn("View report", "btn", () => {
        void openProjectReport(api, it.detail.report_id);
      }));
    }
    actions.push(
      actionBtn("Clear from list", "btn", () => act(`/api/attention/${it.ref}/resolve`, { action: "done" })),
      actionBtn("Dismiss", "btn", () => act(`/api/attention/${it.ref}/resolve`, { action: "dismiss" })),
    );
    return actions;
  }

  async function act(path, body) {
    await api.post(path, body || {});
    await fillQueue();
    await fillWrites();
    await fillConnectorWrites();
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
          await fillConnectorWrites();
        }));
      }
      writes.appendChild(row);
    }
  }

  // The journal proves that an outward connector write happened without surfacing its content,
  // remote identifiers, rollback handles, egress references, or trace ids.
  const connectorWrites = container.querySelector("#gate-connector-writes");
  async function fillConnectorWrites() {
    const data = await api.get("/api/connector-writes");
    connectorWrites.className = "";
    connectorWrites.textContent = "";
    if (!data) {
      connectorWrites.className = "dim";
      connectorWrites.textContent = "External write audit is unavailable.";
      return;
    }
    const rows = Array.isArray(data.writes) ? data.writes : [];
    if (!rows.length) {
      connectorWrites.className = "dim";
      connectorWrites.textContent = "No connector writes in this scope.";
      return;
    }
    for (const write of rows) {
      const row = document.createElement("div");
      row.className = "toolline";
      row.textContent = [write.provider, write.verb, write.status, write.at]
        .filter((value) => typeof value === "string" && value).join(" · ");
      connectorWrites.appendChild(row);
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
  await fillConnectorWrites();

  // The server keeps a bounded current-session activity buffer, unlike the in-memory WebSocket
  // toast list. It is not durable history and never creates an approval or an action.
  const noticeHistory = container.querySelector("#gate-notices");
  async function fillNoticeHistory() {
    const data = await api.get("/api/notices");
    noticeHistory.className = "";
    noticeHistory.textContent = "";
    if (!data) {
      noticeHistory.className = "dim";
      noticeHistory.textContent = "Background activity is unavailable.";
      return;
    }
    const byKey = new Map();
    for (const [source, rows] of [["durable", data.notices], ["live", api.state?.notices]]) {
      if (!Array.isArray(rows)) continue;
      rows.forEach((notice, index) => {
        if (!notice || typeof notice !== "object") return;
        // Sequence plus timestamp distinguishes a restarted NoticeBoard from a duplicate live row.
        const key = notice.seq != null ? `seq:${String(notice.seq)}:${String(notice.at || "")}` : `${source}:${index}`;
        byKey.set(key, notice);
      });
    }
    const noticeTime = (notice) => {
      const timestamp = Date.parse(typeof notice.at === "string" ? notice.at : "");
      return Number.isFinite(timestamp) ? timestamp : 0;
    };
    const notices = [...byKey.values()].sort((a, b) => {
      const byTime = noticeTime(b) - noticeTime(a);
      if (byTime) return byTime;
      const aSeq = Number(a.seq);
      const bSeq = Number(b.seq);
      return Number.isFinite(aSeq) && Number.isFinite(bSeq) ? bSeq - aSeq : 0;
    });
    if (!notices.length) {
      noticeHistory.className = "dim";
      noticeHistory.textContent = "No background activity in this session.";
      return;
    }
    for (const notice of notices.slice(0, 50)) {
      const row = document.createElement("div");
      row.className = "toolline";
      const text = [notice.text, notice.summary, notice.message]
        .find((value) => typeof value === "string" && value);
      row.textContent = [notice.kind, text, notice.at]
        .filter((value) => typeof value === "string" && value).join(" · ");
      noticeHistory.appendChild(row);
    }
  }
  await fillNoticeHistory();

  // Delegated-run history is a server-scoped, metadata-only audit. The browser supplies no
  // project selector; it receives only the current live workspace's rows (or the global
  // administrative aggregate). Prompts, results, errors, and trace IDs never cross this seam.
  const agentHistory = container.querySelector("#gate-agents");
  async function fillAgentHistory() {
    const data = await api.get("/api/agents");
    agentHistory.className = "";
    agentHistory.textContent = "";
    if (!Array.isArray(data)) {
      agentHistory.className = "dim";
      agentHistory.textContent = "Delegation history is unavailable.";
      return;
    }
    if (!data.length) {
      agentHistory.className = "dim";
      agentHistory.textContent = "No delegated runs in this scope.";
      return;
    }
    for (const run of data) {
      const row = document.createElement("div");
      row.className = "toolline";
      const tools = Array.isArray(run.tools_scope) ? run.tools_scope.join(", ") : "";
      const cost = typeof run.cost_usd === "number" ? `$${run.cost_usd.toFixed(4)}` : "unpriced";
      row.textContent = [
        run.title,
        run.status,
        tools ? `tools: ${tools}` : "no tools recorded",
        `${Number(run.iterations) || 0} iterations`,
        `${Number(run.denied_count) || 0} denied`,
        cost,
        run.started_at,
      ].filter((value) => typeof value === "string" && value).join(" · ");
      agentHistory.appendChild(row);
    }
  }
  await fillAgentHistory();

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
