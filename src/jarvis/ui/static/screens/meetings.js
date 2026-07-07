// Meetings — consented capture → an UNREVIEWED Vault source (never an auto-action).
// Recording state is always visible; capture asks for explicit consent first.
export async function render(container, api) {
  const v = await api.get("/api/voice/status");
  const enabled = v && v.enabled;
  const meeting = (v && v.meeting) || "idle";
  const recording = meeting === "recording";
  container.innerHTML = `
    <div class="rise"><h1>Meetings</h1>
      <div class="sub">Capture a meeting as reference. It lands in the Vault review queue — never acted on.</div></div>
    <div class="card rise"><div class="zone-now">
      <span class="runner-dot${recording ? " busy" : ""}"${recording ? ' style="background:var(--amber)"' : ""}></span>
      <div class="body">
        <div class="lead${recording ? "" : " idle"}">${recording ? "Recording…" : "Not recording"}</div>
        <div class="desc">${enabled ? "Everyone present should consent before you capture." : "Voice is off — enable it to capture."}</div>
      </div>
      <button class="btn btn-amber" id="mtg-start" ${enabled ? "" : "disabled"}>Capture meeting</button>
    </div><div class="dim" id="mtg-out" style="margin-top:10px"></div></div>
    <div class="card rise"><div class="card-label">Past captures</div>
      <div class="dim" style="font-size:13px">Captured transcripts appear in the <a href="#vault">Vault</a> review queue as unreviewed sources.</div></div>`;
  const btn = container.querySelector("#mtg-start");
  if (btn && enabled) {
    btn.addEventListener("click", async () => {
      if (!confirm("Start recording this meeting? Everyone present should consent.")) return;
      const r = await api.post("/api/voice/meeting", { title: "Meeting" });
      container.querySelector("#mtg-out").textContent = r.ok
        ? `Captured → ${r.data.review_status} — review it in the Vault.`
        : "Capture unavailable.";
    });
  }
}
