// Lab — eval history + baselines + latest report. View-only: running evals stays a
// deliberate terminal ritual (the note carries the command).
export async function render(container, api) {
  const lab = await api.get("/api/lab");
  if (!lab) { container.innerHTML = `<h1>Lab</h1><div class="sub">Unavailable.</div>`; return; }
  const rows = (lab.history || []).slice().reverse()
    .map((g) => `<tr><td class="mono">${g.git_rev || "?"}</td><td><span class="tag ${g.verdict === "PASS" ? "ok" : "amber"}">${g.verdict || "?"}</span></td><td class="dim">${g.suite || ""}</td></tr>`)
    .join("");
  container.innerHTML = `
    <h1>Lab</h1><div class="sub">Eval gate history — how we know it still works.</div>
    <div class="card"><div class="label">Gate runs (${lab.gate_runs || 0})</div>
      <table><tr><th>rev</th><th>verdict</th><th>suite</th></tr>${rows || '<tr><td class="dim">no runs yet</td></tr>'}</table></div>
    <div class="card"><div class="dim">${lab.note || ""}</div></div>
    <div class="card debug-only"><div class="label">baselines.yaml</div><pre class="mono">${escPre(lab.baselines)}</pre></div>`;
}
function escPre(s) { const d = document.createElement("div"); d.textContent = s || "(none)"; return d.innerHTML; }
