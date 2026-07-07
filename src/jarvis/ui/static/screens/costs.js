// Costs — spend over the model-call ledger (Phase 10). Today/week/month totals, the
// configured limits, and the "why this cost" breakdown by purpose / role / model. Unpriced
// calls are shown separately (never summed as $0). Read-only; no key or prompt content.
export async function render(container, api) {
  const c = await api.get("/api/costs");
  if (!c) {
    container.innerHTML = `<div class="rise"><h1>Costs</h1>
      <div class="sub">Cost tracking is not enabled.</div></div>`;
    return;
  }
  const money = (n) => `$${(n || 0).toFixed(4)}`;
  const period = (p, label) =>
    `<div class="cost-card"><div class="card-label">${label}</div>
      <div class="cost-big">${money(p.cost_usd)}</div>
      <div class="dim">${p.calls} calls${p.unpriced ? ` · ${p.unpriced} unpriced` : ""}</div></div>`;
  const rows = (arr, key) =>
    (arr || [])
      .filter((r) => r[key] != null)
      .map(
        (r) =>
          `<tr><td>${esc(String(r[key]))}</td><td style="text-align:right">${money(r.cost_usd)}</td>
           <td style="text-align:right" class="dim">${r.calls}${r.unpriced ? ` · ${r.unpriced}?` : ""}</td></tr>`,
      )
      .join("");
  const lim = c.limits || {};
  container.innerHTML = `
    <div class="rise"><h1>Costs</h1>
      <div class="sub">LLM completion cost from the ledger (metadata only). Unpriced calls are
        counted separately, never as $0.</div></div>
    <div class="cost-row rise">${period(c.today, "Today")}${period(c.week, "This week")}${period(c.month, "This month")}</div>
    <div class="card rise"><div class="card-label">By purpose (this month)</div>
      <table>${rows(c.by_purpose, "purpose") || '<tr><td class="dim">No spend yet.</td></tr>'}</table></div>
    <div class="card rise"><div class="card-label">By model (this month)</div>
      <table>${rows(c.by_model, "model") || '<tr><td class="dim">—</td></tr>'}</table></div>
    <div class="card rise"><div class="card-label">By role (orchestration)</div>
      <table>${rows(c.by_role, "agent_role") || '<tr><td class="dim">No orchestration spend.</td></tr>'}</table></div>
    <div class="card rise"><div class="card-label">By team (orchestration)</div>
      <table>${rows(c.by_team, "team") || '<tr><td class="dim">No team spend.</td></tr>'}</table></div>
    <div class="card rise"><div class="card-label">By service (local tools)</div>
      <table>${rows(c.by_service, "service") || '<tr><td class="dim">No service calls.</td></tr>'}</table></div>
    <div class="card rise"><div class="card-label">Limits</div>
      <div class="mono dim">soft/run ${money(lim.soft_warn_usd_per_run)} · hard/run ${money(lim.hard_stop_usd_per_run)}
        · confirm above ${money(lim.confirm_above_usd)}
        · project/month ${lim.project_monthly_usd == null ? "—" : money(lim.project_monthly_usd)}</div>
      <div class="mono dim">ROI hourly rate ${money(c.hourly_rate_usd)}</div></div>`;
}

function esc(s) {
  const d = document.createElement("div");
  d.textContent = s ?? "";
  return d.innerHTML;
}
