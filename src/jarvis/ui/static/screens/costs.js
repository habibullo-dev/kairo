// Cost Center (Phase 11 T13) — spend over the model-call + service ledgers. Periods (today/week/
// month) × dimensions (project/model/provider/team/role/stage/purpose/service), a budget-cap
// warning banner, and the ROI (time-saved value − actual cost) aggregate + per-run list. Unpriced
// calls are ALWAYS distinct, never summed as $0. Read-only; metadata only (no key/prompt content).
// Cost = teal monitoring; a hard cap breach is danger (red). (The approval-reserved hue is used
// nowhere here.)
import { esc } from "../ui/dom.js";
import { money } from "../ui/format.js";

function metric(p, label) {
  return `<div class="metric"><div class="n">${money(p ? p.cost_usd : 0)}</div>
    <div class="l">${esc(label)}</div>
    <div class="dim" style="font-size:11px;margin-top:2px">${p ? p.calls : 0} calls${
      p && p.unpriced ? ` · ${p.unpriced} unpriced` : ""}</div></div>`;
}

function dimRows(arr, key) {
  return (arr || [])
    .filter((r) => r[key] != null)
    .map((r) => `<tr><td>${esc(String(r[key]))}</td>
      <td style="text-align:right">${money(r.cost_usd)}</td>
      <td style="text-align:right" class="dim">${r.calls ?? "—"}${r.unpriced ? ` · ${r.unpriced}?` : ""}</td></tr>`)
    .join("");
}

// A collapsible dimension breakdown (secondary dims lazy-expand; the data is already loaded).
function dimSection(arr, key, label, { open = false } = {}) {
  const body = dimRows(arr, key);
  return `<details class="surface rise dim-section"${open ? " open" : ""}>
    <summary>${esc(label)}</summary>
    <table class="dim-table">${body || '<tr><td class="dim">No spend.</td></tr>'}</table></details>`;
}

function budgetBanner(w, confirmAbove) {
  if (!w) return "";
  const confirm = confirmAbove != null ? ` · confirm above ${money(confirmAbove)}` : "";
  if (!w.cap_usd) {
    return `<div class="budget-banner ok rise">No monthly budget cap set${confirm}.</div>`;
  }
  const pct = w.cap_usd ? Math.round((w.month_spend_usd / w.cap_usd) * 100) : 0;
  const tone = w.level === "hard" ? "danger" : w.level === "soft" ? "cost" : "ok";
  const msg = w.level === "hard"
    ? `Monthly budget exceeded — ${money(w.month_spend_usd)} of ${money(w.cap_usd)} (${pct}%)`
    : w.level === "soft"
      ? `Approaching monthly budget — ${money(w.month_spend_usd)} of ${money(w.cap_usd)} (${pct}%)`
      : `Budget — ${money(w.month_spend_usd)} of ${money(w.cap_usd)} this month (${pct}%)`;
  return `<div class="budget-banner ${tone} rise">${esc(msg)}${confirm}</div>`;
}

function roiBlock(roi, hourly) {
  const runs = (roi && roi.roi) || [];
  const priced = runs.filter((r) => r.net_usd != null);
  const sum = (f) => priced.reduce((a, r) => a + (r[f] || 0), 0);
  const agg = priced.length
    ? `<div class="cost-row" style="margin-bottom:10px">
        <div class="metric"><div class="n">${money(sum("value_usd"))}</div><div class="l">time-saved value</div></div>
        <div class="metric"><div class="n">${money(sum("actual_cost_usd"))}</div><div class="l">actual cost</div></div>
        <div class="metric"><div class="n">${money(sum("net_usd"))}</div><div class="l">net (${priced.length} runs)</div></div></div>`
    : `<div class="dim">No priced orchestration runs yet.</div>`;
  const rows = runs.map((r) => `<tr><td class="dim">${esc(String(r.workflow || "?"))}</td>
    <td class="dim">${esc(String(r.team || "—"))}</td>
    <td style="text-align:right">${money(r.value_usd)}</td>
    <td style="text-align:right" class="dim">${money(r.actual_cost_usd)}</td>
    <td style="text-align:right">${r.net_usd == null ? "—" : money(r.net_usd)}</td></tr>`).join("");
  return `<div class="surface rise"><div class="panel-title"><h3>ROI · time saved</h3>
      <span class="dim">rate ${money(hourly)}/hr</span></div>
    ${agg}
    ${rows ? `<details class="dim-section"><summary>Per-run</summary>
      <table class="dim-table"><thead><tr><th>workflow</th><th>team</th><th style="text-align:right">value</th>
      <th style="text-align:right">cost</th><th style="text-align:right">net</th></tr></thead>
      <tbody>${rows}</tbody></table></details>` : ""}</div>`;
}

export async function render(container, api) {
  container.textContent = "";
  const [c, roi] = await Promise.all([api.get("/api/costs"), api.get("/api/roi")]);
  if (!c) {
    container.innerHTML = `<div class="rise"><h1>Costs</h1>
      <div class="sub">Cost tracking is not enabled.</div></div>`;
    return;
  }
  const lim = c.limits || {};
  const primaryDims = c.by_project
    ? dimSection(c.by_project, "project", "By project", { open: true })
    : dimSection(c.by_model, "model", "By model", { open: true });

  container.innerHTML = `
    <div class="rise"><h1>Cost Center</h1>
      <div class="sub">LLM + service spend from the ledger (metadata only). Unpriced calls are
        counted separately, never as $0.</div></div>
    ${budgetBanner(c.budget_warning, lim.confirm_above_usd)}
    <div class="cost-row rise">${metric(c.today, "Today")}${metric(c.week, "This week")}${metric(c.month, "This month")}</div>
    ${roiBlock(roi, c.hourly_rate_usd)}
    ${primaryDims}
    ${c.by_project ? dimSection(c.by_model, "model", "By model") : ""}
    ${dimSection(c.by_provider, "provider", "By provider")}
    ${dimSection(c.by_team, "team", "By team")}
    ${dimSection(c.by_role, "agent_role", "By role")}
    ${dimSection(c.by_stage, "stage", "By stage")}
    ${dimSection(c.by_purpose, "purpose", "By purpose")}
    ${dimSection(c.by_service, "service", "By service (local tools)")}
    <div class="surface rise"><div class="panel-title"><h3>Limits</h3></div>
      <div class="mono dim">soft/run ${money(lim.soft_warn_usd_per_run)} · hard/run ${money(lim.hard_stop_usd_per_run)}
        · confirm above ${money(lim.confirm_above_usd)}
        · project/month ${lim.project_monthly_usd == null ? "—" : money(lim.project_monthly_usd)}</div></div>`;
}
