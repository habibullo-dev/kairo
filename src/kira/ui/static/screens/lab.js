// Lab — eval history + baselines + latest report. View-only: running evals stays a
// deliberate terminal ritual, with backend-owned commands and safety guidance.
import { esc } from "../ui/dom.js";

function latestReportCard(report) {
  if (!report || typeof report !== "object") {
    return `<section class="card rise" aria-labelledby="lab-latest-title">
      <h2 id="lab-latest-title" class="lab-report-title">Latest gate summary</h2>
      <div class="empty-state lab-report-empty"><h4>No report yet</h4>
        <div>Start with the keyless replay command below.</div></div></section>`;
  }
  const notices = [
    report.redacted ? "Potential credential-shaped text was hidden." : "",
    report.truncated ? "Preview capped for responsiveness." : "",
  ].filter(Boolean).map((note) => `<div class="dim lab-report-notice">${note}</div>`).join("");
  return `<section class="card rise" aria-labelledby="lab-latest-title">
    <div class="lab-report-head"><h2 id="lab-latest-title" class="lab-report-title">Latest gate summary</h2>
      <span class="mono lab-report-run">${esc(report.run_id || "")}</span></div>
    <div class="dim lab-report-copy">Human-readable report only. Raw records and transcripts are not loaded.</div>
    <pre class="block lab-report-preview" tabindex="0" aria-label="Latest eval gate summary">${esc(report.preview || "")}</pre>
    ${notices}</section>`;
}

export async function render(container, api) {
  const lab = await api.getRequired("/api/lab");
  if (!lab) { container.innerHTML = `<div class="rise"><h1>Lab</h1><div class="sub">Unavailable.</div></div>`; return; }
  const rows = (lab.history || []).slice().reverse()
    .map((g) => `<tr><td class="mono">${esc(g.git_rev || "?")}</td>
      <td><span class="tag ${g.verdict === "PASS" ? "ok" : "amber"}">${esc(g.verdict || "?")}</span></td>
      <td class="dim">${esc(g.suite || "")}</td></tr>`)
    .join("");
  const latest = latestReportCard(lab.latest_report);
  container.innerHTML = `
    <div class="rise"><h1>Lab</h1><div class="sub">Eval gate history — how we know it still works.</div></div>
    <div class="card rise"><div class="card-label">Gate runs · ${lab.gate_runs || 0}</div>
      <table><tr><th>rev</th><th>verdict</th><th>suite</th></tr>${rows || '<tr><td class="dim">no runs yet</td></tr>'}</table></div>
    ${latest}
    <div class="card rise"><div class="card-label">Keyless replay · recommended first</div>
      <pre class="block">${esc(lab.replay_command || "")}</pre>
      <div class="card-label">Small live scenario · may spend</div>
      <pre class="block">${esc(lab.live_command || "")}</pre>
      <div class="dim lab-note">${esc(lab.note || "")}</div></div>
    <div class="card rise debug-only"><div class="card-label">baselines.yaml</div>
      <pre class="block">${esc(lab.baselines || "(none)")}</pre></div>`;
}
