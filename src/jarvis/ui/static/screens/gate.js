// Gate — approvals + today's decisions. The clearest priority surface (amber = needs you).
// Pending approvals are confirmed in the modal (auto-raised); this screen also shows the
// audit trail and, in Debug, the read-only policy snapshot.

export async function render(container, api) {
  container.innerHTML = `
    <h1>Gate</h1>
    <div class="sub">Approvals, and today's decisions. Amber means Kairo is waiting on you.</div>
    <div class="card"><div class="label" style="color:var(--amber)">Pending</div><div id="gate-pending"></div></div>
    <div class="card"><div class="label">Earlier today · audit</div><div id="gate-audit" class="dim">loading…</div></div>
    <div class="card debug-only"><div class="label">Policy snapshot (read-only)</div><pre id="gate-policy" class="mono"></pre></div>`;

  const pend = container.querySelector("#gate-pending");
  const items = [...api.state.pending.values()];
  if (!items.length) {
    pend.innerHTML = `<div class="dim">Nothing waiting. You're clear.</div>`;
  } else {
    pend.innerHTML = "";
    for (const p of items) {
      const row = document.createElement("div");
      row.className = "toolline";
      row.textContent = `${p.tool}${p.title ? ` — ${p.title}` : ""} · ${p.reason || ""}`;
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
      const bits = [e.event, e.tool, e.permission].filter(Boolean).join(" · ");
      d.textContent = bits;
      at.appendChild(d);
    }
  } else {
    at.textContent = "No decisions logged yet today.";
  }

  const pol = await api.get("/api/gate/policy");
  const pre = container.querySelector("#gate-policy");
  if (pre) pre.textContent = pol ? JSON.stringify(pol.policy, null, 2) : "";
}
