// Gate — approvals + today's decisions. The clearest priority surface (amber = needs you).
// Pending approvals confirm in the amber modal (auto-raised); this screen also shows the
// audit trail and, in Debug, the read-only policy snapshot.

export async function render(container, api) {
  container.innerHTML = `
    <div class="rise">
      <h1>Gate</h1>
      <div class="sub">Approvals, and today's decisions. Amber means Kairo is waiting on you.</div>
    </div>
    <div class="card rise" id="gate-pending-card">
      <div class="card-label amber">Pending</div>
      <div id="gate-pending"></div>
    </div>
    <div class="card rise" id="gate-writes-card">
      <div class="card-label amber">Pending writes · review &amp; approve to send</div>
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
  const items = [...api.state.pending.values()];
  if (!items.length) {
    pend.innerHTML = `<div class="dim">Nothing waiting. You're clear.</div>`;
  } else {
    pend.innerHTML = "";
    for (const p of items) {
      const row = document.createElement("div");
      row.className = "zone-now";
      row.innerHTML = `<span class="runner-dot" style="background:var(--amber)"></span>
        <div class="body">
          <div class="lead"><span class="mono">${esc(p.tool)}</span>${p.title ? " — " + esc(p.title) : ""}</div>
          <div class="desc">${esc(p.reason || "")}</div>
        </div>`;
      const btn = document.createElement("button");
      btn.className = "btn btn-amber";
      btn.textContent = "Review";
      btn.addEventListener("click", () => api.reviewPending());
      row.appendChild(btn);
      pend.appendChild(row);
    }
  }

  // Pending writes: outward connector-write proposals awaiting approval, plus recent writes with
  // undo. Approve here EXECUTES the stored write (the only path that does); reject/undo close it.
  const writes = container.querySelector("#gate-writes");
  async function fillWrites() {
    const q = await api.get("/api/intents");
    writes.className = "";
    writes.innerHTML = "";
    const pending = (q && q.pending) || [];
    const recent = (q && q.recent) || [];
    if (!pending.length && !recent.length) {
      writes.className = "dim";
      writes.textContent = "No outward writes proposed.";
      return;
    }
    for (const it of pending) writes.appendChild(writeRow(it, true));
    if (recent.length) {
      const h = document.createElement("div");
      h.className = "card-label";
      h.style.marginTop = "12px";
      h.textContent = "Recent writes";
      writes.appendChild(h);
      for (const it of recent) writes.appendChild(writeRow(it, false));
    }
  }
  function writeRow(it, pendingRow) {
    const row = document.createElement("div");
    row.className = "zone-now";
    const body = document.createElement("div");
    body.className = "body";
    const lead = document.createElement("div");
    lead.className = "lead";
    lead.textContent = (it.preview && it.preview.title) || it.summary || it.kind;
    body.appendChild(lead);
    if (it.preview) body.appendChild(renderPreview(it.preview));
    if (!pendingRow) {
      const st = document.createElement("div");
      st.className = "desc";
      st.textContent = "state: " + it.state + (it.error ? " — " + it.error : "");
      body.appendChild(st);
    }
    row.appendChild(body);
    if (pendingRow) {
      row.appendChild(actionBtn("Approve & send", "btn-amber", async () => {
        await api.post(`/api/intents/${it.id}/approve`, {});
        await fillWrites();
      }));
      row.appendChild(actionBtn("Reject", "btn", async () => {
        await api.post(`/api/intents/${it.id}/reject`, {});
        await fillWrites();
      }));
    } else if (it.state === "executed") {
      row.appendChild(actionBtn("Undo", "btn", async () => {
        await api.post(`/api/intents/${it.id}/undo`, {});
        await fillWrites();
      }));
    }
    return row;
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

function esc(s) { const d = document.createElement("div"); d.textContent = s ?? ""; return d.innerHTML; }
