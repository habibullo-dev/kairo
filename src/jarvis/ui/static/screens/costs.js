// Cost Center (Phase 11 T13) — spend over the model-call + service ledgers. Periods (today/week/
// month) × dimensions (project/model/provider/team/role/stage/purpose/service), a budget-cap
// warning banner, and outcome-gated ROI + terminal model-cost accounting. Unpriced calls are
// ALWAYS distinct, never summed as $0. Read-only; metadata only (no key/prompt content).
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
  const accounting = (roi && roi.outcome_accounting) || {};
  const pricedAccepted = runs.filter((r) => r.outcome === "review_accepted" && r.net_usd != null);
  const sum = (f) => pricedAccepted.reduce((a, r) => a + (r[f] || 0), 0);
  const agg = pricedAccepted.length
    ? `<div class="cost-row" style="margin-bottom:10px">
        <div class="metric"><div class="n">${money(sum("value_usd"))}</div><div class="l">time-saved value</div></div>
        <div class="metric"><div class="n">${money(sum("actual_cost_usd"))}</div><div class="l">actual cost</div></div>
        <div class="metric"><div class="n">${money(sum("net_usd"))}</div><div class="l">net (${pricedAccepted.length} accepted runs)</div></div></div>`
    : `<div class="dim">No priced review-accepted runs yet.</div>`;
  const unknown = accounting.unknown_actual_model_cost_runs || 0;
  const accountingBlock = `<div class="cost-row" style="margin-bottom:10px">
      <div class="metric"><div class="n">${accounting.completed_runs || 0}</div><div class="l">terminal runs</div></div>
      <div class="metric"><div class="n">${accounting.review_accepted_runs || 0}</div><div class="l">review accepted</div></div>
      <div class="metric"><div class="n">${money(accounting.known_actual_model_cost_usd)}</div><div class="l">known model cost</div></div>
      <div class="metric"><div class="n">${accounting.known_model_cost_per_review_accepted_run == null ? "—" : money(accounting.known_model_cost_per_review_accepted_run)}</div><div class="l">cost / accepted run</div></div></div>
    <div class="dim">Model-call cost only; service estimates excluded.${unknown ? ` ${unknown} terminal run${unknown === 1 ? "" : "s"} has unknown actual cost.` : ""}</div>`;
  const rows = runs.map((r) => `<tr><td class="dim">${esc(String(r.workflow || "?"))}</td>
    <td class="dim">${esc(String(r.team || "—"))}</td>
    <td class="dim">${esc(String(r.outcome || r.status || "—").replace(/_/g, " "))}</td>
    <td style="text-align:right">${r.value_usd == null ? "—" : money(r.value_usd)}</td>
    <td style="text-align:right" class="dim">${money(r.actual_cost_usd)}</td>
    <td style="text-align:right">${r.net_usd == null ? "—" : money(r.net_usd)}</td></tr>`).join("");
  return `<div class="surface rise"><div class="panel-title"><h3>ROI · review accepted</h3>
      <span class="dim">rate ${money(hourly)}/hr</span></div>
    <div class="dim" style="margin-bottom:10px">Only review-accepted runs receive time-saved value; other outcomes retain cost without claimed value.</div>
    ${agg}
    <details class="dim-section"><summary>Model-cost accounting</summary>${accountingBlock}</details>
    ${rows ? `<details class="dim-section"><summary>Per-run</summary>
      <table class="dim-table"><thead><tr><th>workflow</th><th>team</th><th>outcome</th><th style="text-align:right">value</th>
      <th style="text-align:right">cost</th><th style="text-align:right">net</th></tr></thead>
      <tbody>${rows}</tbody></table></details>` : ""}</div>`;
}

function estimateAccuracyCard(calibration) {
  if (!calibration) return "";
  const ratio = (value) => value == null ? "—" : `${Math.round(value * 100)}%`;
  const comparable = calibration.comparable_runs || 0;
  const unavailable = (calibration.unknown_actual_cost_runs || 0)
    + (calibration.missing_estimate_runs || 0)
    + (calibration.zero_or_invalid_estimate_runs || 0);
  const body = comparable
    ? `<div class="cost-row" style="margin-bottom:10px">
        <div class="metric"><div class="n">${comparable}</div><div class="l">comparable runs</div></div>
        <div class="metric"><div class="n">${ratio(calibration.actual_to_estimate_ratio)}</div><div class="l">actual / estimate</div></div>
        <div class="metric"><div class="n">${ratio(calibration.p50_actual_to_estimate_ratio)}</div><div class="l">p50 actual / estimate</div></div>
        <div class="metric"><div class="n">${ratio(calibration.p95_actual_to_estimate_ratio)}</div><div class="l">p95 actual / estimate</div></div></div>
      <div class="cost-row">
        <div class="metric"><div class="n">${money(calibration.estimated_cost_usd)}</div><div class="l">estimated model cost</div></div>
        <div class="metric"><div class="n">${money(calibration.actual_cost_usd)}</div><div class="l">actual model cost</div></div>
        <div class="metric"><div class="n">${money(calibration.delta_usd)}</div><div class="l">actual − estimate</div></div>
        <div class="metric"><div class="n">${calibration.underestimated_runs || 0} / ${calibration.overestimated_runs || 0}</div><div class="l">under / over-estimated</div></div></div>`
    : `<div class="dim">No terminal runs with both a positive estimate and known actual model cost yet.</div>`;
  const scope = calibration.terminal_runs || 0;
  return `<details class="surface rise dim-section"><summary>Estimate calibration</summary>
    <div class="dim" style="margin:10px 0">Read-only comparison of the most recent ${calibration.sample_limit || 0} runs. It never changes pricing, routing, or budget limits automatically.</div>
    ${body}
    <div class="dim" style="margin-top:10px">${scope} terminal run${scope === 1 ? "" : "s"} sampled; ${unavailable} excluded because the actual cost or a usable estimate was unavailable.</div>
  </details>`;
}

// Context reuse (S7): prompt/context caching savings. Aggregate token counts + estimated savings
// only — never prompt content. Empty until caching is enabled (all providers default off).
function contextReuseCard(cr) {
  if (!cr) return "";
  const t = cr.totals || {};
  const active = (t.hit_tokens || 0) + (t.cache_write_tokens || 0) + (t.cached_input_tokens || 0) > 0;
  const rows = (cr.by_provider || [])
    .filter((r) => r.hit_tokens || r.cache_write_tokens || r.cached_input_tokens)
    .map((r) => `<tr><td>${esc(String(r.provider))}</td>
      <td style="text-align:right">${(r.hit_tokens || 0).toLocaleString()}</td>
      <td style="text-align:right">${money(r.estimated_savings_usd)}</td>
      <td style="text-align:right" class="dim">${Math.round((r.hit_rate || 0) * 100)}%</td></tr>`)
    .join("");
  const body = active
    ? `<div class="cost-row" style="margin-bottom:10px">
        <div class="metric"><div class="n">${(t.hit_tokens || 0).toLocaleString()}</div><div class="l">cache-hit tokens</div></div>
        <div class="metric"><div class="n">${(t.cache_write_tokens || 0).toLocaleString()}</div><div class="l">cache writes</div></div>
        <div class="metric"><div class="n">${money(t.estimated_savings_usd)}</div><div class="l">est. savings</div></div>
        <div class="metric"><div class="n">${Math.round((t.hit_rate || 0) * 100)}%</div><div class="l">hit rate</div></div></div>
      <table class="dim-table"><tr><th style="text-align:left">Provider</th><th style="text-align:right">Hit tokens</th>
        <th style="text-align:right">Est. savings</th><th style="text-align:right">Hit rate</th></tr>${rows}</table>`
    : `<div class="dim">No prompt/context cache reuse recorded yet (caching is off until enabled).</div>`;
  return `<details class="surface rise dim-section"><summary>Context reuse · prompt caching</summary>${body}</details>`;
}

function modelRequestHealthCard(health) {
  if (!health) return "";
  const totals = health.totals || {};
  const recording = health.recording_degraded;
  const incomplete = recording && recording.telemetry_complete === false;
  const rate = totals.error_rate == null ? "—" : `${Math.round(totals.error_rate * 10000) / 100}%`;
  const latency = (value) => value == null ? "—" : `${Math.round(value)} ms`;
  const routes = (health.by_provider_model || []).map((row) => `<tr>
    <td>${esc(String(row.provider || "?"))}</td><td>${esc(String(row.model || "?"))}</td>
    <td style="text-align:right">${row.attempts || 0}</td><td style="text-align:right">${row.failed_requests || 0}</td>
    <td style="text-align:right">${row.error_rate == null ? "—" : `${Math.round(row.error_rate * 10000) / 100}%`}</td>
    <td style="text-align:right">${latency(row.p50_completed_latency_ms)}</td>
    <td style="text-align:right">${latency(row.p95_completed_latency_ms)}</td></tr>`).join("");
  const days = (health.by_day || []).map((row) => {
    const day = row.totals || {};
    return `<tr><td>${esc(String(row.day || "?"))}</td>
      <td style="text-align:right">${day.attempts || 0}</td>
      <td style="text-align:right">${day.failed_requests || 0}</td>
      <td style="text-align:right">${day.error_rate == null ? "—" : `${Math.round(day.error_rate * 10000) / 100}%`}</td>
      <td style="text-align:right">${latency(day.p50_completed_latency_ms)}</td>
      <td style="text-align:right">${latency(day.p95_completed_latency_ms)}</td></tr>`;
  }).join("");
  const dayRoutes = (health.by_day || []).flatMap((day) => (day.by_provider_model || []).map((row) => `<tr>
    <td>${esc(String(day.day || "?"))}</td><td>${esc(String(row.provider || "?"))}</td>
    <td>${esc(String(row.model || "?"))}</td><td style="text-align:right">${row.attempts || 0}</td>
    <td style="text-align:right">${row.failed_requests || 0}</td>
    <td style="text-align:right">${row.error_rate == null ? "—" : `${Math.round(row.error_rate * 10000) / 100}%`}</td>
    <td style="text-align:right">${latency(row.p50_completed_latency_ms)}</td>
    <td style="text-align:right">${latency(row.p95_completed_latency_ms)}</td></tr>`)).join("");
  const errors = (health.error_classes || []).map((row) =>
    `${esc(String(row.error_class || "ModelRequestError"))} (${row.failed_requests || 0})`
  ).join(", ");
  const recordingNote = recording == null
    ? "Failure telemetry recording status is unavailable."
    : incomplete
      ? `Model-request telemetry is incomplete: ${recording.lost_records || 0} record${recording.lost_records === 1 ? " was" : "s were"} lost since this process started, so exact counts, error rate, and latency percentiles are unavailable.`
      : recording.degraded
      ? `Failure telemetry recording is degraded; ${recording.unrecorded || 0} request record${recording.unrecorded === 1 ? "" : "s"} may be missing.`
      : "Failure telemetry is recording normally.";
  return `<details class="surface rise dim-section"><summary>Model request health</summary>
    <div class="dim" style="margin:10px 0">Completed model-request latency only; this is not end-to-end turn time. Failed requests have no cost or token estimate.</div>
    <div class="cost-row" style="margin-bottom:10px">
      <div class="metric"><div class="n">${totals.attempts || 0}</div><div class="l">${incomplete ? "recorded attempts" : "attempts"}</div></div>
      <div class="metric"><div class="n">${totals.completed_requests || 0}</div><div class="l">${incomplete ? "recorded completed" : "completed"}</div></div>
      <div class="metric"><div class="n">${totals.failed_requests || 0}</div><div class="l">${incomplete ? "recorded failed" : "failed"}</div></div>
      <div class="metric"><div class="n">${rate}</div><div class="l">error rate</div></div>
      <div class="metric"><div class="n">${latency(totals.p50_completed_latency_ms)}</div><div class="l">p50 latency</div></div>
      <div class="metric"><div class="n">${latency(totals.p95_completed_latency_ms)}</div><div class="l">p95 latency</div></div></div>
    <div class="dim">${totals.measured_completed_latency_requests || 0} measured completed request${totals.measured_completed_latency_requests === 1 ? "" : "s"}; ${totals.unmeasured_completed_latency_requests || 0} unmeasured. ${esc(recordingNote)}</div>
    ${errors ? `<div class="dim" style="margin-top:8px">Failure classes: ${errors}</div>` : ""}
    ${routes ? `<table class="dim-table" style="margin-top:10px"><thead><tr><th>provider</th><th>model</th><th style="text-align:right">attempts</th><th style="text-align:right">failed</th><th style="text-align:right">error rate</th><th style="text-align:right">p50</th><th style="text-align:right">p95</th></tr></thead><tbody>${routes}</tbody></table>` : ""}
    ${days ? `<details class="dim-section" style="margin-top:10px"><summary>Daily health (UTC)</summary>
      <table class="dim-table"><thead><tr><th>day</th><th style="text-align:right">attempts</th><th style="text-align:right">failed</th><th style="text-align:right">error rate</th><th style="text-align:right">p50</th><th style="text-align:right">p95</th></tr></thead><tbody>${days}</tbody></table>
      ${dayRoutes ? `<table class="dim-table" style="margin-top:10px"><thead><tr><th>day</th><th>provider</th><th>model</th><th style="text-align:right">attempts</th><th style="text-align:right">failed</th><th style="text-align:right">error rate</th><th style="text-align:right">p50</th><th style="text-align:right">p95</th></tr></thead><tbody>${dayRoutes}</tbody></table>` : ""}
    </details>` : ""}
  </details>`;
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
        counted separately, never as $0. Chat, orchestration, and local services stay distinct
        in the breakdowns below.</div></div>
    ${budgetBanner(c.budget_warning, lim.confirm_above_usd)}
    <div class="cost-row rise">${metric(c.today, "Today")}${metric(c.week, "This week")}${metric(c.month, "This month")}</div>
    ${roiBlock(roi, c.hourly_rate_usd)}
    ${estimateAccuracyCard(roi.estimate_accuracy)}
    ${primaryDims}
    ${c.by_project ? dimSection(c.by_model, "model", "By model") : ""}
    ${dimSection(c.by_provider, "provider", "By provider")}
    ${dimSection(c.by_team, "team", "By team")}
    ${dimSection(c.by_role, "agent_role", "By role")}
    ${dimSection(c.by_stage, "stage", "By stage")}
    ${dimSection(c.by_purpose, "purpose", "Chat, orchestration, and other work")}
    ${dimSection(c.by_service, "service", "By service (local tools)")}
    ${contextReuseCard(c.context_reuse)}
    ${modelRequestHealthCard(c.model_request_health)}
    <div class="surface rise"><div class="panel-title"><h3>Limits</h3></div>
      <div class="mono dim">soft/run ${money(lim.soft_warn_usd_per_run)} · hard/run ${money(lim.hard_stop_usd_per_run)}
        · confirm above ${money(lim.confirm_above_usd)}
        · project/month ${lim.project_monthly_usd == null ? "—" : money(lim.project_monthly_usd)}</div></div>`;
}
