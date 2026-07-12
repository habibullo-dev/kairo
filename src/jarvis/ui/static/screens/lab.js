// Lab — eval history + baselines + latest report. View-only: running evals stays a
// deliberate terminal ritual (the note carries the exact command).
import { esc } from "../ui/dom.js";

export async function render(container, api) {
  const lab = await api.get("/api/lab");
  if (!lab) { container.innerHTML = `<div class="rise"><h1>Lab</h1><div class="sub">Unavailable.</div></div>`; return; }
  const rows = (lab.history || []).slice().reverse()
    .map((g) => `<tr><td class="mono">${esc(g.git_rev || "?")}</td>
      <td><span class="tag ${g.verdict === "PASS" ? "ok" : "amber"}">${esc(g.verdict || "?")}</span></td>
      <td class="dim">${esc(g.suite || "")}</td></tr>`)
    .join("");
  container.innerHTML = `
    <div class="rise"><h1>Lab</h1><div class="sub">Eval gate history — how we know it still works.</div></div>
    <div class="card rise"><div class="card-label">Gate runs · ${lab.gate_runs || 0}</div>
      <table><tr><th>rev</th><th>verdict</th><th>suite</th></tr>${rows || '<tr><td class="dim">no runs yet</td></tr>'}</table></div>
    <div class="card rise"><div class="card-label">Run a gate from your terminal</div>
      <pre class="block">jarvis eval gate --profile live-chunked</pre>
      <div class="dim" style="font-size:12px;margin-top:8px">${esc(lab.note || "")}</div></div>
    <div class="card rise debug-only"><div class="card-label">baselines.yaml</div>
      <pre class="block">${esc(lab.baselines || "(none)")}</pre></div>`;
}
