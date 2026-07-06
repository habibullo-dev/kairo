// Meetings — consented capture → an UNREVIEWED Vault source (never an auto-action).
// Recording state is always visible; capture asks for explicit consent first.
export async function render(container, api) {
  const v = await api.get("/api/voice/status");
  const enabled = v && v.enabled;
  const meeting = (v && v.meeting) || "idle";
  container.innerHTML = `
    <h1>Meetings</h1>
    <div class="sub">Capture a meeting as reference. It lands in the Vault review queue — never acted on.</div>
    <div class="card">
      <div>State: <span class="tag ${meeting === "recording" ? "amber" : ""}">${meeting}</span></div>
      <div style="margin-top:12px"><button class="rowbtn" id="mtg-start" ${enabled ? "" : "disabled"}>Capture meeting</button></div>
      <div class="dim" id="mtg-out" style="margin-top:8px">${enabled ? "" : "Voice is off — enable it to capture."}</div>
    </div>`;
  const btn = container.querySelector("#mtg-start");
  if (btn && enabled) {
    btn.addEventListener("click", async () => {
      if (!confirm("Start recording this meeting? Everyone present should consent.")) return;
      const r = await api.post("/api/voice/meeting", { title: "Meeting" });
      container.querySelector("#mtg-out").textContent = r.ok
        ? `Captured → ${r.data.review_status} (review it in the Vault).`
        : "Capture unavailable.";
    });
  }
}
