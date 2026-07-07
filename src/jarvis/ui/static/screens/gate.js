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
